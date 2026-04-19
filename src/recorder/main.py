"""Recorder service: a GPIO switch level controls an `arecord` subprocess.

Design goals:
- Tiny and robust. If the uploader or webapp crashes, recording keeps working.
- No audio data is held in Python memory; `arecord` writes the WAV directly to disk.
- SIGINT (not SIGKILL) on stop so arecord can finalize the WAV header cleanly.
- Long-press acts as a safety stop in case state ever drifts.

Button semantics are level-triggered (think slide switch, not toy push-button):
    GPIO17 HI  -> recording (switch open / button not pressed, pull-up wins)
    GPIO17 LO  -> stopped   (switch closed / button pressed, line pulled to GND)

At boot we deliberately ignore the pin's *current* level and only act on
transitions. That means even if the pin boots HI we do NOT auto-start --
the operator has to flick the switch to LO (stop) and back to HI (record)
before a recording begins. gpiozero's `when_pressed` / `when_released`
are edge-triggered, which gives us this behaviour for free.

Run as: `python -m recorder.main` from /opt/audiorec/src (see systemd unit).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common import db
from common.config import Config, is_audio_configured, load_config


log = logging.getLogger("audiorec.recorder")


@dataclass
class ActiveRecording:
    rec_id: str
    path: Path
    proc: subprocess.Popen
    started_monotonic: float


class Recorder:
    """Owns at most one active arecord subprocess and a matching DB row."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.conn = db.connect(cfg.paths.db_path)
        self._lock = threading.Lock()
        self._active: Optional[ActiveRecording] = None
        self._stopping = False

        cfg.paths.recordings_dir.mkdir(parents=True, exist_ok=True)

        orphaned = db.reset_orphaned_on_startup(self.conn)
        if orphaned:
            log.warning("Recovered %d orphaned 'recording' rows from previous crash", orphaned)

    # --- public API (called by button handler and signal handlers) ---

    def start(self) -> None:
        """Begin recording if idle. Idempotent: no-op if already recording."""
        with self._lock:
            if self._active is None:
                self._start_locked()
            else:
                log.debug("start() called while already recording; ignoring")

    def stop(self) -> None:
        """Stop the current recording if any. Idempotent: no-op if idle."""
        with self._lock:
            if self._active is not None:
                self._stop_locked()
            else:
                log.debug("stop() called while idle; ignoring")

    def toggle(self) -> None:
        """Flip state: start if idle, stop if recording. Used by the web UI."""
        with self._lock:
            if self._active is None:
                self._start_locked()
            else:
                self._stop_locked()

    def force_stop(self) -> None:
        """Long-press safety net: always stops if recording."""
        with self._lock:
            if self._active is not None:
                log.warning("Force-stop triggered by long press")
                self._stop_locked()

    def shutdown(self) -> None:
        """Gracefully stop any active recording on service exit."""
        self._stopping = True
        with self._lock:
            if self._active is not None:
                log.info("Shutdown: stopping active recording")
                self._stop_locked()
        try:
            self.conn.close()
        except Exception:
            pass

    # --- internals (must hold self._lock) ---

    def _start_locked(self) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{ts}.wav"
        path = self.cfg.paths.recordings_dir / filename

        rec = db.create_recording(self.conn, filename)

        cmd = [
            "arecord",
            "-q",
            "-D", self.cfg.audio.device,
            "-f", self.cfg.audio.format,
            "-r", str(self.cfg.audio.sample_rate),
            "-c", str(self.cfg.audio.channels),
            "-t", "wav",
            str(path),
        ]
        log.info("Starting recording %s -> %s", rec.id, path)
        log.debug("arecord cmd: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._active = ActiveRecording(
            rec_id=rec.id,
            path=path,
            proc=proc,
            started_monotonic=time.monotonic(),
        )
        _fire_led(self.cfg.gpio.led_pin, on=True)

    def _stop_locked(self) -> None:
        active = self._active
        assert active is not None
        self._active = None

        duration = time.monotonic() - active.started_monotonic

        # SIGINT lets arecord write a proper RIFF size in the WAV header.
        try:
            active.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass

        try:
            _, stderr = active.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("arecord did not exit after SIGINT, killing")
            active.proc.kill()
            _, stderr = active.proc.communicate()

        if active.proc.returncode not in (0, -signal.SIGINT, 130):
            err_text = (stderr or b"").decode("utf-8", errors="replace").strip()
            log.error("arecord exited %s: %s", active.proc.returncode, err_text)

        try:
            size = active.path.stat().st_size
        except FileNotFoundError:
            size = 0
            log.error("Recording file missing after stop: %s", active.path)

        db.finish_recording(self.conn, active.rec_id, duration_s=duration, size_bytes=size)
        log.info("Stopped recording %s (%.1fs, %d bytes)", active.rec_id, duration, size)
        _fire_led(self.cfg.gpio.led_pin, on=False)


