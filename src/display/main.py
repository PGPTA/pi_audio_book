"""Display service: drives the 2.13" Touch e-Paper HAT as a guest keypad.

Flow:
    IDLE         "touch to begin"             -> tap anywhere -> KEYBOARD
    KEYBOARD     QWERTY + preview             -> OK -> READY
                                               or CANCEL -> IDLE
    READY        "pick up the phone, <name>"  -> guest picks up handset
                                                 (DB row appears) -> RECORDING
                                               or edit/cancel touch
    RECORDING    "RECORDING <name> mm:ss"     -> guest hangs up handset
                                                 (DB row leaves 'recording')
                                                 -> SAVED
    SAVED        "saved. thanks, <name>"      -> timeout -> IDLE
                                               or tap -> KEYBOARD

The recorder is still the sole owner of the arecord subprocess. We talk
to it purely via:
  1. `current_name.txt` in the data dir (we write, it consumes)
  2. The `recordings` row status (we observe it transitioning through
     'recording' -> 'pending_upload').

That deliberately keeps the display out of arecord's hot path -- if the
display service crashes, pressing the handset still records audio (just
without a name).
"""
from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from common import db
from common.config import Config, load_config
from display import epd as epd_mod
from display import ui


log = logging.getLogger("audiorec.display")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NAME_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def slugify(name: str) -> str:
    """Produce a filesystem-safe token from a user-typed name."""
    cleaned = _NAME_SLUG_RE.sub("_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "guest"


def write_current_name(cfg: Config, name: str) -> None:
    """Atomically publish `name` to the shared name file."""
    target = cfg.paths.current_name_file
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(name + "\n", encoding="utf-8")
    os.replace(tmp, target)


def clear_current_name(cfg: Config) -> None:
    try:
        cfg.paths.current_name_file.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class State(str, Enum):
    IDLE = "idle"
    KEYBOARD = "keyboard"
    READY = "ready"
    RECORDING = "recording"
    SAVED = "saved"


@dataclass
class Ctx:
    cfg: Config
    epd: epd_mod.EPD
    conn: object  # sqlite3.Connection
    state: State = State.IDLE
    typed: str = ""
    last_full_refresh: float = 0.0
    state_entered_at: float = field(default_factory=time.monotonic)
    last_touch_time: float = 0.0
    zones: list[ui.HitZone] = field(default_factory=list)
    # Track the recording we're displaying so we recognise when it ends.
    active_rec_id: Optional[str] = None
    # Waveform tick for the RECORDING screen's blinking dots.
    tick: int = 0
    # Blink-cursor toggle for keyboard preview.
    caret_on: bool = True


# ---------------------------------------------------------------------------
# Rendering dispatch
# ---------------------------------------------------------------------------


def render_full(ctx: Ctx) -> None:
    w, h = ctx.epd.width, ctx.epd.height
    touch = ctx.epd.touch_available
    if ctx.state == State.IDLE:
        if touch:
            img, zones = ui.render_idle(w, h)
        else:
            img, zones = ui.render_idle_no_touch(w, h)
    elif ctx.state == State.KEYBOARD:
        img, zones = ui.render_keyboard(w, h, ctx.typed, caret=ctx.caret_on)
    elif ctx.state == State.READY:
        img, zones = ui.render_ready(w, h, ctx.typed)
    elif ctx.state == State.RECORDING:
        elapsed = time.monotonic() - ctx.state_entered_at
        img, zones = ui.render_recording(w, h, ctx.typed, elapsed, ctx.tick)
    elif ctx.state == State.SAVED:
        img, zones = ui.render_saved(w, h, ctx.typed)
    else:
        img, zones = ui.render_idle(w, h)
    ctx.zones = zones
    ctx.epd.full_refresh(img)
    ctx.last_full_refresh = time.monotonic()


def render_partial(ctx: Ctx) -> None:
    """Cheap refresh of just-changed areas. Falls back to full if needed."""
    w, h = ctx.epd.width, ctx.epd.height
    if ctx.state == State.KEYBOARD:
        img = ui.render_keyboard_preview(w, h, ctx.typed, caret=ctx.caret_on)
        ctx.epd.partial_refresh(img)
    else:
        render_full(ctx)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def go(ctx: Ctx, new_state: State) -> None:
    log.info("state: %s -> %s (name=%r)", ctx.state.value, new_state.value, ctx.typed)
    ctx.state = new_state
    ctx.state_entered_at = time.monotonic()
    ctx.tick = 0
    render_full(ctx)


def on_touch(ctx: Ctx, x: int, y: int) -> None:
    ctx.last_touch_time = time.monotonic()
    if not ctx.epd.touch_available:
        return  # impossible, but defend against it
    if ctx.state == State.IDLE or ctx.state == State.SAVED:
        ctx.typed = ""
        go(ctx, State.KEYBOARD)
        return
    if ctx.state == State.KEYBOARD:
        _on_keyboard_touch(ctx, x, y)
        return
    if ctx.state == State.READY:
        for z in ctx.zones:
            if z.contains(x, y):
                if z.label == "edit":
                    go(ctx, State.KEYBOARD)
                    return
                if z.label == "cancel":
                    ctx.typed = ""
                    clear_current_name(ctx.cfg)
                    go(ctx, State.IDLE)
                    return
    # RECORDING: ignore touches; the physical handset is the control.


def _on_keyboard_touch(ctx: Ctx, x: int, y: int) -> None:
    for z in ctx.zones:
        if not z.contains(x, y):
            continue
        label = z.label
        if label == "<<":  # backspace
            ctx.typed = ctx.typed[:-1]
            render_partial(ctx)
            return
        if label == "DONE":
            name = ctx.typed.strip()
            if not name:
                return  # refuse empty
            write_current_name(ctx.cfg, name)
            go(ctx, State.READY)
            return
        if label.isalpha() and len(ctx.typed) < ctx.cfg.display.max_name_length:
            # Show the first letter capitalised, the rest lower-case. Feels
            # more natural for names on a caps-only keyboard layout.
            ch = label.lower() if ctx.typed else label
            ctx.typed += ch
            render_partial(ctx)
            return


# ---------------------------------------------------------------------------
# Recorder-state observers
# ---------------------------------------------------------------------------


def poll_recording_state(ctx: Ctx) -> None:
    """Cross-check the DB and advance state when the handset is picked up/hung up."""
    current = db.current_recording(ctx.conn)

    if ctx.state == State.READY and current is not None:
        # Guest picked up the handset -> recorder started. Move to RECORDING.
        ctx.active_rec_id = current.id
        ctx.state_entered_at = time.monotonic()
        go(ctx, State.RECORDING)
        return

    if ctx.state == State.RECORDING and current is None:
        # Handset hung up -> recorder finalised the WAV.
        ctx.active_rec_id = None
        # Clear the name so a subsequent stray handset pickup (no touch screen
        # interaction) goes in as 'anonymous' rather than reusing the last guest.
        clear_current_name(ctx.cfg)
        go(ctx, State.SAVED)
        return

    if ctx.state == State.IDLE and current is not None:
        # Someone picked up the handset without typing a name. In display-only
        # (no-touch) mode this is the *normal* path; in touch mode it's an
        # edge case (physical switch flipped while idle, no name typed).
        # Either way the recorder writes `anonymous_<ts>.wav`. Surface that
        # so the screen isn't lying.
        ctx.typed = "anonymous"
        ctx.active_rec_id = current.id
        ctx.state_entered_at = time.monotonic()
        go(ctx, State.RECORDING)
        return

    if ctx.state == State.SAVED and not ctx.epd.touch_available:
        # In kiosk (no-touch) mode there's no way to get out of SAVED via a
        # tap; the timeout in _tick_loop handles it.
        return


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


_shutdown = threading.Event()


def _install_signal_handlers() -> None:
    def _stop(signum, _frame):
        log.info("signal %s -> shutdown", signum)
        _shutdown.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


def _tick_loop(ctx: Ctx) -> None:
    TOUCH_HZ = 20
    STATE_POLL_S = 0.5
    last_state_poll = 0.0
    last_caret_flip = time.monotonic()
    last_anim_tick = time.monotonic()

    while not _shutdown.is_set():
        loop_start = time.monotonic()

        # Touch events. Skipped entirely in display-only (no-touch) mode.
        if ctx.epd.touch_available:
            try:
                pts = ctx.epd.read_touch()
            except Exception:
                log.exception("read_touch failed")
                pts = []
            if pts:
                x, y = pts[0]
                # Debounce: ignore touches spaced <120ms apart
                if loop_start - ctx.last_touch_time > 0.12:
                    on_touch(ctx, x, y)

        now = time.monotonic()

        # Caret blink during keyboard entry.
        if ctx.state == State.KEYBOARD and now - last_caret_flip > 0.7:
            ctx.caret_on = not ctx.caret_on
            last_caret_flip = now
            render_partial(ctx)

        # Recording screen animation + periodic full redraw for the clock.
        if ctx.state == State.RECORDING and now - last_anim_tick > 1.0:
            ctx.tick += 1
            last_anim_tick = now
            render_full(ctx)

        # SAVED -> IDLE timeout
        if ctx.state == State.SAVED:
            if now - ctx.state_entered_at > ctx.cfg.display.thank_you_seconds:
                ctx.typed = ""
                go(ctx, State.IDLE)

        # Cross-check recorder DB roughly twice per second.
        if now - last_state_poll > STATE_POLL_S:
            try:
                poll_recording_state(ctx)
            except Exception:
                log.exception("poll_recording_state failed")
            last_state_poll = now

        # Sleep out the remainder of the touch-polling budget.
        elapsed = time.monotonic() - loop_start
        slack = max(0.0, (1.0 / TOUCH_HZ) - elapsed)
        if slack:
            time.sleep(slack)

    # Shutdown
    try:
        ctx.epd.sleep()
    except Exception:
        pass


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("AUDIOREC_LOG", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config()
    if not cfg.display.enabled:
        log.warning(
            "display.enabled = false in config. Idling. "
            "Set it to true in /etc/audiorec/config.toml to activate the e-paper."
        )
        _install_signal_handlers()
        _shutdown.wait()
        return 0

    _install_signal_handlers()
    log.info("Opening EPD (panel=%s, rotate=%d)", cfg.display.panel, cfg.display.rotate_deg)
    epd = epd_mod.wait_until_available(
        panel=cfg.display.panel,
        rotate_deg=cfg.display.rotate_deg,
    )
    conn = db.connect(cfg.paths.db_path)

    ctx = Ctx(cfg=cfg, epd=epd, conn=conn)
    # Start from a known-good blank state.
    clear_current_name(cfg)
    go(ctx, State.IDLE)

    try:
        _tick_loop(ctx)
    except Exception:
        log.exception("display loop crashed")
        return 1
    finally:
        try:
            epd.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
