"""Loads /etc/audiorec/config.toml into a typed object shared by all services."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_CONFIG_PATH = Path(os.environ.get("AUDIOREC_CONFIG", "/etc/audiorec/config.toml"))


@dataclass(frozen=True)
class PathsConfig:
    data_dir: Path
    recordings_subdir: str

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / self.recordings_subdir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "recordings.db"


@dataclass(frozen=True)
class AudioConfig:
    device: str
    sample_rate: int
    channels: int
    format: str


@dataclass(frozen=True)
class GpioConfig:
    button_pin: int
    led_pin: int
    debounce_s: float
    long_press_s: float


@dataclass(frozen=True)
class UploadConfig:
    poll_interval_s: int
    local_retention_days: int
    opus_bitrate: str
    multipart_part_size_mb: int
    max_retries: int


@dataclass(frozen=True)
class WasabiConfig:
    access_key: str
    secret_key: str
    endpoint_url: str
    region: str
    bucket: str
    key_prefix: str


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    username: str
    password_hash: str
    session_secret: str
    session_lifetime_s: int
    hostname: str


@dataclass(frozen=True)
class Config:
    paths: PathsConfig
    audio: AudioConfig
    gpio: GpioConfig
    upload: UploadConfig
    wasabi: WasabiConfig
    web: WebConfig


def load_config(path: Path | None = None) -> Config:
    """Parse the TOML config file into a Config dataclass."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)

    paths = PathsConfig(
        data_dir=Path(data["paths"]["data_dir"]),
        recordings_subdir=data["paths"]["recordings_subdir"],
    )
    audio = AudioConfig(**data["audio"])
    gpio = GpioConfig(**data["gpio"])
    upload = UploadConfig(**data["upload"])
    wasabi = WasabiConfig(**data["wasabi"])
    web = WebConfig(**data["web"])
    return Config(paths=paths, audio=audio, gpio=gpio, upload=upload, wasabi=wasabi, web=web)
