"""Tests for :mod:`epub_commentor.daemon.server` (process-level wiring).

Strategy: drive :func:`worker_loop` directly with a fake ``shutdown``
event + a small ``max_seconds`` so we don't have to spawn a real daemon
process. Single-instance lock and ``__main__`` entry point get lighter
smoke tests.
"""

from __future__ import annotations

import argparse
import sqlite3
import threading
from pathlib import Path
from unittest import mock

import pytest

from epub_commentor.daemon import db, server
from epub_commentor.daemon.config import DaemonConfig, DiskCircuitConfig


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "test.sqlite")
    db.init_schema(c)
    return c


@pytest.fixture
def cfg(tmp_path: Path) -> DaemonConfig:
    return DaemonConfig(
        workspace_dir=tmp_path.resolve(),
        poll_interval_idle_seconds=0.01,
        poll_interval_paused_seconds=0.01,
        max_retries=3,
        # Permissive defaults so worker_loop tests don't get blocked by
        # the disk-low branch on dev workstations.
        disk=DiskCircuitConfig(min_free_gb=0.001, min_free_percent=0.1),
    )


class TestArgParser:
    def test_requires_workspace(self) -> None:
        with pytest.raises(SystemExit):
            server.build_arg_parser().parse_args([])

    def test_accepts_workspace(self, tmp_path: Path) -> None:
        args = server.build_arg_parser().parse_args(["--workspace", str(tmp_path)])
        assert args.workspace == tmp_path
        assert args.once is False
        assert args.max_seconds == 0


class TestWorkerLoop:
    def test_exits_when_shutdown_set(
        self, cfg: DaemonConfig, conn: sqlite3.Connection
    ) -> None:
        shutdown = threading.Event()
        shutdown.set()  # pre-set so the loop exits on its first check
        rc = server.worker_loop(
            cfg,
            conn,
            shutdown,
            base_llm_kwargs={},
            once=False,
            max_seconds=0,
        )
        assert rc == 0

    def test_max_seconds_exits(
        self, cfg: DaemonConfig, conn: sqlite3.Connection
    ) -> None:
        """``max_seconds=N`` (with N>0) bounds wall-clock so the test
        doesn't hang. ``max_seconds=0`` is a no-op sentinel — the loop
        only exits when ``shutdown`` is set."""
        shutdown = threading.Event()
        rc = server.worker_loop(
            cfg,
            conn,
            shutdown,
            base_llm_kwargs={},
            once=False,
            max_seconds=1,  # bound the loop
        )
        assert rc == 0

    def test_runs_one_cycle_when_once(
        self, cfg: DaemonConfig, conn: sqlite3.Connection
    ) -> None:
        shutdown = threading.Event()
        # No pending jobs, --once must exit cleanly.
        rc = server.worker_loop(
            cfg,
            conn,
            shutdown,
            base_llm_kwargs={},
            once=True,
            max_seconds=0,
        )
        assert rc == 0

    def test_processes_one_job_when_pending(
        self, cfg: DaemonConfig, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # Seed an EPUB into the job's workspace
        ws = tmp_path / "jobs" / "job_1"
        ws.mkdir(parents=True)
        (ws / "input.epub").write_bytes(b"fake-epub-bytes")

        db.insert_job(
            conn,
            file_name="book.epub",
            source_path=str(ws / "input.epub"),
        )

        shutdown = threading.Event()

        # Mock run_job so we don't touch the real pipeline
        with mock.patch.object(server, "run_job") as fake_run:
            fake_run.return_value = None
            # Run exactly one iteration via --once semantics
            rc = server.worker_loop(
                cfg,
                conn,
                shutdown,
                base_llm_kwargs={},
                once=True,
                max_seconds=10,
            )
        assert rc == 0
        fake_run.assert_called_once()
        job_arg = fake_run.call_args.args[1]
        assert job_arg.id == 1


class TestDiskPausing:
    def test_low_disk_pauses_pending_job(
        self, cfg: DaemonConfig, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """When disk is low, the pending job is bulk-paused."""
        db.insert_job(
            conn,
            file_name="book.epub",
            source_path="/tmp/job_1/input.epub",
        )

        shutdown = threading.Event()
        cfg.poll_interval_paused_seconds = 0.0
        cfg.poll_interval_idle_seconds = 0.0

        with mock.patch.object(server, "DiskMonitor") as mock_cls:
            instance = mock_cls.return_value
            instance.is_low.return_value = True
            instance.was_low.return_value = False
            # Bound wall-clock so the loop exits via max_seconds without
            # relying on `shutdown` (which would short-circuit the disk
            # branch we want to exercise).
            rc = server.worker_loop(
                cfg,
                conn,
                shutdown,
                base_llm_kwargs={},
                once=False,
                max_seconds=1,
            )

        assert rc == 0
        depths = db.queue_depths(conn)
        assert depths.get(db.STATUS_PAUSED, 0) == 1


class TestAcquireSingleton:
    def test_writes_pid_and_returns_fd(self, tmp_path: Path) -> None:
        fd = server.acquire_singleton(tmp_path / "test.lock")
        try:
            content = tmp_path.joinpath("test.lock").read_bytes()
            # PID is non-empty bytes
            assert content
        finally:
            server.release_singleton(fd)


class TestServeSmoke:
    def test_serve_with_once_no_jobs(
        self, cfg: DaemonConfig, monkeypatch, tmp_path: Path
    ) -> None:
        """End-to-end: ``serve(args)`` boots, sees no jobs, exits 0."""
        cfg.workspace_dir.mkdir(parents=True, exist_ok=True)
        # Drop a format.daemon.json to exercise the config path
        cfg_path = tmp_path / "format.daemon.json"
        cfg_path.write_text(
            '{"workspace_dir": "' + str(cfg.workspace_dir).replace("\\", "\\\\") + '"}',
            encoding="utf-8",
        )
        args = argparse.Namespace(
            workspace=str(cfg.workspace_dir),
            config=cfg_path,
            once=True,
            max_seconds=5,
        )
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        rc = server.serve(args)
        assert rc == 0
