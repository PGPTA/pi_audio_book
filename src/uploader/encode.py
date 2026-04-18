"""ffmpeg wrapper: WAV -> Opus for cheap voice-quality uploads."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path


log = logging.getLogger("audiorec.uploader.encode")


class EncodeError(RuntimeError):
    pass


def wav_to_opus(src: Path, dst: Path, bitrate: str) -> None:
    """Encode WAV to Opus at the given bitrate (e.g. '24k').

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
