"""Wasabi (S3-compatible) client wrapper with low-memory multipart upload."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from common.config import WasabiConfig


log = logging.getLogger("audiorec.uploader.wasabi")


class WasabiClient:
    def __init__(self, cfg: WasabiConfig, part_size_mb: int) -> None:
        self.cfg = cfg
        self.part_size_mb = part_size_mb
        self._client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            region_name=cfg.region,
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    def _key(self, filename: str) -> str:
        prefix = self.cfg.key_prefix or ""
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return f"{prefix}{filename}"

    def upload(self, src: Path, filename: str) -> str:
        """Upload `src` to the bucket and return the resulting key.

        Uses multipart upload with a small part size so RAM stays low on the Pi.
        boto3's TransferManager streams parts from disk and handles retries.
        """
        key = self._key(filename)
        part_size = self.part_size_mb * 1024 * 1024
        config = TransferConfig(
            multipart_threshold=part_size,
            multipart_chunksize=part_size,
            max_concurrency=1,           # one part at a time -> tiny RAM footprint
            use_threads=False,           # avoid thread-pool overhead on Pi Zero
        )
        extra_args = {"ContentType": _content_type_for(filename)}
        log.info("Uploading %s -> s3://%s/%s", src, self.cfg.bucket, key)
        try:
            self._client.upload_file(
                Filename=str(src),
                Bucket=self.cfg.bucket,
                Key=key,
                ExtraArgs=extra_args,
                Config=config,
            )
        except (BotoCoreError, ClientError) as e:
            raise UploadError(str(e)) from e
        return key

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.cfg.bucket, Key=key)
        except (BotoCoreError, ClientError) as e:
            raise UploadError(str(e)) from e

    def presign_get(self, key: str, expires_s: int = 300) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.cfg.bucket, "Key": key},
            ExpiresIn=expires_s,
        )


class UploadError(RuntimeError):
    pass


def _content_type_for(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".opus"):
        return "audio/ogg"
    if lower.endswith(".flac"):
        return "audio/flac"
    if lower.endswith(".wav"):
        return "audio/wav"
    return "application/octet-stream"
