"""Recorder service: a GPIO switch level controls an `arecord` subprocess.

Design goals:
- Tiny and robust. If the uploader or webapp crashes, recording keeps working.
- No audio data is held in Python memory; `arecord` writes the WAV directly to disk.
- SIGINT (not SIGKILL) on stop so arecord can finalize the WAV header cleanly.
- Long-press acts as a safety stop in case state ever drifts.

Button semantics are level-triggered (think slide switch, not toy push-button):
    GPIO17 LO  -> recording (switch closed to ground, pull-up defeated)
    GPIO17 HI  -> stopped   (switch open, pull-up pulls the line high)

At boot we deliberately ignore the pin's *current* level and only act on
transitions. That means a switch accidentally left in the "on" position
at power-up does NOT silently begin a recording -- the operator has to
flick it off and then on again before anything happens. gpiozero's
`when_pressed` / `when_released` are edge-triggered, which gives us
this behaviour for free.

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

    - Pin goes LO (switch closed, button pressed):  start recording
    - Pin goes HI (switch open,  button released):  stop  recording

    gpiozero's callbacks are edge-triggered, so the *initial* state at
    process startup doesn't fire anything. That's intentional: if the
    switch was left in the "on" position when the Pi was powered up,
    we do not start a recording until the operator has actually
    flicked it -- first off (HI), then on (LO). Only then does the
    LO edge trigger a start.
    """
    from gpiozero import Button, Device  # imported lazily for non-Pi test envs

    button = Button(
        cfg.gpio.button_pin,
        pull_up=True,
        bounce_time=cfg.gpio.debounce_s,
        hold_time=cfg.gpio.long_press_s,
    )

    def _on_pressed() -> None:
        log.info("GPIO%d edge: HI->LO (switch ON) -> start()", cfg.gpio.button_pin)
        try:
            recorder.start()
        except Exception:
            log.exception("recorder.start() from edge failed")

    def _on_released() -> None:
        log.info("GPIO%d edge: LO->HI (switch OFF) -> stop()", cfg.gpio.button_pin)
        try:
            recorder.stop()
        except Exception:
            log.exception("recorder.stop() from edge failed")

    def _on_held() -> None:
        log.warning("GPIO%d held >= %.1fs -> force_stop()",
                    cfg.gpio.button_pin, cfg.gpio.long_press_s)
        try:
            recorder.force_stop()
        except Exception:
            log.exception("recorder.force_stop() from edge failed")

    button.when_pressed = _on_pressed
    button.when_released = _on_released
    button.when_held = _on_held

    pin_factory_name = type(Device.pin_factory).__name__ if Device.pin_factory else "?"
    initial_level = "LO (switch ON)" if button.is_pressed else "HI (switch OFF)"
    log.info(
        "Button on GPIO%d ready: pin_factory=%s, level-triggered, initial=%s. "
        "Edges will be ignored until the switch changes state.",
        cfg.gpio.button_pin, pin_factory_name, initial_level,
    )

    # Diagnostic watchdog: poll the pin at 2 Hz and log whenever the level
    # changes without a corresponding edge callback having fired. If the
    # logs show "poll noticed LO but no edge fired", edge detection is
    # broken (hardware or lgpio config); if they show nothing at all when
    # you flick the switch, the pin's simply not moving (wiring issue).
    def _pin_watchdog() -> None:
        last = button.is_pressed
        last_edge_level = last
        while True:
            time.sleep(0.5)
            try:
                now = button.is_pressed
            except Exception:
                return
            if now != last:
                log.debug("GPIO%d poll: %s -> %s",
                          cfg.gpio.button_pin,
                          "LO" if last else "HI",
                          "LO" if now else "HI")
                if now == last_edge_level:
                    log.warning(
                        "GPIO%d level changed but no edge callback fired "
                        "(current=%s, last-edge-saw=%s). "
                        "Edge detection may be broken.",
                        cfg.gpio.button_pin,
                        "LO" if now else "HI",
                        "LO" if last_edge_level else "HI",
                    )
                last = now

    # Tag edge callbacks so the watchdog can tell if they fired.
    def _tracked_pressed():
        nonlocal_state["last_edge_level"] = True  # True = pressed (LO)
        _on_pressed()

    def _tracked_released():
        nonlocal_state["last_edge_level"] = False  # False = released (HI)
        _on_released()

    nonlocal_state = {"last_edge_level": button.is_pressed}
    button.when_pressed = _tracked_pressed
    button.when_released = _tracked_released

    # Rewire watchdog to consult the shared last_edge_level.
    def _pin_watchdog_v2() -> None:
        last_poll = button.is_pressed
        while True:
            time.sleep(0.5)
            try:
                now = button.is_pressed
            except Exception:
                return
            if now == last_poll:
                continue
            log.debug("GPIO%d poll: %s -> %s",
                      cfg.gpio.button_pin,
                      "LO" if last_poll else "HI",
                      "LO" if now else "HI")
            if nonlocal_state["last_edge_level"] != now:
                log.warning(
                    "GPIO%d: poll saw %s but last edge saw %s. "
                    "Edge callback didn't fire for this transition.",
                    cfg.gpio.button_pin,
                    "LO" if now else "HI",
                    "LO" if nonlocal_state["last_edge_level"] else "HI",
                )
            last_poll = now

    threading.Thread(target=_pin_watchdog_v2, name="gpio-watchdog", daemon=True).start()

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
