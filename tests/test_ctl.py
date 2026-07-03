"""Tests for :mod:`epub_commentor.ctl`.

Strategy: drive ``main()`` in-process with ``sys.argv`` patched; for
tests that need side-effects (logs, workspaces) build a real but
minimal daemon.sqlite via :func:`epub_commentor.daemon.db.connect`.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from epub_commentor import ctl
from epub_commentor.daemon import db


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    """Set up a workspace with ``daemon.sqlite`` + a sample EPUB."""
    monkeypatch.chdir(tmp_path)
    conn = db.connect(tmp_path / "daemon.sqlite")
    db.init_schema(conn)
    conn.close()
    # Sample EPUB (just enough to satisfy the file extension).
    epub = tmp_path / "book.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
    return tmp_path


def _make_running_job(conn: sqlite3.Connection, *, tmp_path: Path, status: str) -> int:
    """Insert a job in ``status`` and return its id."""
    job_id = db.insert_job(
        conn,
        file_name="book.epub",
        source_path=str(tmp_path / "book.epub"),
        priority=0,
    )
    if status != "PENDING":
        db.mark_processing(conn, job_id)
        if status == "SUCCESS":
            db.mark_success(
                conn,
                job_id,
                output_path=str(tmp_path / "out.commented.epub"),
                input_tokens=10,
                output_tokens=5,
                cache_tokens=0,
                chapters_processed=2,
                chapters_skipped=0,
                total_comments=4,
            )
        elif status == "FAILED":
            db.mark_failed(conn, job_id, stage="process", message="x")
        elif status == "PAUSED":
            db.pause(conn, job_id)
    return job_id


class TestSubmit:
    def test_creates_workspace_and_row(self, workdir: Path) -> None:
        rc = ctl.main(["submit", str(workdir / "book.epub"), "--priority", "5"])
        assert rc == 0
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jobs = db.list_jobs(conn)
            assert len(jobs) == 1
            j = jobs[0]
            assert j.priority == 5
            assert j.status == db.STATUS_PENDING
            ws_epub = Path(j.source_path)
            assert ws_epub.exists()
            assert ws_epub.name == "input.epub"
            assert ws_epub.parent.parent == workdir / "jobs"
        finally:
            conn.close()

    def test_bad_flags_json(self, workdir: Path) -> None:
        rc = ctl.main(["submit", str(workdir / "book.epub"), "--flags-json", "not json"])
        assert rc == 2

    def test_missing_file(self, workdir: Path) -> None:
        rc = ctl.main(["submit", str(workdir / "nope.epub")])
        assert rc == 2


class TestStatusAndShow:
    def test_status_renders_table(self, workdir: Path, capsys) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            _make_running_job(conn, tmp_path=workdir, status="FAILED")
        finally:
            conn.close()
        rc = ctl.main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "depths:" in out

    def test_show_missing_id(self, workdir: Path, capsys) -> None:
        rc = ctl.main(["show", "999"])
        assert rc == 2
        assert "no such job" in capsys.readouterr().err

    def test_show_existing(self, workdir: Path, capsys) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jid = _make_running_job(conn, tmp_path=workdir, status="PROCESSING")
        finally:
            conn.close()
        rc = ctl.main(["show", str(jid)])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["id"] == jid
        assert payload["status"] == "PROCESSING"


class TestControlSignals:
    def test_cancel_sends_signal(self, workdir: Path, capsys) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jid = _make_running_job(conn, tmp_path=workdir, status="PROCESSING")
        finally:
            conn.close()
        rc = ctl.main(["cancel", str(jid)])
        assert rc == 0
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            sigs = db.fetch_control_signals(conn)
        finally:
            conn.close()
        assert any(s["job_id"] == jid for s in sigs)

    def test_cancel_idempotent(self, workdir: Path) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jid = _make_running_job(conn, tmp_path=workdir, status="PROCESSING")
        finally:
            conn.close()
        assert ctl.main(["cancel", str(jid)]) == 0
        # Second call should refuse (signal already queued)
        assert ctl.main(["cancel", str(jid)]) == 2

    def test_retry_on_failed(self, workdir: Path) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jid = _make_running_job(conn, tmp_path=workdir, status="FAILED")
        finally:
            conn.close()
        rc = ctl.main(["retry", str(jid)])
        assert rc == 0
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            assert db.fetch_job(conn, jid).status == db.STATUS_PENDING
        finally:
            conn.close()


class TestPauseResumeAll:
    def test_pause_then_resume(self, workdir: Path) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            _make_running_job(conn, tmp_path=workdir, status="PENDING")
            _make_running_job(conn, tmp_path=workdir, status="PENDING")
        finally:
            conn.close()
        assert ctl.main(["pause-all", "--reason", "test"]) == 0
        assert ctl.main(["resume-all"]) == 0
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            depths = db.queue_depths(conn)
            assert depths[db.STATUS_PENDING] == 2
            assert depths.get(db.STATUS_PAUSED, 0) == 0
        finally:
            conn.close()


class TestPriority:
    def test_sets_priority(self, workdir: Path) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jid = _make_running_job(conn, tmp_path=workdir, status="PENDING")
        finally:
            conn.close()
        rc = ctl.main(["priority", str(jid), "99"])
        assert rc == 0
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            assert db.fetch_job(conn, jid).priority == 99
        finally:
            conn.close()


class TestHealthAndRecover:
    def test_health_no_stats(self, workdir: Path, capsys) -> None:
        rc = ctl.main(["health"])
        assert rc == 0
        assert "no server stats" in capsys.readouterr().out

    def test_recover_runs(self, workdir: Path) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jid = _make_running_job(conn, tmp_path=workdir, status="PROCESSING")
        finally:
            conn.close()
        rc = ctl.main(["recover"])
        assert rc == 0
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            assert db.fetch_job(conn, jid).status == db.STATUS_PENDING
        finally:
            conn.close()


class TestPrune:
    def test_dry_run_force_removes(self, workdir: Path) -> None:
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            jid = _make_running_job(conn, tmp_path=workdir, status="FAILED")
        finally:
            conn.close()
        rc = ctl.main(["prune", "--failed", "--force"])
        assert rc == 0
        conn = db.connect(workdir / "daemon.sqlite")
        try:
            assert db.fetch_job(conn, jid) is None
        finally:
            conn.close()


class TestResolveDbPath:
    def test_db_arg_wins(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "explicit.sqlite"
        db.connect(target)  # create
        db.init_schema(db.connect(target))
        assert ctl.resolve_db_path(str(target)) == target

    def test_env_var(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "env.sqlite"
        db.connect(target)
        db.init_schema(db.connect(target))
        monkeypatch.setenv("EPUBCTL_DAEMON_DB", str(target))
        monkeypatch.chdir(tmp_path)
        # Drop a default so we know env wins:
        (tmp_path / "daemon.sqlite").write_bytes(b"")
        assert ctl.resolve_db_path(None) == target

    def test_missing_raises(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EPUBCTL_DAEMON_DB", raising=False)
        with pytest.raises(SystemExit):
            ctl.resolve_db_path(None)