def _fire_led(pin: int, on: bool) -> None:
    """Best-effort LED toggle; ignore if gpiozero not ready or pin disabled."""
    if pin <= 0:
        return
    try:
        from gpiozero import LED  # imported lazily so this module is testable
        led = _led_cache.get(pin)
        if led is None:
            led = LED(pin)
            _led_cache[pin] = led
        if on:
            led.on()
        else:
            led.off()
    except Exception as e:  # pragma: no cover - hardware-only path
        log.debug("LED toggle failed: %s", e)


_led_cache: dict[int, object] = {}


def _install_signal_handlers(recorder: Recorder) -> None:
    def _graceful(signum, _frame):
        log.info("Received signal %s, shutting down", signum)
        recorder.shutdown()
        sys.exit(0)

    def _toggle_signal(_signum, _frame):
        log.info("SIGUSR1 received -> toggle")
        try:
            recorder.toggle()
        except Exception:
            log.exception("toggle via signal failed")

    def _force_stop_signal(_signum, _frame):
        log.info("SIGUSR2 received -> force_stop")
        try:
            recorder.force_stop()
        except Exception:
            log.exception("force_stop via signal failed")

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGUSR1, _toggle_signal)
    signal.signal(signal.SIGUSR2, _force_stop_signal)


def _setup_button(cfg: Config, recorder: Recorder) -> None:
    """Wire a latching GPIO switch to the recorder.

    Inverted active-low hardware, so the semantics are:
    - Pin at HI (switch open / pull-up wins):       record
    - Pin at LO (switch closed / pulled to GND):    stop

    gpiozero's callbacks are edge-triggered, so the *initial* level at
    process startup doesn't fire anything. A switch left in either
    position at power-up is therefore silent until the user actually
    moves it -- safe default, and it matches the original "wait for a
    lo-then-hi cycle before acting" requirement: if the pin is HI at
    boot, you have to flick it LO (stop, no-op) and back to HI (start)
    before a recording begins.
    """
    from gpiozero import Button, Device  # imported lazily for non-Pi test envs

    button = Button(
        cfg.gpio.button_pin,
        pull_up=True,
        bounce_time=cfg.gpio.debounce_s,
        hold_time=cfg.gpio.long_press_s,
    )

    # `when_pressed` fires on the HI->LO edge (button down / switch closed).
    # `when_released` fires on the LO->HI edge (button up / switch open).
    # We want HI == record, so those map to stop() / start() respectively.

    def _on_pressed() -> None:
        log.info("GPIO%d edge: HI->LO (button down / switch OFF) -> stop()",
                 cfg.gpio.button_pin)
        try:
            recorder.stop()
        except Exception:
            log.exception("recorder.stop() from edge failed")

    def _on_released() -> None:
        log.info("GPIO%d edge: LO->HI (button up / switch ON) -> start()",
                 cfg.gpio.button_pin)
        try:
            recorder.start()
        except Exception:
            log.exception("recorder.start() from edge failed")

    button.when_pressed = _on_pressed
    button.when_released = _on_released
    # No when_held handler: holding HI (or LO) indefinitely is the normal
    # operating mode of a latching switch; we must not force_stop on a
    # timer or every recording would be truncated.

    pin_factory_name = type(Device.pin_factory).__name__ if Device.pin_factory else "?"
    initial_level = "LO (button DOWN / idle)" if button.is_pressed else "HI (button UP / ready to record)"
    log.info(
        "Button on GPIO%d ready: pin_factory=%s, level-triggered "
        "(HI=record, LO=stop), initial=%s. "
        "Edges will be ignored until the switch changes state.",
        cfg.gpio.button_pin, pin_factory_name, initial_level,
    )

    # Keep a reference so the Button isn't garbage-collected.
    global _button_ref
    _button_ref = button


_button_ref: Optional[object] = None


def _install_idle_signal_handlers() -> None:
    """Minimal handlers for the 'awaiting setup' idle state."""
    def _exit(_signum, _frame):
        log.info("Idle recorder exiting")
        sys.exit(0)

    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("AUDIOREC_LOG", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config()

    if not is_audio_configured(cfg):
        # First-boot / partially configured install. Idle politely until the
        # webapp finishes setup and asks systemd to restart us with new config.
        log.warning(
            "No microphone configured. Waiting for setup wizard "
            "(http://%s/setup) to finish.",
            cfg.web.hostname or "audiorec.local",
        )
        _install_idle_signal_handlers()
        try:
            signal.pause()
        except KeyboardInterrupt:
            pass
        return 0

    recorder = Recorder(cfg)
    _install_signal_handlers(recorder)
    _setup_button(cfg, recorder)

    # Write our PID so the webapp can signal us (SIGUSR1 = toggle).
    pidfile = cfg.paths.data_dir / "recorder.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    log.info(
        "Recorder ready. Button on GPIO%d, device=%s, %dHz %dch, pid=%d",
        cfg.gpio.button_pin,
        cfg.audio.device,
        cfg.audio.sample_rate,
        cfg.audio.channels,
        os.getpid(),
    )

    try:
        # Block forever; gpiozero runs callbacks on its own thread, signals wake us.
        signal.pause()
    except KeyboardInterrupt:
        recorder.shutdown()
    finally:
        try:
            pidfile.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
