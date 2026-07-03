"""End-to-end daemon + ``epubctl`` integration test.

Drives the real :mod:`server.worker_loop` once on a freshly-built EPUB,
mocked LLM, and a real :class:`sqlite3.Connection`. Verifies that the
CLI round-trip (submit → status → log → show → prune) composes with
the daemon's persistence layer correctly.
"""

from __future__ import annotations

import threading
import zipfile
from pathlib import Path

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor import ctl
from epub_commentor.daemon import db, server, worker
from epub_commentor.daemon.config import DaemonConfig, DiskCircuitConfig


def _memo_json() -> str:
    return json_dumps(
        {
            "core_thesis": "test memo thesis",
            "outline": ["point a", "point b"],
            "tone": "neutral",
            "target_audience": "general",
        }
    )


def _annotations_json() -> str:
    return json_dumps(
        {
            "comments": [
                {
                    "target_p_ids": [0],
                    "position": "before",
                    "kind": "intro",
                    "content": "Tiny intro for e2e.",
                }
            ]
        }
    )


def _smart_mock_llm() -> MockLLM:
    return MockLLM(
        responses_by_seed={
            "scan__response": _memo_json(),
            "annotate__response": _annotations_json(),
        },
        default_response=_memo_json(),
    )


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> DaemonConfig:
    monkeypatch.chdir(tmp_path)
    cfg = DaemonConfig(
        workspace_dir=tmp_path.resolve(),
        poll_interval_idle_seconds=0.01,
        poll_interval_paused_seconds=0.01,
        disk=DiskCircuitConfig(min_free_gb=0.001, min_free_percent=0.1),
    )
    # Initialise the SQLite file so subsequent CLI/worker invocations
    # find it via cwd-resolved ``daemon.sqlite``.
    conn = db.connect(cfg.resolve_sqlite_path())
    db.init_schema(conn)
    conn.close()
    return cfg


def _make_epub(path: Path, *, title: str = "E2E Book", paragraphs: int = 4) -> None:
    body = "".join(f"<p>Sentence {i} of e2e test.</p>" for i in range(paragraphs))
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>{title}</title></head>"
        f"<body><h1>{title}</h1>{body}</body></html>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">'
        '<manifest>'
        '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        '</manifest>'
        '<spine><itemref idref="ch1"/></spine>'
        '</package>'
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles>'
        '<rootfile full-path="book.opf" media-type="application/oebps-package+xml"/>'
        '</rootfiles>'
        '</container>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("ch1.xhtml", xhtml)
        zf.writestr("book.opf", opf)


class TestDaemonE2E:
    def test_worker_run_marks_success(
        self, cfg: DaemonConfig, tmp_path: Path, monkeypatch
    ) -> None:
        """Full pipeline: submit EPUB → daemon runs MockLLM → SUCCESS."""
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")

        # Build the source EPUB and submit via ctl
        src = tmp_path / "source.epub"
        _make_epub(src)
        assert ctl.main(["submit", str(src)]) == 0

        # The submit created workspace/job_1/; drive one worker_loop iteration
        conn = db.connect(cfg.resolve_sqlite_path())
        db.init_schema(conn)
        shutdown = threading.Event()
        monkeypatch.setattr(worker, "_build_llm", lambda *_a, **_kw: _smart_mock_llm())

        rc = server.worker_loop(
            cfg, conn, shutdown, base_llm_kwargs={}, once=True, max_seconds=30
        )
        assert rc == 0

        job = db.fetch_job(conn, 1)
        assert job is not None
        assert job.status == db.STATUS_SUCCESS
        assert Path(job.output_path).exists()
        # Per-job workspace exists
        ws = tmp_path / "jobs" / "job_1"
        assert (ws / "input.epub").exists()
        assert (ws / "meta.json").exists()

    def test_cli_round_trip(
        self, cfg: DaemonConfig, tmp_path: Path, monkeypatch
    ) -> None:
        """submit → status → show → log → prune compose correctly."""
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")

        src = tmp_path / "src.epub"
        _make_epub(src)
        assert ctl.main(["submit", str(src)]) == 0

        # status
        conn = db.connect(cfg.resolve_sqlite_path())
        db.init_schema(conn)
        jobs = db.list_jobs(conn)
        assert len(jobs) == 1

        # show
        rc = ctl.main(["show", str(jobs[0].id)])
        assert rc == 0
        # Now run the job in-place
        monkeypatch.setattr(worker, "_build_llm", lambda *_a, **_kw: _smart_mock_llm())
        shutdown = threading.Event()
        server.worker_loop(
            cfg, conn, shutdown, base_llm_kwargs={}, once=True, max_seconds=30
        )

        # show again
        rc = ctl.main(["show", str(jobs[0].id)])
        assert rc == 0

        # log via CLI
        rc = ctl.main(["log", str(jobs[0].id), "--tail", "20"])
        assert rc == 0

        # prune (success already; should be removable)
        rc = ctl.main(["prune", "--success", "--force"])
        assert rc == 0

        # After prune, the job row should be gone
        assert db.fetch_job(db.connect(cfg.resolve_sqlite_path()), jobs[0].id) is None


class TestSingleInstance:
    def test_acquire_singleton_writes_pid(self, tmp_path: Path) -> None:
        """Two acquires on the same lock file should both succeed on Windows
        (no flock), but only the test sees the right PID content."""
        lock = tmp_path / "daemon.lock"
        fd1 = server.acquire_singleton(lock)
        try:
            content = lock.read_text(encoding="utf-8")
            assert content.strip()
        finally:
            server.release_singleton(fd1)
