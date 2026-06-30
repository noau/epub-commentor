"""Top-level orchestration entry point for ``epub-commentor``.

This module wires the three pipeline layers together so callers (CLI,
scripts, third-party tools) can run a full annotation pass with a single
function call.

Public surface:

- :class:`CommentorResult` — a small dataclass reporting token usage and
  per-chapter counts so the CLI can render a tidy summary.
- :func:`comment_epub` — open the source EPUB, run Stage 1 + Stage 2
  against every chapter, splice the resulting asides and ``commentary.css``
  into the target EPUB, and close it.

The function accepts any :class:`~epub_commentor.llm.protocol.LLMProtocol`
implementation (the real :class:`epub_commentor.llm.LLM` or any test
double), so unit tests can drive the full stack without an OpenAI call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import CommentConfig
from .epub.zip import Zip
from .llm.protocol import LLMProtocol
from .pipeline import ChapterAnnotation, extract_chapters, inject_annotations, process_chapters

_logger = logging.getLogger(__name__)

# Optional progress hook signature: ``callback(stage: str, current: int, total: int)``.
ProgressCallback = Callable[[str, int, int], None]


@dataclass
class CommentorResult:
    """Summary of one full ``comment_epub`` run.

    Token counters reflect whatever the supplied LLM instance reports at
    the end of the run. Chapter counts distinguish chapters that produced
    at least one comment from those that were skipped (zero ``<p>``
    elements, or chapters whose scan failed after all retries).
    """

    output_path: Path
    annotations: list[ChapterAnnotation] = field(default_factory=list)
    chapters_processed: int = 0
    chapters_skipped: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    input_cache_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_comments(self) -> int:
        return sum(len(ann.comments) for ann in self.annotations)


def _default_output_path(source: Path) -> Path:
    """Compute the default output EPUB path next to the source file.

    The convention is to append ``.commented`` before the extension so a
    re-run on the same source never clobbers the original.
    """
    if source.suffix.lower() == ".epub":
        return source.with_name(source.stem + ".commented.epub")
    return source.with_name(source.name + ".commented.epub")


def _emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    current: int,
    total: int,
) -> None:
    """Fire a progress callback if one was supplied; swallow handler errors.

    A buggy progress hook should never crash the whole annotation run,
    so any exception from the callback is logged at WARNING and
    otherwise ignored.
    """
    if callback is None:
        return
    try:
        callback(stage, current, total)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("progress callback raised %s: %s", type(exc).__name__, exc)


def _count_chapters(annotations: list[ChapterAnnotation]) -> tuple[int, int]:
    """Split the annotation list into (processed, skipped) buckets.

    A chapter counts as processed if its memo is anything other than the
    placeholder produced for empty chapters (see
    :func:`epub_commentor.pipeline.process._empty_memo`). The placeholder
    memo's ``core_thesis`` always starts with ``"(chapter skipped"``.
    """
    processed = 0
    skipped = 0
    for ann in annotations:
        if ann.memo.core_thesis.startswith("(chapter skipped"):
            skipped += 1
        else:
            processed += 1
    return processed, skipped


def comment_epub(
    source: Path | str,
    output: Path | str | None = None,
    *,
    llm: LLMProtocol,
    config: CommentConfig | None = None,
    progress_callback: ProgressCallback | None = None,
) -> CommentorResult:
    """Run the full extract → process → inject pipeline on ``source``.

    Parameters
    ----------
    source:
        Path to the source EPUB. The file is read but never modified;
        a fresh target EPUB is always written next to it (or to
        ``output`` when supplied).
    output:
        Where the annotated EPUB is written. ``None`` writes to
        ``<stem>.commented.epub`` in the same directory.
    llm:
        Any :class:`LLMProtocol` (the production :class:`LLM` or a
        :class:`~tests._mock_llm.MockLLM`). The instance's token counters
        are read after the run completes.
    config:
        Pipeline configuration. ``None`` uses :class:`CommentConfig`'s
        defaults.
    progress_callback:
        Optional ``(stage, current, total)`` hook for CLI progress
        bars. Stages fired (in order): ``"extract"``, ``"process"``,
        ``"inject"``.

    Returns
    -------
    :class:`CommentorResult`
        Token usage + per-chapter counts. The ``annotations`` field is
        the same in-memory list the pipeline produced; mutating it does
        not retroactively affect the output file.
    """
    cfg = config or CommentConfig()
    source_path = Path(source).resolve()
    target_path = Path(output).resolve() if output is not None else _default_output_path(source_path)

    _emit_progress(progress_callback, "extract", 0, 1)

    with Zip(source_path, target_path) as z:
        chapters, book_metadata = extract_chapters(z)
        _emit_progress(progress_callback, "extract", 1, 1)

        total = max(len(chapters), 1)
        _emit_progress(progress_callback, "process", 0, total)

        # The pipeline mutates chapter bodies in place; we surface a
        # 1-of-N progress hint before and after process so the CLI can
        # render a per-chapter bar.
        annotations = process_chapters(
            chapters=chapters,
            book_metadata=book_metadata,
            llm=llm,
            config=cfg,
        )
        _emit_progress(progress_callback, "process", total, total)

        _emit_progress(progress_callback, "inject", 0, 1)
        inject_annotations(zip=z, annotations=annotations, config=cfg, book_metadata=book_metadata)
        _emit_progress(progress_callback, "inject", 1, 1)

    chapters_processed, chapters_skipped = _count_chapters(annotations)

    # Read the live token counters off the LLM if it exposes them. The
    # protocol only requires template() + context(), but the real LLM
    # class — and any test double that mimics it — has these properties.
    total_tokens = getattr(llm, "total_tokens", 0)
    input_tokens = getattr(llm, "input_tokens", 0)
    input_cache_tokens = getattr(llm, "input_cache_tokens", 0)
    output_tokens = getattr(llm, "output_tokens", 0)

    return CommentorResult(
        output_path=target_path,
        annotations=annotations,
        chapters_processed=chapters_processed,
        chapters_skipped=chapters_skipped,
        total_tokens=total_tokens,
        input_tokens=input_tokens,
        input_cache_tokens=input_cache_tokens,
        output_tokens=output_tokens,
    )


__all__ = ["CommentorResult", "ProgressCallback", "comment_epub"]
