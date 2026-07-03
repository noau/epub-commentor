"""``epubctl`` — local CLI to manage the epub-commentor daemon.

No HTTP, no auth: this script talks to the daemon SQLite database
directly via :mod:`epub_commentor.daemon.db`. Subcommands:

* ``submit``            enqueue a new EPUB
* ``status``            list jobs (optionally live-watch)
* ``show``              job details
* ``log``               tail ``<ws>/jobs/job_<id>/logs/*.log``
* ``events``            list lifecycle events for a job
* ``retry``             FAILED → PENDING
* ``cancel``            ask worker to abort a job
* ``resume``            PAUSED → PENDING
* ``pause-all`` / ``resume-all``  bulk toggles
* ``priority <id> N``   adjust job priority
* ``health``            show server stats + queue depths
* ``prune``             delete old jobs + their workspaces
* ``watch``             live status refresh (Ctrl-C exits)
* ``recover``           manual ``recover_crashed_jobs`` trigger

SQLite location resolution order:

1. ``--db <path>`` argument
2. ``$EPUBCTL_DAEMON_DB`` env var
3. ``./daemon.sqlite`` (relative to cwd)

This mirrors the operator-friendly defaults in the daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .daemon import db
from .daemon.workspace import Workspace, jobs_root

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = Path("daemon.sqlite")


def resolve_db_path(args_db: str | None) -> Path:
    """Resolve the SQLite path from CLI / env / cwd."""
    if args_db:
        p = Path(args_db)
        if not p.exists():
            sys.stderr.write(f"epubctl: database not found at {p}\n")
            raise SystemExit(2)
        return p
    env = os.environ.get("EPUBCTL_DAEMON_DB")
    if env:
        p = Path(env)
        if not p.exists():
            sys.stderr.write(f"epubctl: $EPUBCTL_DAEMON_DB={env} does not exist\n")
            raise SystemExit(2)
        return p
    if DEFAULT_DB_PATH.exists():
        return DEFAULT_DB_PATH.resolve()
    sys.stderr.write(
        f"epubctl: no database found; pass --db, set $EPUBCTL_DAEMON_DB, "
        f"or run from a directory containing {DEFAULT_DB_PATH}\n"
    )
    raise SystemExit(2)


def open_db(path: Path) -> sqlite3.Connection:
    """Open SQLite read-write, ensure schema exists.

    Returns the bare :class:`sqlite3.Connection`; callers that need the
    DB path (e.g. :func:`cmd_submit`) should call :func:`open_db_with_path`
    instead so the path can be retrieved via :func:`conn_db_path`.
    """
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


_DbHolder = sqlite3.Connection  # alias kept for type hints only


_DB_PATHS: dict[int, str] = {}


def open_db_with_path(path: Path) -> sqlite3.Connection:
    """Like :func:`open_db`, but also remember the DB path for downstream helpers.

    We can't set arbitrary attributes on ``sqlite3.Connection`` (no
    ``__dict__``), so we keep a tiny module-level ``dict[id(conn)]→str``.
    """
    conn = open_db(path)
    _DB_PATHS[id(conn)] = str(path)
    return conn


def conn_db_path(conn: sqlite3.Connection) -> str | None:
    return _DB_PATHS.get(id(conn))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_status_table(jobs: list[db.Job]) -> str:
    """Render a fixed-width table for ``status``."""
    if not jobs:
        return "(no jobs)"
    headers = ("id", "status", "pri", "file", "tokens", "comments", "age")
    rows: list[tuple[str, ...]] = []
    now = time.time()
    for j in jobs:
        age = _human_age(j.created_at or "", now)
        tokens = f"{j.input_tokens}/{j.output_tokens}"
        rows.append(
            (
                str(j.id),
                j.status,
                str(j.priority),
                _truncate(j.file_name, 24),
                tokens,
                str(j.total_comments),
                age,
            )
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = "\n".join("  ".join(r[i].ljust(widths[i]) for i in range(len(r))) for r in rows)
    return f"{line}\n{sep}\n{body}"


def _human_age(iso_ts: str, now_epoch: float) -> str:
    """Best-effort humanised age from an ISO-ish timestamp."""
    try:
        # SQLite ``datetime('now')`` returns ``YYYY-MM-DD HH:MM:SS`` UTC
        head, _, tail = iso_ts.partition("T")
        if "T" in iso_ts:
            iso = iso_ts.replace("Z", "+00:00")
        else:
            iso = iso_ts + "Z"
        from datetime import datetime

        parsed = datetime.fromisoformat(iso).timestamp()
        delta = max(0.0, now_epoch - parsed)
    except ValueError:
        return iso_ts
    if delta < 60:
        return f"{delta:.0f}s"
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    if delta < 86400:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86400:.1f}d"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_submit(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """Copy the EPUB into the job's workspace + INSERT."""
    src = Path(args.file).resolve()
    if not src.exists() or src.suffix.lower() != ".epub":
        sys.stderr.write(f"epubctl: not an EPUB file: {src}\n")
        return 2

    flags: dict[str, Any] = {}
    if args.flags_json:
        try:
            flags = json.loads(args.flags_json)
            if not isinstance(flags, dict):
                raise ValueError("flags JSON must be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            sys.stderr.write(f"epubctl: bad --flags-json: {exc}\n")
            return 2
    if args.ai_select:
        flags["ai_select"] = True
    if args.no_review:
        flags["no_review"] = True
    if args.ai_review:
        flags["ai_review"] = True

    # Determine workspace_dir from sqlite location (assumed
    # ``<workspace_dir>/daemon.sqlite`` pattern).
    workspace_dir = conn_to_workspace_dir(conn)
    job_id = db.insert_job(
        conn,
        file_name=src.name,
        source_path=str(src),  # source *path* — the daemon copies it
        priority=args.priority,
        book_synopsis=args.synopsis,
        flags=flags,
        max_retries=args.max_retries,
    )
    # Allocate the workspace eagerly so the daemon doesn't need to
    # touch the filesystem on first run.
    ws = Workspace(job_id=job_id, base_dir=jobs_root(workspace_dir))
    ws.ensure_dirs()
    shutil.copy2(src, ws.input_epub)
    # Update the row's source_path to the workspace location.
    conn.execute(
        "UPDATE jobs SET source_path = ? WHERE id = ?",
        (str(ws.input_epub), job_id),
    )
    conn.commit()
    print(
        f"submitted job {job_id}: {src.name} (priority={args.priority}, retries={args.max_retries})"
    )
    print(f"  workspace: {ws.root}")
    return 0


def conn_to_workspace_dir(conn: sqlite3.Connection) -> Path:
    """Infer ``workspace_dir`` from the SQLite path.

    Convention: daemon.sqlite lives in ``workspace_dir/``. The CLI uses
    the parent directory of the DB as the workspace root.
    """
    p = conn_db_path(conn)
    if p is None:
        return Path(".").resolve()
    return Path(p).parent


def cmd_status(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    jobs = db.list_jobs(conn, status=args.status, limit=args.limit)
    print(_format_status_table(jobs))
    depths = db.queue_depths(conn)
    print()
    print(
        "depths: "
        + ", ".join(f"{k}={v}" for k, v in sorted(depths.items()))
    )
    return 0


def cmd_show(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    job = db.fetch_job(conn, args.id)
    if job is None:
        sys.stderr.write(f"epubctl: no such job {args.id}\n")
        return 2
    print(json.dumps(_job_to_dict(job), indent=2, ensure_ascii=False))
    if args.meta and job.output_path:
        meta = Path(job.output_path).parent / "meta.json"
        if meta.exists():
            print(f"\n--- {meta} ---")
            print(meta.read_text(encoding="utf-8"))
    return 0


def _job_to_dict(j: db.Job) -> dict[str, Any]:
    return {
        "id": j.id,
        "file_name": j.file_name,
        "status": j.status,
        "priority": j.priority,
        "retry_count": j.retry_count,
        "max_retries": j.max_retries,
        "error_stage": j.error_stage,
        "error_message": j.error_message,
        "source_path": j.source_path,
        "output_path": j.output_path,
        "created_at": j.created_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "tokens": {
            "input": j.input_tokens,
            "output": j.output_tokens,
            "cache": j.cache_tokens,
        },
        "chapters": {
            "processed": j.chapters_processed,
            "skipped": j.chapters_skipped,
        },
        "total_comments": j.total_comments,
        "flags": j.flags,
    }


def cmd_log(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    job = db.fetch_job(conn, args.id)
    if job is None:
        sys.stderr.write(f"epubctl: no such job {args.id}\n")
        return 2
    workspace_dir = conn_to_workspace_dir(conn)
    ws = Workspace(job_id=job.id, base_dir=jobs_root(workspace_dir))
    if not ws.log_dir.exists():
        sys.stderr.write(f"epubctl: no log directory for job {job.id}\n")
        return 2
    logs = sorted(ws.log_dir.glob("*.log"))
    if not logs:
        logs = sorted(ws.log_dir.glob("**/*"))
    last_size = 0
    while True:
        for log_file in logs:
            if log_file.exists() and log_file.stat().st_size != last_size:
                last_size = log_file.stat().st_size
                content = _tail_lines(log_file, args.tail)
                sys.stdout.write(content)
                sys.stdout.flush()
        if not args.follow:
            return 0
        time.sleep(args.interval)


def _tail_lines(path: Path, n: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-n:] if n > 0 else lines)
    except OSError as exc:
        return f"(unable to read {path}: {exc})"


def cmd_events(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    events = db.list_events(conn, args.id, limit=args.limit)
    if not events:
        print("(no events)")
        return 0
    for ev in events:
        ts = ev["ts"]
        detail = f" — {ev['detail']}" if ev["detail"] else ""
        print(f"{ts}  {ev['kind']}{detail}")
    return 0


def cmd_retry(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    if not db.resume(conn, args.id):
        sys.stderr.write(f"epubctl: cannot resume job {args.id} (terminal or missing)\n")
        return 2
    print(f"job {args.id} re-queued for retry")
    return 0


def cmd_cancel(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    job = db.fetch_job(conn, args.id)
    if job is None:
        sys.stderr.write(f"epubctl: no such job {args.id}\n")
        return 2
    # Detect an existing pending signal so duplicate cancels are loud.
    existing = [
        s for s in db.fetch_control_signals(conn) if s["job_id"] == args.id
    ]
    # Restoring the signals we just consumed — the table is empty before
    # this point, but to be safe re-insert what we read.
    for s in existing:
        db.send_control_signal(conn, args.id, s["kind"])
    if existing:
        sys.stderr.write(f"epubctl: cancel signal already pending for job {args.id}\n")
        return 2
    db.send_control_signal(conn, args.id, db.SIGNAL_CANCEL)
    print(f"cancel signal sent to job {args.id}")
    return 0


def cmd_resume(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    if not db.resume(conn, args.id):
        sys.stderr.write(f"epubctl: cannot resume job {args.id}\n")
        return 2
    print(f"job {args.id} resumed")
    return 0


def cmd_pause_all(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    n = db.pause_all_non_terminal(conn, reason=args.reason or "operator")
    print(f"paused {n} non-terminal job(s)")
    return 0


def cmd_resume_all(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    n = db.resume_all_paused(conn)
    print(f"resumed {n} paused job(s)")
    return 0


def cmd_priority(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    if not db.set_priority(conn, args.id, args.priority):
        sys.stderr.write(f"epubctl: cannot set priority for job {args.id}\n")
        return 2
    print(f"job {args.id} priority = {args.priority}")
    return 0


def cmd_health(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    stat = db.fetch_latest_stat(conn)
    depths = db.queue_depths(conn)
    print("queue:")
    for k, v in sorted(depths.items()):
        print(f"  {k:12s} {v}")
    if stat is None:
        print("\nno server stats recorded yet")
    else:
        print("\nlatest server sample:")
        print(f"  ts={stat['ts']}")
        print(f"  cpu={stat['cpu_percent']:.1f}%  mem={stat['mem_percent']:.1f}%")
        print(
            f"  disk used={stat['disk_used_percent']:.1f}%  "
            f"avail={stat['disk_available_gb']:.2f} GB"
        )
        print(
            f"  pending={stat['pending_jobs']}  processing={stat['processing_jobs']}"
        )
    return 0


def cmd_prune(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    keep = {"SUCCESS"} if args.keep_success else set()
    status_filter: set[str] = set()
    if args.success:
        status_filter.add("SUCCESS")
    if args.failed:
        status_filter.add("FAILED")
    if args.cancelled:
        status_filter.add("CANCELLED")
    if not status_filter:
        status_filter = {"SUCCESS", "FAILED", "CANCELLED"}
    keep = keep or set()
    deleted_jobs = 0
    jobs = db.list_jobs(conn)
    for job in jobs:
        if job.status not in status_filter:
            continue
        if job.status in keep:
            continue
        if not args.force and not _confirm(f"delete job {job.id} ({job.file_name}, {job.status})"):
            continue
        # Remove workspace directory
        workspace_dir = conn_to_workspace_dir(conn)
        ws_dir = jobs_root(workspace_dir) / f"job_{job.id}"
        if ws_dir.exists():
            shutil.rmtree(ws_dir, ignore_errors=True)
        deleted_jobs += db.delete_jobs(conn, [job.id])
    print(f"pruned {deleted_jobs} job row(s) and their workspaces")
    return 0


def _confirm(prompt: str) -> bool:
    sys.stdout.write(f"{prompt}? [y/N] ")
    sys.stdout.flush()
    answer = sys.stdin.readline().strip().lower()
    return answer == "y"


def cmd_watch(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")  # clear screen
            print(_format_status_table(db.list_jobs(conn)))
            print(f"\nrefreshing every {args.interval}s — Ctrl-C to exit")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_recover(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    n = db.recover_crashed_jobs(conn)
    print(f"recovered {n} orphaned PROCESSING job(s)")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epubctl",
        description="Manage a running epub-commentor daemon (SQLite-backed).",
    )
    p.add_argument("--db", help="Path to daemon.sqlite (overrides env + cwd).")
    sub = p.add_subparsers(dest="command", required=True)

    # submit
    s = sub.add_parser("submit", help="Enqueue a new EPUB for commenting.")
    s.add_argument("file", help="Path to the source EPUB file.")
    s.add_argument("--priority", type=int, default=0)
    s.add_argument("--synopsis", default=None)
    s.add_argument("--max-retries", type=int, default=3)
    s.add_argument("--flags-json", default=None)
    s.add_argument("--ai-select", action="store_true")
    s.add_argument("--no-review", action="store_true")
    s.add_argument("--ai-review", action="store_true")

    # status / watch
    s = sub.add_parser("status", help="List jobs.")
    s.add_argument("--status", default=None)
    s.add_argument("--limit", type=int, default=None)

    s = sub.add_parser("watch", help="Live status refresh.")
    s.add_argument("--interval", type=float, default=2.0)

    # show
    s = sub.add_parser("show", help="Job details (JSON).")
    s.add_argument("id", type=int)
    s.add_argument("--meta", action="store_true")

    # log
    s = sub.add_parser("log", help="Tail a job's logs.")
    s.add_argument("id", type=int)
    s.add_argument("--tail", type=int, default=200)
    s.add_argument("--follow", action="store_true")
    s.add_argument("--interval", type=float, default=1.0)

    # events
    s = sub.add_parser("events", help="Lifecycle events for a job.")
    s.add_argument("id", type=int)
    s.add_argument("--limit", type=int, default=50)

    # retry / cancel / resume
    for name, help_text in (
        ("retry", "Re-queue a FAILED job."),
        ("cancel", "Send a cooperative cancel to a running job."),
        ("resume", "Resume a PAUSED job."),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("id", type=int)

    # priority
    s = sub.add_parser("priority", help="Adjust job priority.")
    s.add_argument("id", type=int)
    s.add_argument("priority", type=int)

    # bulk
    s = sub.add_parser("pause-all", help="Pause every non-terminal job.")
    s.add_argument("--reason", default=None)
    sub.add_parser("resume-all", help="Resume every paused job.")

    # health / prune / recover
    sub.add_parser("health", help="Show queue + last server sample.")
    s = sub.add_parser("prune", help="Delete old jobs + workspaces.")
    s.add_argument("--success", action="store_true")
    s.add_argument("--failed", action="store_true")
    s.add_argument("--cancelled", action="store_true")
    s.add_argument("--keep-success", action="store_true")
    s.add_argument("--force", action="store_true")
    sub.add_parser("recover", help="Manually trigger crash recovery.")

    return p


# Dispatch table
_HANDLERS = {
    "submit": cmd_submit,
    "status": cmd_status,
    "watch": cmd_watch,
    "show": cmd_show,
    "log": cmd_log,
    "events": cmd_events,
    "retry": cmd_retry,
    "cancel": cmd_cancel,
    "resume": cmd_resume,
    "priority": cmd_priority,
    "pause-all": cmd_pause_all,
    "resume-all": cmd_resume_all,
    "health": cmd_health,
    "prune": cmd_prune,
    "recover": cmd_recover,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = resolve_db_path(args.db)
    conn = open_db_with_path(db_path)
    try:
        return _HANDLERS[args.command](args, conn)
    finally:
        _DB_PATHS.pop(id(conn), None)
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
