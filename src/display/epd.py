"""Thin wrapper around Waveshare's 2.13" Touch e-Paper HAT driver.

The Waveshare code is vendored under $AUDIOREC_VENDOR_DIR at install time
(scripts/install.sh clones their public repo there). We only import it
lazily so this module can still be unit-tested on a laptop.

Public surface:
    EPD.open()              -> prepare the panel + touch controller
    EPD.full_refresh(img)   -> slow clean paint of the whole screen
    EPD.partial_refresh(img)-> fast partial paint (text previews, keyboard)
    EPD.sleep()             -> deep-sleep the panel before shutdown
    EPD.read_touch()        -> returns list[(x, y)] of active touch points,
                               mapped into the logical (rotated) canvas

A "logical canvas" is what UI code draws on; the wrapper handles rotation
both when pushing image data and when reading touch coordinates so the
rest of the service never has to think about the panel's native 122x250
portrait orientation.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("audiorec.display.epd")


# ---------------------------------------------------------------------------
# Vendor path setup. install.sh clones waveshare/Touch_e-Paper_HAT into
# /opt/audiorec/vendor/Touch_e-Paper_HAT/python/ and exposes its lib dir on
# PYTHONPATH via the systemd unit, but we also patch sys.path here as a
# belt-and-braces measure for local dev.
# ---------------------------------------------------------------------------

_VENDOR_CANDIDATES = [
    os.environ.get("AUDIOREC_VENDOR_TPLIB", ""),
    "/opt/audiorec/vendor/Touch_e-Paper_HAT/python",
    "/opt/audiorec/vendor/Touch_e-Paper_HAT/python/lib",
    str(Path.home() / "Touch_e-Paper_HAT/python"),
    str(Path.home() / "Touch_e-Paper_HAT/python/lib"),
]


def _ensure_vendor_on_path() -> None:
    for candidate in _VENDOR_CANDIDATES:
        if candidate and os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)


def _shim_smbus() -> None:
    """Make Waveshare's `import smbus` resolve to smbus2 inside our venv.

    The legacy `smbus` package is a C extension that only installs from apt
    (`python3-smbus`) and doesn't live on PyPI in a form venv-friendly. smbus2
    is a drop-in pure-Python replacement with the same public surface, so we
    alias it into `sys.modules` before the vendor code runs.
    """
    if "smbus" in sys.modules:
        return
    try:
        import smbus2  # type: ignore[import-not-found]
    except ImportError:
        return
    sys.modules["smbus"] = smbus2


class EPDUnavailable(RuntimeError):
    """Raised when the vendor driver or the hardware is missing."""


class EPD:
    """State-carrying wrapper. One instance per process."""

    def __init__(
        self,
        panel: str = "2in13_V4",
        rotate_deg: int = 90,
    ) -> None:
        self.panel = panel
        self.rotate_deg = rotate_deg % 360
        self._epd = None
        self._tp = None
        self._gt_dev = None
        self._gt_old = None
        self._partial_base_set = False

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        _ensure_vendor_on_path()
        _shim_smbus()
        try:
            # The Waveshare repo exposes both the panel driver and the touch
            # driver under a top-level `TP_lib` package. Module names depend
            # on the panel revision; the 2.13 V4 is `epd2in13_V4` + `gt1151`.
            from TP_lib import gt1151  # type: ignore[import-not-found]

            panel_mod_name = f"epd{self.panel}"
            panel_mod = __import__(f"TP_lib.{panel_mod_name}", fromlist=[panel_mod_name])
        except ImportError as e:
            raise EPDUnavailable(
                f"Waveshare TP_lib not found for panel={self.panel}. "
                f"Did install.sh vendor the driver? ({e})"
            ) from e

        log.info("Opening e-paper panel %s (rot=%d)", self.panel, self.rotate_deg)
        self._epd = panel_mod.EPD()
        self._epd.init(self._epd.FULL_UPDATE)
        self._epd.Clear(0xFF)
        self._partial_base_set = False

        # Touch is optional. If the GT1151 doesn't respond on I2C (board has
        # no touch chip, HAT poorly seated, etc.) we still want the screen
        # half to come up so the service can run in status-display kiosk
        # mode. Failures here are demoted to a single warning -- the rest
        # of the codebase checks `self.touch_available`.
        try:
            self._tp = gt1151.GT1151()
            self._gt_dev = gt1151.GT_Development()
            self._gt_old = gt1151.GT_Development()
            self._tp.GT_Init()
            log.info("Touch controller (GT1151) initialised on I2C")
        except OSError as e:
            log.warning(
                "Touch controller did not respond on I2C (%s). "
                "Falling back to display-only mode -- screen will still show "
                "status, but the on-screen keyboard is disabled.",
                e,
            )
            self._tp = None
            self._gt_dev = None
            self._gt_old = None
        except Exception:
            log.exception(
                "Unexpected error initialising touch controller; falling back "
                "to display-only mode."
            )
            self._tp = None
            self._gt_dev = None
            self._gt_old = None

    @property
    def touch_available(self) -> bool:
        return self._tp is not None and self._gt_dev is not None

    def close(self) -> None:
        try:
            if self._epd is not None:
                self._epd.sleep()
        except Exception:
            log.debug("EPD sleep failed on close", exc_info=True)
        try:
            # Waveshare's driver exposes module_exit via epdconfig
            from TP_lib import epdconfig  # type: ignore[import-not-found]
            epdconfig.module_exit()
        except Exception:
            log.debug("epdconfig.module_exit failed", exc_info=True)

    # -- drawing -------------------------------------------------------------

    @property
    def width(self) -> int:
        """Logical canvas width (after rotation)."""
        if self._epd is None:
            # Nominal dimensions for the 2.13" so UI code can still build
            # an image pre-open() in dry-run paths.
            return 250 if self.rotate_deg in (90, 270) else 122
        return self._epd.height if self.rotate_deg in (90, 270) else self._epd.width

    @property
    def height(self) -> int:
        if self._epd is None:
            return 122 if self.rotate_deg in (90, 270) else 250
        return self._epd.width if self.rotate_deg in (90, 270) else self._epd.height

    def _rotate_for_panel(self, image):
        """Rotate a logical-canvas image back to the panel's native orientation."""
        from PIL import Image  # local import - not needed in dry-run

        if self.rotate_deg == 0:
            return image
        if self.rotate_deg == 90:
            return image.transpose(Image.ROTATE_270)
        if self.rotate_deg == 180:
            return image.transpose(Image.ROTATE_180)
        if self.rotate_deg == 270:
            return image.transpose(Image.ROTATE_90)
        return image

    def full_refresh(self, image) -> None:
        if self._epd is None:
            raise EPDUnavailable("EPD not opened")
        panel_img = self._rotate_for_panel(image)
        # Switch back to full-update mode if we were doing partial.
        self._epd.init(self._epd.FULL_UPDATE)
        self._epd.displayPartBaseImage(self._epd.getbuffer(panel_img))
        self._partial_base_set = True

    def partial_refresh(self, image) -> None:
        if self._epd is None:
            raise EPDUnavailable("EPD not opened")
        panel_img = self._rotate_for_panel(image)
        if not self._partial_base_set:
            self._epd.displayPartBaseImage(self._epd.getbuffer(panel_img))
            self._partial_base_set = True
            return
        self._epd.init(self._epd.PART_UPDATE)
        self._epd.displayPartial(self._epd.getbuffer(panel_img))

    def sleep(self) -> None:
        if self._epd is not None:
            try:
                self._epd.sleep()
            except Exception:
                log.debug("sleep failed", exc_info=True)

    # -- touch ---------------------------------------------------------------

    def read_touch(self) -> list[tuple[int, int]]:
        """Return a snapshot of currently-pressed points in logical coords.

        Returns an empty list when nothing is being touched. Coordinates are
        already mapped into the rotated canvas.
        """
        if self._tp is None or self._gt_dev is None or self._gt_old is None:
            return []
        try:
            self._tp.GT_Scan(self._gt_dev, self._gt_old)
        except Exception:
            log.debug("GT_Scan failed", exc_info=True)
            return []
        if self._gt_dev.TouchpointFlag == 0:
            return []
        self._gt_dev.TouchpointFlag = 0
        pts: list[tuple[int, int]] = []
        for i in range(self._gt_dev.TouchCount):
            raw_x = self._gt_dev.X[i]
            raw_y = self._gt_dev.Y[i]
            pts.append(self._map_touch(raw_x, raw_y))
        return pts

    def _map_touch(self, x: int, y: int) -> tuple[int, int]:
        """Map raw (panel-native) touch coords into our rotated canvas."""
        nw = self._epd.width if self._epd is not None else 122
        nh = self._epd.height if self._epd is not None else 250
        if self.rotate_deg == 0:
            return x, y
        if self.rotate_deg == 90:
            return y, nw - 1 - x
        if self.rotate_deg == 180:
            return nw - 1 - x, nh - 1 - y
        if self.rotate_deg == 270:
            return nh - 1 - y, x
        return x, y


def wait_until_available(
    panel: str,
    rotate_deg: int,
    retry_s: float = 10.0,
) -> EPD:
    """Block until the driver and hardware are present, then return an opened EPD.

    Used by the display service on startup so a fresh Pi (that hasn't yet
    had install.sh vendor the driver) doesn't crashloop.
    """
    epd = EPD(panel=panel, rotate_deg=rotate_deg)
    while True:
        try:
            epd.open()
            return epd
        except EPDUnavailable as e:
            log.warning("EPD not available yet (%s). Retrying in %.0fs.", e, retry_s)
            time.sleep(retry_s)
        except Exception as e:
            log.exception("Unexpected error opening EPD: %s", e)
            time.sleep(retry_s)
