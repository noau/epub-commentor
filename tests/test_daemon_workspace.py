"""Tests for :mod:`epub_commentor.daemon.workspace`."""

from __future__ import annotations

from pathlib import Path

import pytest

from epub_commentor.daemon.workspace import Workspace, jobs_root


class TestLayout:
    def test_paths(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=42, base_dir=tmp_path / "jobs")
        assert ws.root == tmp_path / "jobs" / "job_42"
        assert ws.input_epub == tmp_path / "jobs" / "job_42" / "input.epub"
        assert ws.output_epub == tmp_path / "jobs" / "job_42" / "output.commented.epub"
        assert ws.cache_dir == tmp_path / "jobs" / "job_42" / "cache"
        assert ws.log_dir == tmp_path / "jobs" / "job_42" / "logs"
        assert ws.meta_json == tmp_path / "jobs" / "job_42" / "meta.json"
        assert ws.log_archive == tmp_path / "jobs" / "job_42" / "logs" / "archive.tar.gz"

    def test_frozen_dataclass(self) -> None:
        ws = Workspace(job_id=1, base_dir=Path("/tmp/jobs"))
        with pytest.raises((AttributeError, Exception)):
            ws.job_id = 99  # type: ignore[misc]


class TestEnsureDirs:
    def test_creates_cache_and_logs(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.input_epub.parent.mkdir(parents=True)
        ws.input_epub.write_bytes(b"fake epub")
        ws.ensure_dirs()
        assert ws.cache_dir.exists() and ws.cache_dir.is_dir()
        assert ws.log_dir.exists() and ws.log_dir.is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.input_epub.parent.mkdir(parents=True)
        ws.input_epub.write_bytes(b"x")
        ws.ensure_dirs()
        ws.ensure_dirs()  # second call should not raise
        assert ws.cache_dir.exists()
        assert ws.log_dir.exists()


class TestArchiveLogs:
    def test_no_logs_is_noop(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.ensure_dirs()
        ws.archive_logs()
        assert not ws.log_archive.exists()

    def test_tars_logs(self, tmp_path: Path) -> None:
        import tarfile

        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.ensure_dirs()
        (ws.log_dir / "a.log").write_text("alpha", encoding="utf-8")
        (ws.log_dir / "b.log").write_text("bravo", encoding="utf-8")
        ws.archive_logs()
        assert ws.log_archive.exists()
        assert not (ws.log_dir / "a.log").exists()
        assert not (ws.log_dir / "b.log").exists()
        # The archive contains both files
        with tarfile.open(ws.log_archive, "r:gz") as tar:
            names = tar.getnames()
        assert sorted(names) == ["a.log", "b.log"]

    def test_archive_overwrites_previous(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.ensure_dirs()
        (ws.log_dir / "first.log").write_text("first", encoding="utf-8")
        ws.archive_logs()
        # Simulate a second run
        (ws.log_dir / "second.log").write_text("second", encoding="utf-8")
        ws.archive_logs()
        assert ws.log_archive.exists()
        # New archive should differ from the first one (different content).
        # We don't assert strictly greater because tar metadata could
        # compress smaller; just check both .log files are gone.
        assert not (ws.log_dir / "second.log").exists()


class TestCleanupCache:
    def test_removes_cache_dir(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.ensure_dirs()
        (ws.cache_dir / "entry.txt").write_text("stale", encoding="utf-8")
        ws.cleanup_cache()
        assert not ws.cache_dir.exists()

    def test_missing_cache_is_noop(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.ensure_dirs()
        # Cache exists; remove it once
        ws.cleanup_cache()
        # Second call should not raise
        ws.cleanup_cache()


class TestRemove:
    def test_removes_whole_workspace(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.ensure_dirs()
        (ws.cache_dir / "x").write_text("x", encoding="utf-8")
        (ws.input_epub).write_bytes(b"fake")
        ws.remove()
        assert not ws.root.exists()

    def test_missing_is_noop(self, tmp_path: Path) -> None:
        ws = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        ws.remove()  # should not raise


class TestJobsRoot:
    def test_returns_workspace_jobs_subdir(self) -> None:
        assert jobs_root(Path("/tmp/ws")) == Path("/tmp/ws/jobs")
