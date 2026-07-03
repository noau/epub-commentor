"""SQLite state machine + DAO for the daemon job queue.

The schema (see ``daemon-prd.md`` §3) covers four concerns:

* ``jobs`` — the task table. Single source of truth for state.
* ``events`` — append-only audit log per job (enqueued / started / …).
* ``control_signals`` — out-of-band commands from ``epubctl`` (cancel,
  pause, resume) that the worker reads between chapter iterations.
* ``server_stats`` — sampled health metrics for ``epubctl health``.

WAL is enabled so ``epubctl`` can SELECT while the worker is INSERTing.
A short busy_timeout (5 s) hides the "database is locked" race that
otherwise surfaces when ``epubctl submit`` and the worker both write
at the same instant.

DAO functions are pure: they accept a :class:`sqlite3.Connection` so
the worker and the CLI can share their own connection and ``PRAGMA``
state stays predictable. The :func:`connect` helper centralises WAL +
busy_timeout + row_factory setup.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Job lifecycle states. The 6-state machine matches daemon-prd.md §3.
STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_PAUSED = "PAUSED"
STATUS_CANCELLED = "CANCELLED"

# Lifecycle event kinds (events table). Kept as module constants so
# tests can reference the same strings the worker writes.
EVENT_ENQUEUED = "enqueued"
EVENT_STARTED = "started"
EVENT_FINISHED = "finished"
EVENT_FAILED = "failed"
EVENT_CANCELLED = "cancelled"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"
EVENT_RESTARTED = "restarted"
EVENT_DISK_LOW = "disk_low"
EVENT_DISK_RECOVERED = "disk_recovered"

# Control signal kinds.
SIGNAL_CANCEL = "cancel"

_TERMINAL_STATUSES = frozenset({STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED})

_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name          TEXT    NOT NULL,
    source_path        TEXT    NOT NULL,
    output_path        TEXT,
    status             TEXT    NOT NULL DEFAULT 'PENDING',
    priority           INTEGER NOT NULL DEFAULT 0,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    max_retries        INTEGER NOT NULL DEFAULT 3,
    error_message      TEXT,
    error_stage        TEXT,
    book_synopsis      TEXT,
    flags_json         TEXT    NOT NULL DEFAULT '{}',
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    started_at         TEXT,
    finished_at        TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_tokens       INTEGER NOT NULL DEFAULT 0,
    chapters_processed INTEGER NOT NULL DEFAULT 0,
    chapters_skipped   INTEGER NOT NULL DEFAULT 0,
    total_comments     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts      TEXT    NOT NULL DEFAULT (datetime('now')),
    kind    TEXT    NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, ts);

CREATE TABLE IF NOT EXISTS control_signals (
    job_id  INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    kind    TEXT    NOT NULL,
    sent_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS server_stats (
    ts                TEXT    PRIMARY KEY,
    cpu_percent       REAL,
    mem_percent       REAL,
    disk_used_percent REAL,
    disk_available_gb REAL,
    pending_jobs      INTEGER,
    processing_jobs   INTEGER
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL, busy_timeout, and row factory configured.

    Always opens with ``isolation_level=None`` (autocommit-style) so the
    DAO helpers can run ``BEGIN`` themselves when they need a multi-
    statement transaction. WAL is set via ``PRAGMA`` rather than URL
    flags so the same code path works on Windows (no Unix socket magic).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL lets ``epubctl`` SELECT while the worker is INSERTing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables / indexes if they don't already exist.

    Safe to call repeatedly — every CREATE uses ``IF NOT EXISTS``.
    """
    conn.executescript(_SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Domain dataclass
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """In-memory view of one ``jobs`` row.

    The DAO returns these from SELECTs and accepts them (or the relevant
    subset) as INSERT / UPDATE inputs. ``flags_json`` is decoded once
    on read so callers don't have to ``json.loads`` it themselves; on
    write it's serialised back so the schema stays plain text.
    """

    id: int
    file_name: str
    source_path: str
    status: str
    priority: int
    retry_count: int
    max_retries: int
    flags: dict[str, Any] = field(default_factory=dict)
    output_path: str | None = None
    error_message: str | None = None
    error_stage: str | None = None
    book_synopsis: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    chapters_processed: int = 0
    chapters_skipped: int = 0
    total_comments: int = 0


def _row_to_job(row: sqlite3.Row) -> Job:
    """Convert a ``SELECT * FROM jobs`` row into a :class:`Job`."""
    raw_flags = row["flags_json"] or "{}"
    try:
        flags = json.loads(raw_flags)
    except json.JSONDecodeError:
        flags = {}
    return Job(
        id=row["id"],
        file_name=row["file_name"],
        source_path=row["source_path"],
        status=row["status"],
        priority=row["priority"],
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        flags=flags,
        output_path=row["output_path"],
        error_message=row["error_message"],
        error_stage=row["error_stage"],
        book_synopsis=row["book_synopsis"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_tokens=row["cache_tokens"],
        chapters_processed=row["chapters_processed"],
        chapters_skipped=row["chapters_skipped"],
        total_comments=row["total_comments"],
    )


# ---------------------------------------------------------------------------
# Submit / fetch
# ---------------------------------------------------------------------------


def insert_job(
    conn: sqlite3.Connection,
    *,
    file_name: str,
    source_path: str,
    priority: int = 0,
    book_synopsis: str | None = None,
    flags: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> int:
    """Insert a new PENDING job and return its id.

    ``flags`` is stored as JSON. ``book_synopsis`` is optional — the
    pipeline uses it to bias the Stage 1 prompt.
    """
    flags_json = json.dumps(flags or {}, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO jobs (file_name, source_path, priority, book_synopsis, flags_json, max_retries)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (file_name, source_path, priority, book_synopsis, flags_json, max_retries),
    )
    job_id = int(cur.lastrowid or 0)
    record_event(conn, job_id, EVENT_ENQUEUED, detail=None)
    return job_id


def fetch_next_pending(conn: sqlite3.Connection) -> Job | None:
    """Pick the highest-priority PENDING job (ties broken by created_at).

    Returns ``None`` when the queue is empty. ``priority DESC, created_at``
    matches the ``idx_jobs_queue`` index, so the query is O(log N).
    """
    row = conn.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'PENDING'
        ORDER BY priority DESC, created_at ASC
        LIMIT 1
        """,
    ).fetchone()
    return _row_to_job(row) if row is not None else None


def fetch_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    """Fetch a single job by id; ``None`` if missing."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row is not None else None


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[Job]:
    """List jobs ordered by creation time descending."""
    sql = "SELECT * FROM jobs"
    params: list[Any] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_job(r) for r in rows]


def queue_depths(conn: sqlite3.Connection) -> dict[str, int]:
    """Return a {status: count} snapshot for ``epubctl status``."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status",
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def mark_processing(conn: sqlite3.Connection, job_id: int) -> None:
    """Transition PENDING/PAUSED → PROCESSING.

    No-op when the job is already PROCESSING (idempotent retry) or in a
    terminal state (worker should never call this for a terminal job).
    Records the ``started`` event.
    """
    now = _utcnow()
    conn.execute(
        """
        UPDATE jobs
        SET status = 'PROCESSING',
            started_at = COALESCE(started_at, ?),
            updated_at  = ?,
            error_stage = NULL
        WHERE id = ? AND status IN ('PENDING', 'PAUSED', 'PROCESSING')
        """,
        (now, now, job_id),
    )
    record_event(conn, job_id, EVENT_STARTED)


def mark_success(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    output_path: str,
    input_tokens: int,
    output_tokens: int,
    cache_tokens: int,
    chapters_processed: int,
    chapters_skipped: int,
    total_comments: int,
) -> None:
    """Transition PROCESSING → SUCCESS with the result summary."""
    now = _utcnow()
    conn.execute(
        """
        UPDATE jobs SET
            status = 'SUCCESS',
            output_path = ?,
            finished_at = ?,
            updated_at = ?,
            input_tokens = ?,
            output_tokens = ?,
            cache_tokens = ?,
            chapters_processed = ?,
            chapters_skipped = ?,
            total_comments = ?,
            error_message = NULL,
            error_stage  = NULL
        WHERE id = ?
        """,
        (
            output_path,
            now,
            now,
            input_tokens,
            output_tokens,
            cache_tokens,
            chapters_processed,
            chapters_skipped,
            total_comments,
            job_id,
        ),
    )
    record_event(conn, job_id, EVENT_FINISHED, detail=output_path)


def mark_failed(conn: sqlite3.Connection, job_id: int, *, stage: str, message: str) -> None:
    """Transition PROCESSING → FAILED (terminal)."""
    now = _utcnow()
    conn.execute(
        """
        UPDATE jobs SET
            status = 'FAILED',
            finished_at = ?,
            updated_at  = ?,
            error_stage  = ?,
            error_message = ?
        WHERE id = ?
        """,
        (now, now, stage, message, job_id),
    )
    record_event(conn, job_id, EVENT_FAILED, detail=f"{stage}: {message}")


def mark_cancelled(conn: sqlite3.Connection, job_id: int) -> None:
    """Transition PROCESSING → CANCELLED (terminal)."""
    now = _utcnow()
    conn.execute(
        """
        UPDATE jobs SET
            status = 'CANCELLED',
            finished_at = ?,
            updated_at  = ?,
            error_stage = 'cancelled'
        WHERE id = ?
        """,
        (now, now, job_id),
    )
    record_event(conn, job_id, EVENT_CANCELLED)


def increment_retry(conn: sqlite3.Connection, job_id: int) -> bool:
    """Bump retry_count and reset status to PENDING if under max_retries.

    Returns ``True`` if the retry was scheduled (and the caller should
    hand the job back to the queue), ``False`` if the budget is
    exhausted (caller should mark FAILED instead).
    """
    now = _utcnow()
    cur = conn.execute(
        """
        UPDATE jobs SET
            status = 'PENDING',
            updated_at = ?,
            retry_count = retry_count + 1,
            error_stage = NULL,
            error_message = NULL
        WHERE id = ? AND retry_count < max_retries
        """,
        (now, job_id),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Pause / resume (manual + circuit breaker)
# ---------------------------------------------------------------------------


def set_priority(conn: sqlite3.Connection, job_id: int, priority: int) -> bool:
    """Update the job's priority. Returns True iff the row existed."""
    cur = conn.execute(
        "UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ?",
        (priority, _utcnow(), job_id),
    )
    return cur.rowcount > 0


def pause(conn: sqlite3.Connection, job_id: int) -> bool:
    """Transition a single PENDING job to PAUSED."""
    now = _utcnow()
    cur = conn.execute(
        "UPDATE jobs SET status='PAUSED', updated_at=? WHERE id = ? AND status='PENDING'",
        (now, job_id),
    )
    if cur.rowcount > 0:
        record_event(conn, job_id, EVENT_PAUSED, detail="manual")
    return cur.rowcount > 0


def resume(conn: sqlite3.Connection, job_id: int) -> bool:
    """Transition a PAUSED / FAILED job back to PENDING.

    Resuming a FAILED job increments retry_count (so the operator can
    tell at a glance how many times it's been re-queued).
    """
    now = _utcnow()
    cur = conn.execute(
        """
        UPDATE jobs SET
            status = 'PENDING',
            updated_at = ?,
            retry_count = CASE WHEN status='FAILED' THEN retry_count + 1 ELSE retry_count END,
            error_stage = NULL,
            error_message = NULL
        WHERE id = ? AND status IN ('PAUSED', 'FAILED')
        """,
        (now, job_id),
    )
    if cur.rowcount > 0:
        record_event(conn, job_id, EVENT_RESUMED, detail="manual")
    return cur.rowcount > 0


def pause_all_non_terminal(conn: sqlite3.Connection, *, reason: str) -> int:
    """Bulk PAUSE every non-terminal job. Returns the number of rows touched."""
    now = _utcnow()
    cur = conn.execute(
        """
        UPDATE jobs SET status='PAUSED', updated_at=?
        WHERE status IN ('PENDING', 'PROCESSING')
        """,
        (now,),
    )
    if cur.rowcount > 0:
        # Bulk event so ``epubctl show <id>`` shows disk_pause provenance.
        conn.execute(
            """
            INSERT INTO events (job_id, kind, detail)
            SELECT id, 'paused', ? FROM jobs WHERE status = 'PAUSED' AND updated_at = ?
            """,
            (f"bulk: {reason}", now),
        )
    return cur.rowcount


def resume_all_paused(conn: sqlite3.Connection) -> int:
    """Bulk resume PAUSED → PENDING."""
    now = _utcnow()
    cur = conn.execute(
        """
        UPDATE jobs SET status='PENDING', updated_at=?
        WHERE status='PAUSED'
        """,
        (now,),
    )
    if cur.rowcount > 0:
        conn.execute(
            """
            INSERT INTO events (job_id, kind, detail)
            SELECT id, 'resumed', 'bulk' FROM jobs
            WHERE status='PENDING' AND updated_at=?
            """,
            (now,),
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Control signals (epubctl cancel)
# ---------------------------------------------------------------------------


def send_control_signal(conn: sqlite3.Connection, job_id: int, kind: str) -> bool:
    """Insert a control signal. Idempotent — re-sending same kind is no-op."""
    conn.execute(
        """
        INSERT INTO control_signals (job_id, kind) VALUES (?, ?)
        ON CONFLICT(job_id) DO UPDATE SET kind = excluded.kind, sent_at = datetime('now')
        """,
        (job_id, kind),
    )
    return True


def fetch_control_signals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Read and clear all pending control signals.

    Worker calls this once per iteration. The DELETE happens after the
    SELECT inside a single transaction so a crash mid-fetch doesn't lose
    a cancel request.
    """
    with _transaction(conn):
        rows = conn.execute("SELECT job_id, kind FROM control_signals").fetchall()
        if rows:
            conn.execute("DELETE FROM control_signals")
        return rows


# ---------------------------------------------------------------------------
# Events + stats
# ---------------------------------------------------------------------------


def record_event(conn: sqlite3.Connection, job_id: int, kind: str, detail: str | None = None) -> None:
    """Append a row to ``events``. Cheap — no transaction needed."""
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, ?, ?)",
        (job_id, kind, detail),
    )


def list_events(conn: sqlite3.Connection, job_id: int, *, limit: int = 100) -> list[sqlite3.Row]:
    """Return up to ``limit`` events for ``job_id`` ordered by time ascending."""
    return conn.execute(
        "SELECT id, ts, kind, detail FROM events WHERE job_id = ? ORDER BY ts ASC, id ASC LIMIT ?",
        (job_id, limit),
    ).fetchall()


def record_stat(
    conn: sqlite3.Connection,
    *,
    cpu_percent: float | None,
    mem_percent: float | None,
    disk_used_percent: float | None,
    disk_available_gb: float | None,
    pending_jobs: int,
    processing_jobs: int,
) -> None:
    """Sample server health metrics. ``ts`` defaults to ``datetime('now')``."""
    conn.execute(
        """
        INSERT INTO server_stats (ts, cpu_percent, mem_percent, disk_used_percent,
                                  disk_available_gb, pending_jobs, processing_jobs)
        VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)
        """,
        (cpu_percent, mem_percent, disk_used_percent, disk_available_gb, pending_jobs, processing_jobs),
    )


def fetch_latest_stat(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Most recent server_stats row, or ``None`` if never sampled."""
    return conn.execute(
        "SELECT * FROM server_stats ORDER BY ts DESC LIMIT 1",
    ).fetchone()


# ---------------------------------------------------------------------------
# Crash recovery + prune
# ---------------------------------------------------------------------------


def recover_crashed_jobs(conn: sqlite3.Connection) -> int:
    """Reset PROCESSING jobs back to PENDING on daemon startup.

    Called once when the daemon comes up. Rows that are mid-flight in
    another (dead) process become PENDING so the new worker can pick
    them up. A row-level ``restarted`` event is recorded for audit.

    Also escalates stale rows (``started_at`` older than 1 hour) to
    FAILED so truly orphaned jobs don't loop forever.
    """
    now = _utcnow()
    # 1) Stale rows (started > 1h ago) → FAILED with stage=timeout
    stale = conn.execute(
        """
        UPDATE jobs SET
            status = 'FAILED',
            error_stage = 'timeout',
            error_message = 'daemon restart: stuck PROCESSING > 1h',
            finished_at = ?,
            updated_at  = ?
        WHERE status = 'PROCESSING'
          AND started_at IS NOT NULL
          AND (julianday(?) - julianday(started_at)) * 24 > 1
        """,
        (now, now, now),
    ).rowcount
    if stale:
        conn.execute(
            """
            INSERT INTO events (job_id, kind, detail)
            SELECT id, 'failed', 'timeout: PROCESSING > 1h'
            FROM jobs WHERE status='FAILED' AND error_stage='timeout'
              AND updated_at = ?
            """,
            (now,),
        )

    # 2) Fresh PROCESSING rows → PENDING with a restart marker
    fresh = conn.execute(
        """
        UPDATE jobs SET
            status = 'PENDING',
            error_stage = 'daemon_restart',
            updated_at = ?
        WHERE status = 'PROCESSING'
        """,
        (now,),
    ).rowcount
    if fresh:
        conn.execute(
            """
            INSERT INTO events (job_id, kind, detail)
            SELECT id, 'restarted', 'PROCESSING→PENDING'
            FROM jobs WHERE status='PENDING' AND error_stage='daemon_restart'
              AND updated_at = ?
            """,
            (now,),
        )
    return stale + fresh


def delete_jobs(conn: sqlite3.Connection, job_ids: Iterable[int]) -> int:
    """Delete jobs and (via FK cascade) their events. Returns rowcount."""
    ids = list(job_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", ids)
    return cur.rowcount


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    """ISO 8601 UTC timestamp with second precision (matches SQLite default)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _transaction(conn: sqlite3.Connection):
    """Tiny BEGIN/COMMIT wrapper used by ``fetch_control_signals``.

    The connection is opened in autocommit mode (``isolation_level=None``)
    so we have to be explicit about transactions when we want atomic
    multi-statement reads + deletes.
    """
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


__all__ = [
    "EVENT_CANCELLED",
    "EVENT_DISK_LOW",
    "EVENT_DISK_RECOVERED",
    "EVENT_ENQUEUED",
    "EVENT_FAILED",
    "EVENT_FINISHED",
    "EVENT_PAUSED",
    "EVENT_RESTARTED",
    "EVENT_RESUMED",
    "EVENT_STARTED",
    "Job",
    "SIGNAL_CANCEL",
    "STATUS_CANCELLED",
    "STATUS_FAILED",
    "STATUS_PAUSED",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "STATUS_SUCCESS",
    "connect",
    "delete_jobs",
    "fetch_control_signals",
    "fetch_job",
    "fetch_latest_stat",
    "fetch_next_pending",
    "increment_retry",
    "init_schema",
    "insert_job",
    "list_events",
    "list_jobs",
    "mark_cancelled",
    "mark_failed",
    "mark_processing",
    "mark_success",
    "pause",
    "pause_all_non_terminal",
    "queue_depths",
    "recover_crashed_jobs",
    "record_event",
    "record_stat",
    "resume",
    "resume_all_paused",
    "send_control_signal",
    "set_priority",
]
