"""Typed config loaded from /etc/audiorec/config.toml.

This module deliberately tolerates *partial* configs: a brand-new install has
no mic picked, no cloud creds, and no admin password until the user completes
the web setup wizard. The recorder, uploader, and webapp each use the
`is_*_configured` helpers to decide whether to run normally or idle until
the config is filled in.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w


log = logging.getLogger("audiorec.config")

DEFAULT_CONFIG_PATH = Path(os.environ.get("AUDIOREC_CONFIG", "/etc/audiorec/config.toml"))


@dataclass
class MetaConfig:
    setup_complete: bool = False


@dataclass
class PathsConfig:
    data_dir: Path = Path("/var/lib/audiorec")
    recordings_subdir: str = "recordings"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / self.recordings_subdir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "recordings.db"


@dataclass
class AudioConfig:
    device: str = ""
    sample_rate: int = 16000
    channels: int = 1
    format: str = "S16_LE"


@dataclass
class GpioConfig:
    button_pin: int = 17
    led_pin: int = 18
    debounce_s: float = 0.05
    long_press_s: float = 3.0


@dataclass
class UploadConfig:
    poll_interval_s: int = 5
    local_retention_days: int = 7
    # Output format for files uploaded to the cloud.
    #   "mp3"  - universally playable, small (default). 64k = decent voice.
    #   "opus" - best quality/size for voice but QuickTime/Windows can't play it.
    #   "wav"  - lossless but huge (~10 MB/min at 16 kHz mono).
    format: str = "mp3"
    # Bitrate for lossy formats (opus, mp3). Ignored for wav.
    bitrate: str = "64k"
    # Legacy field; still honored if `bitrate` is unset so old configs keep working.
    opus_bitrate: str = "32k"
    multipart_part_size_mb: int = 5
    max_retries: int = 10
    # ffmpeg audio filter chain applied before encoding. Empty string = off.
    # Defaults clean up the typical Pi Zero 2 W + USB mic interference:
    # mains hum, ultrasonic switching whine, and constant hiss.
    audio_filters: str = "highpass=f=80,lowpass=f=8000,afftdn=nr=12"


@dataclass
class CloudConfig:
    """S3-compatible cloud storage (Backblaze B2, Cloudflare R2, Wasabi, DO, ...)."""
    provider: str = ""
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str = ""
    region: str = ""
    bucket: str = ""
    key_prefix: str = "recordings/"


WasabiConfig = CloudConfig


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 80
    username: str = ""
    password_hash: str = ""
    session_secret: str = ""
    session_lifetime_s: int = 2592000
    hostname: str = "audiorec.local"


@dataclass
class Config:
    meta: MetaConfig = field(default_factory=MetaConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    gpio: GpioConfig = field(default_factory=GpioConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def wasabi(self) -> CloudConfig:
        return self.cloud


def load_config(path: Path | None = None) -> Config:
    """Parse the TOML file into a Config. Missing file -> all defaults."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        log.warning("Config %s missing; using defaults (setup not complete).", cfg_path)
        return Config()
    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)
    return _parse(data)


def _filter(klass, data: dict[str, Any]) -> dict[str, Any]:
    fields = klass.__dataclass_fields__
    return {k: v for k, v in data.items() if k in fields}


def _parse(data: dict) -> Config:
    cfg = Config()
    if isinstance(data.get("meta"), dict):
        cfg.meta = MetaConfig(**_filter(MetaConfig, data["meta"]))
    if isinstance(data.get("paths"), dict):
        p = data["paths"]
        cfg.paths = PathsConfig(
            data_dir=Path(p.get("data_dir", cfg.paths.data_dir)),
            recordings_subdir=p.get("recordings_subdir", cfg.paths.recordings_subdir),
        )
    for section_name, klass in [
        ("audio", AudioConfig),
        ("gpio", GpioConfig),
        ("upload", UploadConfig),
        ("web", WebConfig),
    ]:
        section = data.get(section_name)
        if isinstance(section, dict):
            current = getattr(cfg, section_name)
            merged = {**current.__dict__, **_filter(klass, section)}
            setattr(cfg, section_name, klass(**merged))

    # Accept both the new [cloud] name and the legacy [wasabi] for existing installs.
    cloud_section = data.get("cloud") or data.get("wasabi")
    if isinstance(cloud_section, dict):
        merged = {**cfg.cloud.__dict__, **_filter(CloudConfig, cloud_section)}
        cfg.cloud = CloudConfig(**merged)

    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Atomically persist `cfg` to disk (write to tmp + rename)."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    data = _serialize(cfg)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(data, f)
    try:
        os.chmod(tmp, 0o640)
    except PermissionError:
        pass
    os.replace(tmp, cfg_path)
    log.info("Wrote config %s", cfg_path)


def _serialize(cfg: Config) -> dict:
    return {
        "meta": {"setup_complete": cfg.meta.setup_complete},
        "paths": {
            "data_dir": str(cfg.paths.data_dir),
            "recordings_subdir": cfg.paths.recordings_subdir,
        },
        "audio": {
            "device": cfg.audio.device,
            "sample_rate": cfg.audio.sample_rate,
            "channels": cfg.audio.channels,
            "format": cfg.audio.format,
        },
        "gpio": {
            "button_pin": cfg.gpio.button_pin,
            "led_pin": cfg.gpio.led_pin,
            "debounce_s": cfg.gpio.debounce_s,
            "long_press_s": cfg.gpio.long_press_s,
        },
        "upload": {
            "poll_interval_s": cfg.upload.poll_interval_s,
            "local_retention_days": cfg.upload.local_retention_days,
            "format": cfg.upload.format,
            "bitrate": cfg.upload.bitrate,
            "opus_bitrate": cfg.upload.opus_bitrate,
            "multipart_part_size_mb": cfg.upload.multipart_part_size_mb,
            "max_retries": cfg.upload.max_retries,
            "audio_filters": cfg.upload.audio_filters,
        },
        "cloud": {
            "provider": cfg.cloud.provider,
            "access_key": cfg.cloud.access_key,
            "secret_key": cfg.cloud.secret_key,
            "endpoint_url": cfg.cloud.endpoint_url,
            "region": cfg.cloud.region,
            "bucket": cfg.cloud.bucket,
            "key_prefix": cfg.cloud.key_prefix,
        },
        "web": {
            "host": cfg.web.host,
            "port": cfg.web.port,
            "username": cfg.web.username,
            "password_hash": cfg.web.password_hash,
            "session_secret": cfg.web.session_secret,
            "session_lifetime_s": cfg.web.session_lifetime_s,
            "hostname": cfg.web.hostname,
        },
    }


def is_audio_configured(cfg: Config) -> bool:
    return bool(cfg.audio.device)


def is_cloud_configured(cfg: Config) -> bool:
    c = cfg.cloud
    return bool(c.access_key and c.secret_key and c.endpoint_url and c.bucket)


def is_admin_set(cfg: Config) -> bool:
    return bool(cfg.web.username and cfg.web.password_hash and cfg.web.session_secret)


def is_fully_configured(cfg: Config) -> bool:
    return (
        cfg.meta.setup_complete
        and is_admin_set(cfg)
        and is_audio_configured(cfg)
        and is_cloud_configured(cfg)
    )
