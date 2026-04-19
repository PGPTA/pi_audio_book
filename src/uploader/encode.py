"""ffmpeg wrapper: WAV -> {opus,mp3,wav} for cloud uploads.

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


# fmt -> (file extension, ffmpeg encoder args)
_CODECS: dict[str, tuple[str, list[str]]] = {
    "opus": (".opus", ["-c:a", "libopus", "-application", "voip"]),
    "mp3":  (".mp3",  ["-c:a", "libmp3lame"]),
    "wav":  (".wav",  ["-c:a", "pcm_s16le"]),
}


def extension_for(fmt: str) -> str:
    """Return the file extension (including dot) for a format name."""
    fmt = (fmt or "").lower()
    if fmt not in _CODECS:
        raise EncodeError(f"Unknown upload format: {fmt!r} (use opus, mp3, or wav)")
    return _CODECS[fmt][0]


def encode_audio(
    src: Path,
    dst: Path,
    fmt: str,
    bitrate: str = "64k",
    filters: str = "",
) -> None:
    """Transcode a WAV file to the given format.

    - fmt: "opus", "mp3", or "wav". WAV output still re-runs through ffmpeg
      so the filter chain is applied; set filters="" if you want a bit-exact
      passthrough... actually that only copies the stream:
          for true passthrough use fmt="wav", filters=""; we still re-encode
          to PCM s16le but that's equivalent on the vast majority of inputs.
    - bitrate: ignored for WAV, otherwise passed as ffmpeg -b:a.
    - filters: ffmpeg -af filter chain. Empty string = no filtering.

    Uses a single ffmpeg thread to keep the recorder's CPU share safe.
    """
    fmt_key = (fmt or "").lower()
    if fmt_key not in _CODECS:
        raise EncodeError(f"Unknown upload format: {fmt!r} (use opus, mp3, or wav)")

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
    cmd += _CODECS[fmt_key][1]
    # Bitrate only makes sense for lossy codecs.
    if fmt_key in ("opus", "mp3"):
        cmd += ["-b:a", bitrate]
    cmd += [str(dst)]

    log.debug("encode: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise EncodeError(f"ffmpeg failed ({result.returncode}): {stderr}")


# Backwards-compatible shim so anything importing the old name still works.
def wav_to_opus(src: Path, dst: Path, bitrate: str, filters: str = "") -> None:
    encode_audio(src, dst, fmt="opus", bitrate=bitrate, filters=filters)
