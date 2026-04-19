"""ffmpeg wrapper: WAV -> Opus for cheap voice-quality uploads.

The optional `filters` string is a comma-separated ffmpeg audio filter chain
applied *before* encoding. Sensible defaults target Pi Zero 2 W + USB mic
interference patterns:

    highpass=f=80      -- roll off mains hum (50/60 Hz) + low-frequency rumble
    lowpass=f=8000     -- kill the ultrasonic whine above the voice band
    afftdn=nr=12       -- FFT-based noise reduction, 12 dB attenuation

Pass an empty string to disable filtering entirely.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path


log = logging.getLogger("audiorec.uploader.encode")


class EncodeError(RuntimeError):
    pass


def wav_to_opus(src: Path, dst: Path, bitrate: str, filters: str = "") -> None:
    """Encode WAV to Opus at the given bitrate (e.g. '32k').

    If `filters` is non-empty, it's passed to ffmpeg as -af to clean up the
    signal (hum, hiss, ultrasonic noise) before the Opus encoder.

    Uses a single ffmpeg thread to keep the recorder's CPU share safe.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-threads", "1",
        "-i", str(src),
    ]
    if filters.strip():
        cmd += ["-af", filters.strip()]
    cmd += [
        "-c:a", "libopus",
        "-b:a", bitrate,
        "-application", "voip",
        str(dst),
    ]
    log.debug("encode: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise EncodeError(f"ffmpeg failed ({result.returncode}): {stderr}")
