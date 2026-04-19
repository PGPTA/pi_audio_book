"""Uploader service: drains pending recordings to Wasabi.

Loop (every `poll_interval_s`):
  1. Atomically claim the oldest pending_upload row -> mark uploading.
  2. Encode WAV -> Opus in a temp file.
  3. Multipart-upload the Opus file to Wasabi.
  4. Mark uploaded (or failed with backoff).
  5. Prune local WAVs older than the retention window.

Never blocks the recorder; runs with Nice=10 / IOSchedulingClass=idle via systemd.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from common import db
from common.config import Config, is_cloud_configured, load_config

from .encode import EncodeError, wav_to_opus
from .wasabi import UploadError, WasabiClient


log = logging.getLogger("audiorec.uploader")


_stop = False


def _graceful(_signum, _frame):
    global _stop
    _stop = True
    log.info("Uploader shutdown requested")


def _backoff_seconds(retry_count: int) -> float:
    """Exponential backoff capped at ~5 minutes."""
    return min(300.0, 2 ** retry_count)


def _process_one(
    cfg: Config,
    conn,
    wasabi: WasabiClient,
) -> bool:
    """Claim and process one recording. Returns True if something was done."""
    rec = db.claim_next_pending(conn)
    if rec is None:
        return False

    src_wav = cfg.paths.recordings_dir / rec.filename
    if not src_wav.exists():
        log.error("Source WAV missing for %s: %s", rec.id, src_wav)
        db.mark_upload_failed(conn, rec.id, f"missing file: {src_wav}", give_up=True)
        return True

    if rec.retry_count > 0:
        wait = _backoff_seconds(rec.retry_count)
        log.info("Backoff %.0fs before retry %d of %s", wait, rec.retry_count, rec.id)
        # Sleep in small chunks so SIGTERM is still responsive.
        slept = 0.0
        while slept < wait and not _stop:
            time.sleep(min(1.0, wait - slept))
            slept += 1.0
        if _stop:
            # Return the row to the queue so another invocation can pick it up.
            db.mark_upload_failed(conn, rec.id, "interrupted during backoff", give_up=False)
            return True

    opus_name = Path(rec.filename).with_suffix(".opus").name
    try:
        with tempfile.TemporaryDirectory(prefix="audiorec-enc-") as tmp:
            opus_path = Path(tmp) / opus_name
            log.info("Encoding %s -> %s", src_wav.name, opus_path.name)
            wav_to_opus(
                src_wav,
                opus_path,
                bitrate=cfg.upload.opus_bitrate,
                filters=cfg.upload.audio_filters,
            )
            cloud_key = wasabi.upload(opus_path, opus_name)
    except EncodeError as e:
        log.exception("Encode failed for %s", rec.id)
        give_up = rec.retry_count + 1 >= cfg.upload.max_retries
        db.mark_upload_failed(conn, rec.id, f"encode: {e}", give_up=give_up)
        return True
    except UploadError as e:
        log.warning("Upload failed for %s: %s", rec.id, e)
        give_up = rec.retry_count + 1 >= cfg.upload.max_retries
        db.mark_upload_failed(conn, rec.id, f"upload: {e}", give_up=give_up)
        return True
    except Exception as e:  # pragma: no cover - defensive
        log.exception("Unexpected error processing %s", rec.id)
        db.mark_upload_failed(conn, rec.id, f"unexpected: {e}", give_up=False)
        return True

    db.mark_uploaded(conn, rec.id, cloud_key)
    log.info("Uploaded %s (key=%s)", rec.id, cloud_key)
    return True


def _prune_old(cfg: Config, conn) -> None:
    """Delete local WAVs for recordings uploaded longer ago than retention window."""
    if cfg.upload.local_retention_days <= 0:
        return
    old = db.old_uploaded_to_prune(conn, cfg.upload.local_retention_days)
    for rec in old:
        path = cfg.paths.recordings_dir / rec.filename
        try:
            if path.exists():
                path.unlink()
                log.info("Pruned local WAV for %s (%s)", rec.id, path.name)
        except OSError as e:
            log.warning("Failed to prune %s: %s", path, e)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("AUDIOREC_LOG", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    cfg = load_config()

    if not is_cloud_configured(cfg):
        log.warning(
            "Cloud storage not configured. Idling until setup wizard "
            "(http://%s/setup) finishes.",
            cfg.web.hostname or "audiorec.local",
        )
        while not _stop:
            time.sleep(1.0)
        return 0

    conn = db.connect(cfg.paths.db_path)
    wasabi = WasabiClient(cfg.cloud, part_size_mb=cfg.upload.multipart_part_size_mb)

    log.info(
        "Uploader ready. bucket=%s prefix=%s poll=%ds",
        cfg.cloud.bucket,
        cfg.cloud.key_prefix,
        cfg.upload.poll_interval_s,
    )

    last_prune = 0.0
    while not _stop:
        try:
            did_work = _process_one(cfg, conn, wasabi)
        except Exception:
            log.exception("Unhandled error in uploader loop")
            did_work = False

        now = time.monotonic()
        if now - last_prune > 3600:  # prune at most once per hour
            try:
                _prune_old(cfg, conn)
            except Exception:
                log.exception("Prune failed")
            last_prune = now

        if not did_work:
            # Sleep poll_interval_s in small chunks so shutdown is snappy.
            slept = 0.0
            while slept < cfg.upload.poll_interval_s and not _stop:
                time.sleep(0.5)
                slept += 0.5

    conn.close()
    log.info("Uploader stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
