"""Tests for :mod:`epub_commentor.daemon.worker`.

Layered approach:

* :class:`TestBuildConfig` / :class:`TestBuildLlM` — pure helpers, no I/O.
* :class:`TestClassifyError` — error → stage mapping.
* :class:`TestSaveMeta` — JSON snapshot of :class:`CommentorResult`.
* :class:`TestRunJob*` — end-to-end via a programmatically-built EPUB
  (no dependency on ``tests/assets/`` so this stays fast and portable).
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from unittest import mock

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor import LLM, CommentConfig, CommentorError
from epub_commentor.daemon import db, worker
from epub_commentor.errors import (
    CommentAbortError,
    CommentInvalidJSONError,
    CommentNoParagraphsError,
    CommentOrphanPIdError,
    CommentOverlapError,
    CommentReviewFailedError,
    CommentScanFailedError,
    CommentSelectFailedError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = db.connect(tmp_path / "test.sqlite")
    db.init_schema(c)
    return c


def _make_tiny_epub(path: Path, *, chapter_title: str = "Chapter 1", paragraphs: int = 2) -> None:
    """Build a minimal valid EPUB containing one chapter with N paragraphs.

    The structure mimics what :func:`epub_commentor.pipeline.extract.extract_chapters`
    expects — a spine-ordered XHTML body, an OPF manifest, and a mimetype
    entry that must be the first ZIP entry.
    """
    body_p = "".join(f"<p>Paragraph {i}.</p>" for i in range(paragraphs))
    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>"
        + chapter_title
        + "</title></head>"
        "<body><h1>"
        + chapter_title
        + "</h1>"
        + body_p
        + "</body></html>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">'
        "<manifest>"
        '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest>"
        "<spine>"
        '<itemref idref="ch1"/>'
        "</spine>"
        "</package>"
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        "<rootfiles>"
        '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles>"
        "</container>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # mimetype MUST be the first entry, stored uncompressed.
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("ch1.xhtml", xhtml)
        zf.writestr("content.opf", opf)


def _make_job_with_input(
    conn: sqlite3.Connection,
    *,
    workspace_root: Path,
    flags: dict | None = None,
    max_retries: int = 3,
) -> int:
    """Create a job AND copy a fake EPUB into its workspace.

    Returns the job id. Without the input.epub on disk, ``run_job``
    marks the job FAILED with ``stage='extract'`` — most tests want a
    real file, so we wire that up here.
    """
    job_id = db.insert_job(
        conn,
        file_name="tiny.epub",
        source_path=str(workspace_root / "jobs" / "job_placeholder" / "input.epub"),
        flags=flags or {},
        max_retries=max_retries,
    )
    ws = workspace_root / "jobs" / f"job_{job_id}"
    ws.mkdir(parents=True, exist_ok=True)
    _make_tiny_epub(ws / "input.epub")
    # Patch the source_path so it points at the now-existing file
    conn.execute("UPDATE jobs SET source_path = ? WHERE id = ?", (str(ws / "input.epub"), job_id))
    return job_id


def _job_id_placeholder() -> int:
    """Stand-in used only inside the source_path string at INSERT time."""
    return 0  # the row doesn't exist yet; we UPDATE on the next line


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
                    "content": "Tiny test intro.",
                }
            ]
        }
    )


# ---------------------------------------------------------------------------
# Helper: build_llm / build_config
# ---------------------------------------------------------------------------


class TestBuildLlM:
    def test_routes_cache_and_log_paths_to_workspace(self, tmp_path: Path) -> None:
        from epub_commentor.daemon.workspace import Workspace

        workspace = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        workspace.ensure_dirs()
        base = {
            "key": "secret",
            "url": "https://example.com",
            "model": "m",
            "token_encoding": "cl100k_base",
        }
        llm = worker._build_llm(base, workspace, api_key="secret")
        assert isinstance(llm, LLM)
        assert llm._cache_path == workspace.cache_dir
        assert llm._log_dir_path == workspace.log_dir

    def test_base_cache_path_overridden(self, tmp_path: Path) -> None:
        """An attacker-controlled ``cache_path`` in base kwargs must NOT
        leak out — the per-job workspace always wins."""
        from epub_commentor.daemon.workspace import Workspace

        workspace = Workspace(job_id=1, base_dir=tmp_path / "jobs")
        workspace.ensure_dirs()
        base = {
            "key": "secret",
            "url": "https://example.com",
            "model": "m",
            "token_encoding": "cl100k_base",
            "cache_path": "/tmp/leaked",
        }
        llm = worker._build_llm(base, workspace, api_key="secret")
        assert llm._cache_path != Path("/tmp/leaked")
        assert llm._cache_path == workspace.cache_dir


class TestBuildConfig:
    def test_routes_known_fields(self) -> None:
        flags = {
            "block_size": 8,
            "target_language": "English",
            "fail_on_empty_chapter": True,
        }
        cfg, unknown = worker._build_config(flags)
        assert cfg.block_size == 8
        assert cfg.target_language == "English"
        assert cfg.fail_on_empty_chapter is True
        assert unknown == {}

    def test_unknown_fields_collected(self) -> None:
        flags = {"block_size": 4, "made_up": True, "also_bad": "x"}
        cfg, unknown = worker._build_config(flags)
        assert cfg.block_size == 4
        assert set(unknown.keys()) == {"made_up", "also_bad"}

    def test_empty_flags_returns_defaults(self) -> None:
        cfg, unknown = worker._build_config({})
        assert isinstance(cfg, CommentConfig)
        assert cfg.block_size == 6  # default
        assert unknown == {}


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestClassifyError:
    @pytest.mark.parametrize(
        "exc_cls, expected_stage",
        [
            (CommentScanFailedError, "scan"),
            (CommentInvalidJSONError, "annotate"),
            (CommentOrphanPIdError, "validate"),
            (CommentOverlapError, "validate"),
            (CommentNoParagraphsError, "extract"),
            (CommentSelectFailedError, "select"),
            (CommentReviewFailedError, "review"),
        ],
    )
    def test_maps_subclass_to_stage(self, exc_cls: type[CommentorError], expected_stage: str) -> None:
        stage, message = worker._classify_commentor_error(exc_cls("oops"))
        assert stage == expected_stage
        assert message == "oops"

    def test_generic_commentor_error_defaults_to_process(self) -> None:
        class _Other(CommentorError):
            pass

        stage, _ = worker._classify_commentor_error(_Other("custom"))
        assert stage == "process"


# ---------------------------------------------------------------------------
# save_meta
# ---------------------------------------------------------------------------


class TestSaveMeta:
    def test_writes_json_snapshot(self, tmp_path: Path) -> None:
        # Build a minimal ChapterAnnotation without going through the
        # real extract pipeline (Chapter requires path + xml_node). We
        # use ``SimpleNamespace`` for the chapter proxy since ``save_meta``
        # only touches ``.title``.
        from types import SimpleNamespace

        from epub_commentor import CommentorResult
        from epub_commentor.llm.schema import ChapterMemo

        chapter = SimpleNamespace(title="Ch 1")
        memo = ChapterMemo(
            core_thesis="test",
            outline=[],
            tone="t",
            target_audience="a",
        )
        from epub_commentor.pipeline import ChapterAnnotation

        ann = ChapterAnnotation(chapter=chapter, memo=memo, comments=[], skipped_blocks=0, has_empty_blocks=False)
        result = CommentorResult(
            output_path=Path("/jobs/job_1/output.commented.epub"),
            annotations=[ann],
            chapters_processed=1,
            chapters_skipped=0,
            chapters_filtered=0,
            blocks_skipped=0,
            total_tokens=100,
            input_tokens=80,
            input_cache_tokens=10,
            output_tokens=20,
        )
        out = tmp_path / "meta.json"
        worker.save_meta(out, result)
        assert out.exists()
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["chapters_processed"] == 1
        assert payload["total_tokens"] == 100
        # ``total_comments`` is a derived property on CommentorResult;
        # ``save_meta`` records the live value.
        assert payload["total_comments"] == 0
        assert payload["annotations"][0]["chapter_title"] == "Ch 1"
        assert payload["annotations"][0]["comment_count"] == 0

    def test_meta_round_trip_through_dump(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from epub_commentor import CommentorResult
        from epub_commentor.llm.schema import ChapterMemo
        from epub_commentor.pipeline import ChapterAnnotation

        chapter = SimpleNamespace(title="Ch 1")
        memo = ChapterMemo(core_thesis="t", outline=[], tone="t", target_audience="a")

        # Build a CommentItem to populate the annotations so total_comments > 0
        from epub_commentor.llm.schema import CommentItem, CommentKind, CommentPosition

        ci = CommentItem(target_p_ids=[0], position=CommentPosition.BEFORE, kind=CommentKind.NOTE, content="x")
        ann = ChapterAnnotation(
            chapter=chapter,
            memo=memo,
            comments=[ci, ci],
            skipped_blocks=0,
            has_empty_blocks=False,
        )
        result = CommentorResult(
            output_path=Path("/x"),
            annotations=[ann],
            chapters_processed=1,
        )
        out = tmp_path / "meta.json"
        worker.save_meta(out, result)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["total_comments"] == 2
        assert payload["output_path"] == str(Path("/x"))


# ---------------------------------------------------------------------------
# run_job — failure paths
# ---------------------------------------------------------------------------


class TestRunJobMissingKey:
    def test_marks_failed_with_api_key_stage(self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("EPUB_COMMENTOR_API_KEY", raising=False)
        job_id = db.insert_job(
            conn,
            file_name="x.epub",
            source_path=str(tmp_path / "x.epub"),
        )
        ws = tmp_path / "jobs"
        worker.run_job(
            conn,
            db.fetch_job(conn, job_id),
            base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
            workspace_root=ws,
        )
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_FAILED
        assert job.error_stage == "api_key"
        assert "EPUB_COMMENTOR_API_KEY" in (job.error_message or "")


class TestRunJobMissingInput:
    def test_marks_failed_with_extract_stage(self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        job_id = db.insert_job(
            conn,
            file_name="x.epub",
            source_path=str(tmp_path / "jobs" / "job_x" / "input.epub"),
        )
        ws = tmp_path / "jobs"
        # Don't create the input file → expect extract failure
        worker.run_job(
            conn,
            db.fetch_job(conn, job_id),
            base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
            workspace_root=ws,
        )
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_FAILED
        assert job.error_stage == "extract"


class TestRunJobRejectsInteractive:
    def test_interactive_flag_fails(self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root, flags={"interactive": True})
        worker.run_job(
            conn,
            db.fetch_job(conn, job_id),
            base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
            workspace_root=ws_root,
        )
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_FAILED
        assert job.error_stage == "flag"
        assert "interactive" in (job.error_message or "")

    def test_review_flag_fails(self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root, flags={"review": True})
        worker.run_job(
            conn,
            db.fetch_job(conn, job_id),
            base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
            workspace_root=ws_root,
        )
        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_FAILED
        assert job.error_stage == "flag"


# ---------------------------------------------------------------------------
# run_job — happy path with MockLLM
# ---------------------------------------------------------------------------


class TestRunJobHappyPath:
    def test_end_to_end_marks_success(self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")

        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root)

        # Build a MockLLM and patch ``epub_commentor.daemon.worker._build_llm``
        # so the worker's run_job uses our canned responses instead of
        # the real ``LLM(...)`` (which would try to connect to OpenAI).
        responses = {
            "scan__response": _memo_json(),
            "annotate__response": _annotations_json(),
        }
        mock_llm = MockLLM(responses_by_seed=responses, default_response=_memo_json())

        with mock.patch.object(worker, "_build_llm", return_value=mock_llm):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_SUCCESS, f"got {job.error_stage}: {job.error_message}"
        assert job.output_path is not None
        # output.commented.epub should exist on disk
        output_path = Path(job.output_path)
        assert output_path.exists()
        # meta.json should have been written
        ws = ws_root / "jobs" / f"job_{job_id}"
        assert (ws / "meta.json").exists()
        # cache/ should be cleaned up
        assert not (ws / "cache").exists()

    def test_records_token_counts(self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root)
        mock_llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": _annotations_json(),
            },
            default_response=_memo_json(),
        )
        # Mock the token counters that ``comment_epub`` reads via getattr
        mock_llm.total_tokens = 12345
        mock_llm.input_tokens = 10000
        mock_llm.output_tokens = 2000
        mock_llm.input_cache_tokens = 345

        with mock.patch.object(worker, "_build_llm", return_value=mock_llm):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_SUCCESS
        assert job.input_tokens == 10000
        assert job.output_tokens == 2000
        assert job.cache_tokens == 345


class TestRunJobRetryPolicy:
    def test_commentor_error_schedules_retry(
        self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch
    ) -> None:
        """When ``comment_epub`` raises a :class:`CommentorError`,
        ``run_job`` records the failure and re-queues the job."""
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root)

        def boom(*_a, **_kw):
            raise CommentScanFailedError("deliberate scan failure for test")

        with mock.patch.object(worker, "comment_epub", side_effect=boom):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PENDING  # re-queued for retry
        assert job.retry_count == 1
        assert job.error_stage is None  # cleared on successful re-queue
        assert job.error_message is None

    def test_scan_failure_without_flag_keeps_chapter_skipped(
        self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch
    ) -> None:
        """Default behaviour: a failing scan leaves the chapter skipped,
        the job still completes SUCCESS (no retry scheduled)."""
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root)

        bad_scan = MockLLM(
            responses_by_seed={"scan__response": "not json{{{"},
            default_response="not json{{{",
        )

        with mock.patch.object(worker, "_build_llm", return_value=bad_scan):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_SUCCESS
        assert job.chapters_skipped == 1
        assert job.chapters_processed == 0
        assert job.retry_count == 0

    def test_exhausted_retries_leave_failed(
        self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch
    ) -> None:
        """Once ``retry_count == max_retries``, a further failure stays FAILED."""
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root, max_retries=0)

        def boom(*_a, **_kw):
            raise CommentScanFailedError("persistent failure")

        with mock.patch.object(worker, "comment_epub", side_effect=boom):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_FAILED
        assert job.retry_count == 0
        assert job.error_stage == "scan"

    def test_abort_error_cancels_without_retry(
        self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch
    ) -> None:
        """``CommentAbortError`` is treated as cancellation, not failure."""
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root)

        def boom(*_a, **_kw):
            raise CommentAbortError("user cancelled")

        with mock.patch.object(worker, "comment_epub", side_effect=boom):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_CANCELLED
        assert job.retry_count == 0

    def test_unhandled_exception_marks_unhandled(
        self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch
    ) -> None:
        """A non-:class:`CommentorError` exception is captured as unhandled."""
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root)

        def boom(*_a, **_kw):
            raise RuntimeError("something exploded")

        with mock.patch.object(worker, "comment_epub", side_effect=boom):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_PENDING  # re-queued (default max_retries=3)
        assert job.error_stage is None

    def test_abort_marks_cancelled_no_retry(self, conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("EPUB_COMMENTOR_API_KEY", "fake")
        ws_root = tmp_path / "jobs"
        job_id = _make_job_with_input(conn, workspace_root=ws_root)

        mock_llm = MockLLM(default_response=_memo_json())

        # Force comment_epub to raise CommentAbortError

        def fake_comment_epub(*args, **kwargs):
            raise CommentAbortError()

        with mock.patch.object(worker, "_build_llm", return_value=mock_llm), mock.patch(
            "epub_commentor.daemon.worker.comment_epub", side_effect=fake_comment_epub
        ):
            worker.run_job(
                conn,
                db.fetch_job(conn, job_id),
                base_llm_kwargs={"url": "x", "model": "m", "token_encoding": "cl100k_base"},
                workspace_root=ws_root,
            )

        job = db.fetch_job(conn, job_id)
        assert job is not None
        assert job.status == db.STATUS_CANCELLED
        # Cancelled jobs are terminal — no retry scheduled.
        assert job.retry_count == 0
