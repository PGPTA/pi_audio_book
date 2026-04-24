"""Pure-PIL rendering for the e-paper guest flow.

Each `render_*` function takes a state object and returns a 1-bit PIL image
sized (width, height) in logical canvas coordinates. It also returns the
list of touch hit-zones for that screen so the state machine can map a
touch-point back to a button id without re-deriving the layout.

Design notes:
- We stick to the `1` mode (black on white) -- the 2.13" panel is BW only.
- Fonts fall back to PIL's default bitmap font if DejaVu isn't installed,
  so nothing crashes on an underprovisioned image.
- Hit-zones are returned as (label, x0, y0, x1, y1) tuples, inclusive.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("audiorec.display.ui")


BLACK = 0
WHITE = 255


# ---------------------------------------------------------------------------
# Fonts (lazy + cached). DejaVu ships with Raspberry Pi OS; fall back to
# Pillow's built-in bitmap if for some reason it's absent.
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]

_font_cache: dict[int, ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.ImageFont:
    if size in _font_cache:
        return _font_cache[size]
    for path in _FONT_CANDIDATES:
        try:
            f = ImageFont.truetype(path, size)
            _font_cache[size] = f
            return f
        except OSError:
            continue
    _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


# ---------------------------------------------------------------------------
# Hit-zone helper
# ---------------------------------------------------------------------------


@dataclass
class HitZone:
    label: str
    x0: int
    y0: int
    x1: int
    y1: int

    def contains(self, x: int, y: int) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


def _new_canvas(width: int, height: int):
    img = Image.new("1", (width, height), WHITE)
    return img, ImageDraw.Draw(img)


def _center_text(draw, xy_box, text, font):
    """Draw `text` centered inside the bounding box `xy_box = (x0, y0, x1, y1)`."""
    x0, y0, x1, y1 = xy_box
    try:
        tw, th = draw.textbbox((0, 0), text, font=font)[2:]
    except AttributeError:
        tw, th = draw.textsize(text, font=font)  # type: ignore[attr-defined]
    tx = x0 + ((x1 - x0) - tw) // 2
    ty = y0 + ((y1 - y0) - th) // 2
    draw.text((tx, ty), text, font=font, fill=BLACK)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


def render_idle(width: int, height: int) -> tuple[Image.Image, list[HitZone]]:
    """Big "touch to start" banner filling the whole screen."""
    img, draw = _new_canvas(width, height)
    draw.rectangle((0, 0, width - 1, height - 1), outline=BLACK, width=2)
    draw.rectangle((8, 8, width - 9, height - 9), outline=BLACK, width=1)

    _center_text(draw, (0, 10, width, 50), "wedding.phone", _font(22))
    _center_text(draw, (0, 46, width, 86), "touch to begin", _font(18))
    _center_text(
        draw, (0, height - 28, width, height - 8),
        "tap anywhere", _font(12),
    )

    # Whole screen is the "start" hit zone.
    return img, [HitZone("start", 0, 0, width - 1, height - 1)]


# -- Keyboard ---------------------------------------------------------------

_KB_ROW_1 = list("QWERTYUIOP")
_KB_ROW_2 = list("ASDFGHJKL")
_KB_ROW_3 = list("ZXCVBNM")

_SPECIAL_BACKSPACE = "<<"
_SPECIAL_DONE = "DONE"


def _keyboard_layout(
    width: int,
    height: int,
    preview_h: int,
) -> list[HitZone]:
    """Compute hit-zones for the QWERTY keyboard, bottom-aligned."""
    zones: list[HitZone] = []
    top = preview_h + 2
    kb_h = height - top
    row_h = kb_h // 3
    # Evenly divide 10 columns across the full width for visual density.
    col_w = width // 10

    def place_row(letters: list[str], row_idx: int, x_offset_px: int = 0) -> None:
        y0 = top + row_idx * row_h
        y1 = y0 + row_h - 1
        n = len(letters)
        total_w = col_w * n
        left = (width - total_w) // 2 + x_offset_px
        for i, ch in enumerate(letters):
            x0 = left + i * col_w
            x1 = x0 + col_w - 1
            zones.append(HitZone(ch, x0, y0, x1, y1))

    place_row(_KB_ROW_1, 0)
    place_row(_KB_ROW_2, 1)

    # Row 3: backspace | ZXCVBNM | done
    # Give the function keys 1.5 cells each.
    y0 = top + 2 * row_h
    y1 = top + 3 * row_h - 1
    func_w = int(col_w * 1.5)
    letters = _KB_ROW_3
    total_w = func_w * 2 + col_w * len(letters)
    left = (width - total_w) // 2
    zones.append(HitZone(_SPECIAL_BACKSPACE, left, y0, left + func_w - 1, y1))
    cursor = left + func_w
    for ch in letters:
        zones.append(HitZone(ch, cursor, y0, cursor + col_w - 1, y1))
        cursor += col_w
    zones.append(HitZone(_SPECIAL_DONE, cursor, y0, cursor + func_w - 1, y1))

    return zones


def render_keyboard(
    width: int,
    height: int,
    typed: str,
    caret: bool = True,
) -> tuple[Image.Image, list[HitZone]]:
    """Full keyboard screen. Use `render_keyboard_preview` for partial updates."""
    img, draw = _new_canvas(width, height)

    preview_h = 26
    _draw_preview(draw, width, preview_h, typed, caret)

    zones = _keyboard_layout(width, height, preview_h)
    font = _font(16)
    for z in zones:
        draw.rectangle((z.x0, z.y0, z.x1, z.y1), outline=BLACK, width=1)
        label = z.label
        if label == _SPECIAL_BACKSPACE:
            label = "DEL"
        elif label == _SPECIAL_DONE:
            label = "OK"
        _center_text(draw, (z.x0, z.y0, z.x1, z.y1), label, font)

    return img, zones


def render_keyboard_preview(
    width: int,
    height: int,
    typed: str,
    caret: bool = True,
) -> Image.Image:
    """Just the 'name-so-far' strip at the top, sized to the full canvas.

    The rest of the image is white so partial_refresh only repaints the
    preview stripe -- cheap and fast for per-keystroke updates.
    """
    img, draw = _new_canvas(width, height)
    _draw_preview(draw, width, 26, typed, caret)
    return img


def _draw_preview(draw, width: int, preview_h: int, typed: str, caret: bool) -> None:
    draw.rectangle((0, 0, width - 1, preview_h - 1), outline=BLACK, width=1)
    label = "name: " + (typed or "")
    # Cursor / caret character.
    if caret and len(typed) < 30:
        label += "_"
    font = _font(18)
    try:
        tw, _th = draw.textbbox((0, 0), label, font=font)[2:]
    except AttributeError:
        tw, _th = draw.textsize(label, font=font)  # type: ignore[attr-defined]
    # Left-align with 6px padding; truncate from the left if it overflows.
    if tw > width - 12:
        # Keep the tail visible so the user sees what they just typed.
        while label and tw > width - 12:
            label = label[1:]
            try:
                tw = draw.textbbox((0, 0), label, font=font)[2]
            except AttributeError:
                tw = draw.textsize(label, font=font)[0]  # type: ignore[attr-defined]
    draw.text((6, 4), label, font=font, fill=BLACK)


# -- Ready, recording, saved ------------------------------------------------


def render_ready(width: int, height: int, name: str) -> tuple[Image.Image, list[HitZone]]:
    """`name` is set; waiting for the user to pick up the handset."""
    img, draw = _new_canvas(width, height)
    draw.rectangle((0, 0, width - 1, height - 1), outline=BLACK, width=2)

    _center_text(draw, (0, 6, width, 38), name or "guest", _font(22))
    _center_text(draw, (0, 38, width, 68), "pick up the phone", _font(16))
    _center_text(draw, (0, 66, width, 94), "to start recording", _font(14))

    # Small "change name" button bottom-left, "cancel" bottom-right.
    btn_h = 22
    by0 = height - btn_h - 4
    by1 = height - 4
    w_half = width // 2 - 6
    draw.rectangle((4, by0, 4 + w_half, by1), outline=BLACK, width=1)
    _center_text(draw, (4, by0, 4 + w_half, by1), "< edit", _font(12))
    draw.rectangle((width - 4 - w_half, by0, width - 4, by1), outline=BLACK, width=1)
    _center_text(draw, (width - 4 - w_half, by0, width - 4, by1), "cancel", _font(12))

    zones = [
        HitZone("edit", 4, by0, 4 + w_half, by1),
        HitZone("cancel", width - 4 - w_half, by0, width - 4, by1),
    ]
    return img, zones


def render_recording(
    width: int,
    height: int,
    name: str,
    elapsed_s: float,
    waveform_tick: int,
) -> tuple[Image.Image, list[HitZone]]:
    """Big 'RECORDING' banner with the guest's name and an elapsed clock."""
    img, draw = _new_canvas(width, height)
    # Invert the top strip so "RECORDING" really pops.
    draw.rectangle((0, 0, width - 1, 24), fill=BLACK)
    _center_text_inverted(draw, (0, 2, width, 24), "RECORDING", _font(18))

    _center_text(draw, (0, 28, width, 60), name or "guest", _font(20))

    mins = int(elapsed_s // 60)
    secs = int(elapsed_s % 60)
    _center_text(draw, (0, 60, width, 92), f"{mins:02d}:{secs:02d}", _font(22))

    # Tiny pulse dots to prove we're alive.
    dot_y = height - 16
    for i in range(5):
        cx = width // 2 - 20 + i * 10
        filled = ((waveform_tick + i) % 5) < 2
        if filled:
            draw.ellipse((cx - 3, dot_y - 3, cx + 3, dot_y + 3), fill=BLACK)
        else:
            draw.ellipse((cx - 3, dot_y - 3, cx + 3, dot_y + 3), outline=BLACK)

    return img, []


def _center_text_inverted(draw, xy_box, text, font):
    x0, y0, x1, y1 = xy_box
    try:
        tw, th = draw.textbbox((0, 0), text, font=font)[2:]
    except AttributeError:
        tw, th = draw.textsize(text, font=font)  # type: ignore[attr-defined]
    tx = x0 + ((x1 - x0) - tw) // 2
    ty = y0 + ((y1 - y0) - th) // 2
    draw.text((tx, ty), text, font=font, fill=WHITE)


def render_saved(width: int, height: int, name: str) -> tuple[Image.Image, list[HitZone]]:
    img, draw = _new_canvas(width, height)
    draw.rectangle((0, 0, width - 1, height - 1), outline=BLACK, width=2)
    _center_text(draw, (0, 10, width, 44), "saved.", _font(26))
    _center_text(draw, (0, 44, width, 78), f"thanks, {name or 'guest'}", _font(18))
    _center_text(draw, (0, height - 28, width, height - 8),
                 "tap to add another", _font(12))
    return img, [HitZone("start", 0, 0, width - 1, height - 1)]
