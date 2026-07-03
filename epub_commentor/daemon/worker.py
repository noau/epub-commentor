"""Single-threaded worker: run one job end-to-end, persist the result.

The worker is deliberately thin — it composes the existing
:func:`~epub_commentor.commentor.comment_epub` API rather than
re-implementing the pipeline. Per-job isolation comes from passing
``cache_path`` and ``log_dir_path`` to :class:`~epub_commentor.llm.LLM`
and ``output`` to ``comment_epub`` (no shared state between jobs).

Stages and exception mapping
----------------------------
:class:`~epub_commentor.errors.CommentorError` subclasses map onto
specific lifecycle stages so :func:`epub_commentor.daemon.db.mark_failed`
records an actionable ``error_stage``. Anything else (uncaught
``OSError``, ``KeyError``, …) becomes ``error_stage='unhandled'``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import (
    LLM,
    CommentAbortError,
    CommentConfig,
    CommentorError,
    CommentorResult,
    comment_epub,
    make_default_progress_callback,
)
from ..errors import (
    CommentInvalidJSONError,
    CommentNoParagraphsError,
    CommentOrphanPIdError,
    CommentOverlapError,
    CommentReviewFailedError,
    CommentScanFailedError,
    CommentSelectFailedError,
)
from ..llm._api_key import resolve_api_key
from . import db
from .workspace import Workspace, jobs_root

_logger = logging.getLogger(__name__)

# These keys live in ``CommentConfig`` — they are routed to the
# CommentConfig constructor instead of LLM.__init__ when ``run_job``
# merges the per-job flags.
_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "position",
        "kinds",
        "block_size",
        "max_json_retries",
        "max_scan_retries",
        "concurrency",
        "cache_seed_user_id",
        "book_synopsis",
        "inject_css",
        "css_path_in_epub",
        "target_language",
        "fail_on_empty_chapter",
        "fail_on_block_error",
        "skip_chapter_on_empty_annotation",
        "ai_select_min_body_chars",
        "ai_review_min_comments_per_chapter",
        "ai_select_max_retries",
        "ai_review_max_retries",
    }
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_job(
    conn: sqlite3.Connection,
    job: db.Job,
    *,
    base_llm_kwargs: Mapping[str, Any],
    workspace_root: Path,
) -> None:
    """Run a single job and persist the outcome.

    On any exception path this function **always** mutates the ``jobs``
    row before returning (FAILED / CANCELLED / unhandled) so the worker
    loop never has to clean up zombie PROCESSING rows itself.
    """
    workspace = Workspace(job_id=job.id, base_dir=jobs_root(workspace_root))
    workspace.ensure_dirs()

    # ---- Resolve API key ----
    raw_key = base_llm_kwargs.get("key")
    try:
        api_key = resolve_api_key(raw_key)
    except Exception as exc:  # pragma: no cover - defensive
        api_key = None
        _logger.warning("resolve_api_key raised %s: %s", type(exc).__name__, exc)
    if not api_key:
        db.mark_failed(
            conn,
            job.id,
            stage="api_key",
            message="$EPUB_COMMENTOR_API_KEY (or format.json 'key') is not set",
        )
        return

    # ---- Construct LLM + CommentConfig ----
    try:
        llm = _build_llm(base_llm_kwargs, workspace, api_key)
        config, flag_overrides = _build_config(job.flags)
    except TypeError as exc:
        # Bad field name or wrong type passed through from format.json.
        db.mark_failed(conn, job.id, stage="config", message=f"{type(exc).__name__}: {exc}")
        return
    except Exception as exc:
        db.mark_failed(conn, job.id, stage="config", message=f"{type(exc).__name__}: {exc}")
        return

    # ---- Construct filters (AI select / review only — interactive
    #      modes are rejected in a daemon because they require a TTY) ----
    chapter_filter = _build_ai_chapter_filter(job.flags, llm, config)
    annotation_filter = _build_ai_annotation_filter(job.flags, llm, config)

    # Reject interactive flags explicitly so the user gets a loud
    # failure rather than a silent no-op.
    if job.flags.get("interactive"):
        db.mark_failed(
            conn,
            job.id,
            stage="flag",
            message="-i/--interactive is not supported in the daemon",
        )
        return
    if job.flags.get("review"):
        db.mark_failed(
            conn,
            job.id,
            stage="flag",
            message="--review is not supported in the daemon (use --ai-review or --no-review)",
        )
        return

    # ---- Pre-flight: input exists? ----
    if not workspace.input_epub.exists():
        db.mark_failed(
            conn,
            job.id,
            stage="extract",
            message=f"input epub missing: {workspace.input_epub}",
        )
        return

    # ---- Run ----
    db.mark_processing(conn, job.id)

    progress_callback = make_default_progress_callback(quiet=True)
    try:
        result = comment_epub(
            source=str(workspace.input_epub),
            output=str(workspace.output_epub),
            llm=llm,
            config=config,
            progress_callback=progress_callback,
            chapter_filter=chapter_filter,
            annotation_filter=annotation_filter,
        )
    except CommentAbortError:
        db.mark_cancelled(conn, job.id)
        return
    except CommentorError as exc:
        stage, message = _classify_commentor_error(exc)
        db.mark_failed(conn, job.id, stage=stage, message=message)
        _maybe_schedule_retry(conn, job.id)
        return
    except Exception as exc:
        _logger.exception("job %d crashed (unhandled)", job.id)
        db.mark_failed(conn, job.id, stage="unhandled", message=f"{type(exc).__name__}: {exc}")
        _maybe_schedule_retry(conn, job.id)
        return

    # ---- Success: persist meta + cleanup ----
    save_meta(workspace.meta_json, result)
    db.mark_success(
        conn,
        job.id,
        output_path=str(workspace.output_epub),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_tokens=result.input_cache_tokens,
        chapters_processed=result.chapters_processed,
        chapters_skipped=result.chapters_skipped,
        total_comments=result.total_comments,
    )
    try:
        workspace.cleanup_cache()
        workspace.archive_logs()
    except OSError as exc:
        # Cleanup is best-effort; never let a stray rm error mask the
        # success path. Log loudly so the operator notices.
        _logger.warning("post-success cleanup failed for job %d: %s", job.id, exc)

    _logger.info(
        "job %d SUCCESS: %d chapters, %d comments, %d input / %d output / %d cache tokens",
        job.id,
        result.chapters_processed,
        result.total_comments,
        result.input_tokens,
        result.output_tokens,
        result.input_cache_tokens,
    )


# ---------------------------------------------------------------------------
# LLM / CommentConfig / filter construction
# ---------------------------------------------------------------------------


def _build_llm(base_kwargs: Mapping[str, Any], workspace: Workspace, api_key: str) -> LLM:
    """Build :class:`LLM` with per-job cache / log directories.

    ``cache_path`` and ``log_dir_path`` are *always* overridden by the
    workspace's paths — every other LLM kwarg comes from the daemon's
    ``format.json`` (``base_kwargs``).

    Unknown keys (``concurrency``, ``block_size``, ``target_language``,
    …) are filtered out via :func:`epub_commentor.cli._split_format_config`
    so a stray ``format.json`` field never crashes the job with
    ``LLM.__init__() got an unexpected keyword argument``.
    """
    # Defensive import — the CLI module pulls in rich-selector etc. on
    # load so we keep this lazy.
    from ..cli import _split_format_config

    raw = dict(base_kwargs)
    raw.pop("cache_path", None)
    raw.pop("log_dir_path", None)
    llm_kwargs, _config_kwargs, unknown = _split_format_config(raw)
    if unknown:
        _logger.warning(
            "format.json: ignoring unknown LLM keys %s (typo? See format.template.json)",
            unknown,
        )
    llm_kwargs["cache_path"] = str(workspace.cache_dir)
    llm_kwargs["log_dir_path"] = str(workspace.log_dir)
    llm_kwargs["key"] = api_key
    return LLM(**llm_kwargs)


def _build_config(flags: Mapping[str, Any]) -> tuple[CommentConfig, dict[str, Any]]:
    """Construct a :class:`CommentConfig` from the job's ``flags`` mapping.

    Returns a tuple ``(config, unrecognised)`` so the caller can surface
    typos in the operator's JSON later (Phase 3 polish). Unrecognised
    keys are not passed to ``CommentConfig`` so an unknown flag never
    crashes the job.
    """
    kwargs: dict[str, Any] = {}
    unrecognised: dict[str, Any] = {}
    for key, value in flags.items():
        if key in _CONFIG_FIELDS:
            kwargs[key] = value
        else:
            unrecognised[key] = value
    return CommentConfig(**kwargs), unrecognised


def _build_ai_chapter_filter(flags: Mapping[str, Any], llm: LLM, config: CommentConfig):
    """Build the AI chapter filter if ``ai_select`` is set.

    Delegates to the CLI factory so the behaviour stays identical to
    ``--ai-select`` on the command line. Re-imported lazily to keep the
    worker module importable even when ``rich-selector`` is absent
    (the AI select path does not need it, but the import graph does).
    """
    if not flags.get("ai_select"):
        return None
    from ..cli import _build_ai_chapter_filter as _factory

    fake_args = argparse.Namespace(ai_select=True)
    return _factory(fake_args, llm, config)


def _build_ai_annotation_filter(flags: Mapping[str, Any], llm: LLM, config: CommentConfig):
    """Build the AI annotation filter if ``ai_review`` is set."""
    if not flags.get("ai_review"):
        return None
    from ..cli import _build_ai_annotation_filter as _factory

    fake_args = argparse.Namespace(ai_review=True)
    return _factory(fake_args, llm, config)


# ---------------------------------------------------------------------------
# Error classification + retry policy
# ---------------------------------------------------------------------------


def _classify_commentor_error(exc: CommentorError) -> tuple[str, str]:
    """Map a :class:`CommentorError` subclass onto ``(error_stage, message)``."""
    stage = "process"
    if isinstance(exc, CommentScanFailedError):
        stage = "scan"
    elif isinstance(exc, CommentInvalidJSONError):
        stage = "annotate"
    elif isinstance(exc, (CommentOrphanPIdError, CommentOverlapError)):
        stage = "validate"
    elif isinstance(exc, CommentNoParagraphsError):
        stage = "extract"
    elif isinstance(exc, CommentSelectFailedError):
        stage = "select"
    elif isinstance(exc, CommentReviewFailedError):
        stage = "review"
    return stage, str(exc)


def _maybe_schedule_retry(conn: sqlite3.Connection, job_id: int) -> None:
    """Re-queue a FAILED job if it still has retry budget left.

    When the budget is exhausted, the row stays FAILED so the operator
    sees it in ``epubctl status`` and can intervene.
    """
    scheduled = db.increment_retry(conn, job_id)
    if not scheduled:
        _logger.warning("job %d exhausted retry budget; leaving FAILED", job_id)
    else:
        _logger.info("job %d re-queued for retry", job_id)


# ---------------------------------------------------------------------------
# CommentorResult persistence
# ---------------------------------------------------------------------------


def save_meta(path: Path, result: CommentorResult) -> None:
    """Write a JSON snapshot of :class:`CommentorResult` to ``path``.

    ``annotations`` are converted via ``dataclasses.asdict``; paths are
    serialised as strings so the file is portable across processes.
    """
    payload = {
        "output_path": str(result.output_path),
        "chapters_processed": result.chapters_processed,
        "chapters_skipped": result.chapters_skipped,
        "chapters_filtered": result.chapters_filtered,
        "blocks_skipped": result.blocks_skipped,
        "total_tokens": result.total_tokens,
        "input_tokens": result.input_tokens,
        "input_cache_tokens": result.input_cache_tokens,
        "output_tokens": result.output_tokens,
        "total_comments": result.total_comments,
        "processed_titles": list(result.processed_titles),
        "ai_select_decisions": (
            {str(k): list(v) for k, v in result.ai_select_decisions.items()}
            if result.ai_select_decisions
            else None
        ),
        "ai_review_decisions": (
            {str(k): list(v) for k, v in result.ai_review_decisions.items()}
            if result.ai_review_decisions
            else None
        ),
        "annotations": [
            {
                "chapter_title": ann.chapter.title,
                "core_thesis": ann.memo.core_thesis,
                "comment_count": len(ann.comments),
                "skipped_blocks": ann.skipped_blocks,
                "has_empty_blocks": ann.has_empty_blocks,
            }
            for ann in result.annotations
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ``dataclasses`` is imported above for symmetry; suppress unused warnings.
_ = dataclasses  # noqa: F841 - imported for completeness
