"""SQLite helpers shared by recorder, uploader, and webapp.

Schema is intentionally tiny: a single `recordings` table tracks every
recording from creation through upload. Every service opens its own
connection; SQLite handles concurrent access fine for this workload.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


STATUS_RECORDING = "recording"
STATUS_PENDING_UPLOAD = "pending_upload"
STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"


SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    duration_s   REAL,
    size_bytes   INTEGER,
    status       TEXT NOT NULL,
    cloud_key    TEXT,
    error        TEXT,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    uploaded_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_recordings_status ON recordings(status);
CREATE INDEX IF NOT EXISTS idx_recordings_started_at ON recordings(started_at);
"""


@dataclass
class Recording:
    id: str
    filename: str
    started_at: datetime
    ended_at: Optional[datetime]
    duration_s: Optional[float]
    size_bytes: Optional[int]
    status: str
    cloud_key: Optional[str]
    error: Optional[str]
    retry_count: int
    uploaded_at: Optional[datetime]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Recording":
        return cls(
            id=row["id"],
            filename=row["filename"],
            started_at=_parse_dt(row["started_at"]),
            ended_at=_parse_dt(row["ended_at"]),
            duration_s=row["duration_s"],
            size_bytes=row["size_bytes"],
            status=row["status"],
            cloud_key=row["cloud_key"],
            error=row["error"],
            retry_count=row["retry_count"],
            uploaded_at=_parse_dt(row["uploaded_at"]),
        )


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sensible defaults for embedded use."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def create_recording(conn: sqlite3.Connection, filename: str) -> Recording:
    """Insert a new row with status=recording and return it."""
    rec_id = uuid.uuid4().hex
    started = _now_iso()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO recordings (id, filename, started_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (rec_id, filename, started, STATUS_RECORDING),
        )
    return get_recording(conn, rec_id)


def finish_recording(
    conn: sqlite3.Connection,
    rec_id: str,
    duration_s: float,
    size_bytes: int,
) -> None:
    """Mark recording as finished and ready to upload."""
    ended = _now_iso()
    with transaction(conn):
        conn.execute(
            """
            UPDATE recordings
               SET ended_at = ?, duration_s = ?, size_bytes = ?, status = ?
             WHERE id = ?
            """,
            (ended, duration_s, size_bytes, STATUS_PENDING_UPLOAD, rec_id),
        )


def mark_recording_orphaned(conn: sqlite3.Connection, rec_id: str, error: str) -> None:
    """Called on startup for rows stuck in `recording` after a crash."""
    with transaction(conn):
        conn.execute(
            "UPDATE recordings SET status = ?, error = ? WHERE id = ? AND status = ?",
            (STATUS_PENDING_UPLOAD, error, rec_id, STATUS_RECORDING),
        )


def reset_orphaned_on_startup(conn: sqlite3.Connection) -> int:
    """
    If the recorder crashed mid-recording, a row may be stuck in 'recording'.
    On startup we flip any such rows to 'pending_upload' so the uploader can
    still try to salvage the WAV (arecord writes incrementally, so most of
    the data is there even without a proper header finalize).
    """
    cur = conn.execute(
        "UPDATE recordings SET status = ?, error = ? WHERE status = ?",
        (STATUS_PENDING_UPLOAD, "recorder crashed before clean stop", STATUS_RECORDING),
    )
    return cur.rowcount


def claim_next_pending(conn: sqlite3.Connection) -> Optional[Recording]:
    """Atomically grab the oldest pending_upload row and mark it uploading."""
    with transaction(conn):
        row = conn.execute(
            """
            SELECT * FROM recordings
             WHERE status = ?
             ORDER BY started_at ASC
             LIMIT 1
            """,
            (STATUS_PENDING_UPLOAD,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE recordings SET status = ? WHERE id = ?",
            (STATUS_UPLOADING, row["id"]),
        )
    return get_recording(conn, row["id"])


def mark_uploaded(conn: sqlite3.Connection, rec_id: str, cloud_key: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            UPDATE recordings
               SET status = ?, cloud_key = ?, uploaded_at = ?, error = NULL
             WHERE id = ?
            """,
            (STATUS_UPLOADED, cloud_key, _now_iso(), rec_id),
        )


def mark_upload_failed(
    conn: sqlite3.Connection,
    rec_id: str,
    error: str,
    give_up: bool,
) -> None:
    """Record a failure. If give_up=True go to STATUS_FAILED, else back to pending for retry."""
    new_status = STATUS_FAILED if give_up else STATUS_PENDING_UPLOAD
    with transaction(conn):
        conn.execute(
            """
            UPDATE recordings
               SET status = ?, error = ?, retry_count = retry_count + 1
             WHERE id = ?
            """,
            (new_status, error, rec_id),
        )


def get_recording(conn: sqlite3.Connection, rec_id: str) -> Recording:
    row = conn.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    if row is None:
        raise KeyError(rec_id)
    return Recording.from_row(row)


def list_recordings(
    conn: sqlite3.Connection,
    limit: int = 50,
    offset: int = 0,
) -> list[Recording]:
    rows = conn.execute(
        """
        SELECT * FROM recordings
         ORDER BY started_at DESC
         LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [Recording.from_row(r) for r in rows]


def count_recordings(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM recordings").fetchone()
    return row["c"]


def count_by_status(conn: sqlite3.Connection, status: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM recordings WHERE status = ?", (status,)
    ).fetchone()
    return row["c"]


def current_recording(conn: sqlite3.Connection) -> Optional[Recording]:
    row = conn.execute(
        "SELECT * FROM recordings WHERE status = ? ORDER BY started_at DESC LIMIT 1",
        (STATUS_RECORDING,),
    ).fetchone()
    return Recording.from_row(row) if row else None


def last_uploaded(conn: sqlite3.Connection) -> Optional[Recording]:
    row = conn.execute(
        """
        SELECT * FROM recordings
         WHERE status = ? AND uploaded_at IS NOT NULL
         ORDER BY uploaded_at DESC
         LIMIT 1
        """,
        (STATUS_UPLOADED,),
    ).fetchone()
    return Recording.from_row(row) if row else None


def old_uploaded_to_prune(
    conn: sqlite3.Connection,
    older_than_days: int,
) -> list[Recording]:
    """Uploaded rows whose local WAV is older than the retention window."""
    if older_than_days <= 0:
        return []
    rows = conn.execute(
        """
        SELECT * FROM recordings
         WHERE status = ?
           AND uploaded_at IS NOT NULL
           AND uploaded_at < datetime('now', ?)
        """,
        (STATUS_UPLOADED, f"-{older_than_days} days"),
    ).fetchall()
    return [Recording.from_row(r) for r in rows]


def delete_recording(conn: sqlite3.Connection, rec_id: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
