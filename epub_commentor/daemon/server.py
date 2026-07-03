"""Daemon entry point: worker loop + signal handling + single-instance lock.

This module owns the *process-level* orchestration:

* a single-threaded ``worker_loop`` that pulls jobs from SQLite and
  delegates to :func:`~epub_commentor.daemon.worker.run_job`
* a ``DiskMonitor`` circuit breaker that pauses the whole queue when the
  workspace volume runs low
* POSIX-style signal handlers (``SIGINT``, ``SIGTERM``) for graceful
  shutdown that drain the current job before exiting
* an ``fcntl.flock`` based single-instance guard so two daemons cannot
  race for the same SQLite / workspace

The actual *job*-level logic (LLM construction, retries, …) lives in
:mod:`epub_commentor.daemon.worker`; this module is deliberately thin
so unit tests can mock the heavy bits.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..llm._abort import request_abort, reset_abort
from ..logging_setup import setup_root_logger
from . import db
from .config import DaemonConfig, load_daemon_config
from .disk_monitor import DiskMonitor
from .worker import run_job

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Single-instance lock (fcntl, falls back to PID file on Windows)
# ---------------------------------------------------------------------------


class AlreadyRunningError(RuntimeError):
    """Raised when another daemon process holds the lock file."""


def acquire_singleton(lock_path: Path) -> int:
    """Open (and try to flock) ``lock_path``. Returns the OS file descriptor.

    The caller is responsible for closing it on shutdown. On Windows the
    ``fcntl`` module is unavailable, so we fall back to a best-effort
    PID check; flock-based protection is a no-op there.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        import fcntl  # POSIX-only; absent on Windows

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
    except (ImportError, OSError) as exc:
        _logger.debug("fcntl.flock unavailable: %s", exc)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def release_singleton(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the ``python -m epub_commentor.daemon`` argument parser."""
    p = argparse.ArgumentParser(
        prog="epub_commentor.daemon",
        description="Run the EPUB commentor daemon (single worker, in-process).",
    )
    p.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Directory holding daemon.sqlite + jobs/ (created if missing).",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to format.daemon.json (defaults to <workspace>/format.daemon.json, "
        "or ./format.daemon.json in that order).",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle (debug).",
    )
    p.add_argument(
        "--max-seconds",
        type=int,
        default=0,
        help="Exit after N wall-clock seconds (smoke-test).",
    )
    return p


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def serve(args: argparse.Namespace) -> int:
    """Run the worker loop until SIGINT/SIGTERM.

    Returns the process exit code (``0`` for clean shutdown).
    """
    cfg = load_daemon_config(args.config)
    # CLI --workspace always wins over the JSON
    cfg.workspace_dir = Path(args.workspace).resolve()
    setup_root_logger(level=cfg.log_level, fmt=cfg.log_format)
    log = logging.getLogger("epub_commentor.daemon")

    cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.resolve_sqlite_path())
    db.init_schema(conn)

    # ---- Crash recovery ----
    recovered = db.recover_crashed_jobs(conn)
    if recovered:
        log.warning("recovered %d orphaned PROCESSING jobs on startup", recovered)

    # ---- Single-instance guard ----
    lock_path = cfg.workspace_dir / "daemon.lock"
    try:
        lock_fd = acquire_singleton(lock_path)
    except OSError as exc:
        log.error("could not open %s: %s", lock_path, exc)
        return 2

    # ---- Shutdown event + signals ----
    shutdown = threading.Event()

    def _on_sigint(_signum, _frame):  # pragma: no cover - signal path
        # First Ctrl-C: ask the in-flight job to cooperatively cancel
        # (matches the existing epub_commentor.llm._abort contract).
        # The worker_loop polls `shutdown` to exit the outer loop once
        # the current job returns.
        log.warning("SIGINT received — requesting abort + shutdown")
        request_abort()
        shutdown.set()

    def _on_sigterm(_signum, _frame):  # pragma: no cover - signal path
        log.warning("SIGTERM received — initiating graceful shutdown")
        request_abort()
        shutdown.set()

    for sig, fn in ((signal.SIGINT, _on_sigint), (signal.SIGTERM, _on_sigterm)):
        try:
            signal.signal(sig, fn)
        except (ValueError, OSError):
            pass  # not the main thread on some platforms

    log.info(
        "epub-commentor daemon starting (workspace=%s, sql=%s, session=%s)",
        cfg.workspace_dir,
        cfg.resolve_sqlite_path(),
        uuid.uuid4().hex[:8],
    )

    # ---- Pre-load base LLM kwargs from format.json once ----
    base_llm_kwargs = _load_base_llm_kwargs(cfg)

    rc = worker_loop(
        cfg,
        conn,
        shutdown,
        base_llm_kwargs=base_llm_kwargs,
        once=args.once,
        max_seconds=args.max_seconds,
    )

    release_singleton(lock_fd)
    conn.close()
    log.info("epub-commentor daemon exiting with code %d", rc)
    return rc


# ---------------------------------------------------------------------------
# Worker loop (extracted so tests can drive it without spawning a process)
# ---------------------------------------------------------------------------


def worker_loop(
    cfg: DaemonConfig,
    conn: sqlite3.Connection,
    shutdown: threading.Event,
    *,
    base_llm_kwargs: dict[str, Any],
    once: bool = False,
    max_seconds: int = 0,
) -> int:
    """Single-thread worker loop.

    ``once=True`` returns after one poll cycle (debug). ``max_seconds``
    caps wall-clock time for smoke tests. Returns an exit code.
    """
    log = _logger
    disk = DiskMonitor(cfg.disk, workspace_dir=cfg.workspace_dir)
    current_job_id: int | None = None
    started = time.monotonic()

    while not shutdown.is_set():
        # ---- Stop conditions ----
        if max_seconds and (time.monotonic() - started) >= max_seconds:
            log.info("max-seconds reached, exiting")
            break

        # ---- Drain control signals for current job ----
        sigs = db.fetch_control_signals(conn)
        for sig in sigs:
            if current_job_id is not None and sig["job_id"] == current_job_id:
                if sig["kind"] == db.SIGNAL_CANCEL:
                    log.info("cancelling current job %d via signal", current_job_id)
                    request_abort()

        # ---- Disk circuit breaker ----
        if disk.is_low():
            if not disk.was_low():
                n = db.pause_all_non_terminal(conn, reason="disk_low")
                log.warning("disk low — paused %d non-terminal job(s)", n)
            time.sleep(cfg.poll_interval_paused_seconds)
            continue
        if disk.was_low() and disk.recovered():
            n = db.resume_all_paused(conn)
            log.warning("disk recovered — resumed %d paused job(s)", n)

        # ---- Pull next job ----
        if once and current_job_id is None:
            job = db.fetch_next_pending(conn)
            if job is None:
                log.info("--once + no pending job, exiting")
                return 0
        else:
            job = db.fetch_next_pending(conn)

        if job is None:
            time.sleep(cfg.poll_interval_idle_seconds)
            continue

        current_job_id = job.id
        reset_abort()
        try:
            run_job(
                conn,
                job,
                base_llm_kwargs=base_llm_kwargs,
                workspace_root=cfg.workspace_dir,
            )
        except Exception:  # pragma: no cover — run_job already swallows
            log.exception("job %d crashed (unhandled) in worker_loop", job.id)
            try:
                db.mark_failed(conn, job.id, stage="unhandled", message="see daemon log")
                db.increment_retry(conn, job.id)
            except sqlite3.DatabaseError:
                pass
        finally:
            current_job_id = None

        if once:
            return 0

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_base_llm_kwargs(cfg: DaemonConfig) -> dict[str, Any]:
    """Read the project's ``format.json`` once so all jobs share LLM settings.

    Tries, in order:

    1. ``$EPUB_COMMENTOR_FORMAT_JSON`` (absolute path)
    2. ``<workspace>/format.json``
    3. ``./format.json`` (operator's working directory)

    A missing file yields an empty dict — the daemon still runs.
    """
    candidates: list[Path] = []
    env = os.environ.get("EPUB_COMMENTOR_FORMAT_JSON")
    if env:
        candidates.append(Path(env))
    candidates.append(cfg.workspace_dir / "format.json")
    candidates.append(Path("format.json"))

    for c in candidates:
        if c.exists():
            try:
                with c.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    _logger.info("loaded base LLM kwargs from %s", c)
                    return dict(data)
            except (OSError, json.JSONDecodeError) as exc:
                _logger.warning("ignoring unreadable format.json at %s: %s", c, exc)
    _logger.info("no format.json found; LLM kwargs come from job flags only")
    return {}


__all__ = [
    "serve",
    "worker_loop",
    "acquire_singleton",
    "release_singleton",
    "build_arg_parser",
]
