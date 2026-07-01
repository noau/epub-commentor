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
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import CommentConfig
from .epub.zip import Zip
from .llm.protocol import LLMProtocol
from .pipeline import ChapterAnnotation, ChapterFilter, extract_chapters, inject_annotations, process_chapters
from .progress import ProgressCallback, ProgressEvent

_logger = logging.getLogger(__name__)

# The placeholder memo emitted for empty / scan-failed chapters (see
# ``epub_commentor.pipeline.process._empty_memo``) always starts its
# ``core_thesis`` with this prefix. It's the single source of truth for
# telling a processed chapter from a skipped one.
_SKIPPED_PREFIX = "(chapter skipped"


def _is_chapter_skipped(ann: ChapterAnnotation) -> bool:
    """True iff ``ann`` is the placeholder produced for a skipped chapter."""
    return ann.memo.core_thesis.startswith(_SKIPPED_PREFIX)


# Re-exported for callers that prefer importing from ``epub_commentor``.
__all__ = ["ChapterFilter", "CommentorResult", "ProgressCallback", "comment_epub"]


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

    @property
    def processed_titles(self) -> list[str]:
        """Titles of the chapters that were actually annotated (not skipped)."""
        return [ann.chapter.title for ann in self.annotations if not _is_chapter_skipped(ann)]


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
    event: ProgressEvent,
) -> None:
    """Fire a progress callback if one was supplied; swallow handler errors.

    A buggy progress hook should never crash the whole annotation run,
    so any exception from the callback is logged at WARNING and
    otherwise ignored.
    """
    if callback is None:
        return
    try:
        callback(event)
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
        if _is_chapter_skipped(ann):
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
    chapter_filter: ChapterFilter | None = None,
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
        Optional :class:`~epub_commentor.progress.ProgressEvent` hook
        for the long-running LLM phase. Events are emitted with
        ``stage="process"`` and ``substage="scan"`` /
        ``substage="annotate"`` only. The ``extract`` and ``inject``
        stages are short enough that ``commentor.py`` prints single
        status lines to stderr directly; they do not flow through
        this callback. The first event fires after any
        ``chapter_filter`` has returned, so interactive filters
        (e.g. rich-selector) never share terminal ownership with the
        progress renderer.
    chapter_filter:
        Optional :data:`~epub_commentor.pipeline.extract.ChapterFilter`
        callback invoked between :func:`extract_chapters` and
        :func:`process_chapters`. Receives the spine-ordered chapter
        list and returns a parallel ``list[bool]`` mask — ``True`` keeps
        the chapter, ``False`` drops it. Dropped chapters are never
        passed to the LLM stage; their bytes flow through
        :meth:`Zip.__exit__` as-is. ``None`` keeps every chapter.

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

    print("Extracting chapters...", file=sys.stderr)
    with Zip(source_path, target_path) as z:
        chapters, book_metadata = extract_chapters(z)
        print(f"Extracted {len(chapters)} chapter(s).", file=sys.stderr)

        # Optional user-supplied chapter filter: returns a parallel bool mask
        # where True[i] keeps chapter i and False[i] drops it from the run.
        # Dropped chapters never reach process_chapters; their source bytes
        # flow through Zip.__exit__ as-is, so no restore step is needed.
        if chapter_filter is not None:
            mask = chapter_filter(list(chapters))
            if not isinstance(mask, list) or len(mask) != len(chapters) or not all(isinstance(x, bool) for x in mask):
                _logger.warning(
                    "chapter_filter returned an invalid mask (got %s of length %s; expected list[bool] of length %d)",
                    type(mask).__name__,
                    len(mask) if isinstance(mask, list) else "<not a list>",
                    len(chapters),
                )
                raise ValueError(
                    f"chapter_filter must return a parallel list[bool] of length {len(chapters)}; "
                    f"got {type(mask).__name__} of length {len(mask) if isinstance(mask, list) else 'n/a'}"
                )
            chapters = [ch for ch, keep in zip(chapters, mask) if keep]

        total = max(len(chapters), 1)
        _emit_progress(progress_callback, ProgressEvent(stage="process", current=0, total=total, substage="scan"))

        # The pipeline mutates chapter bodies in place; it emits its own
        # sub-stage events (process/scan + process/annotate) when a
        # callback is supplied.
        annotations = process_chapters(
            chapters=chapters,
            book_metadata=book_metadata,
            llm=llm,
            config=cfg,
            progress_callback=progress_callback,
        )
        _emit_progress(progress_callback, ProgressEvent(stage="process", current=total, total=total, substage="scan"))

        print("Injecting annotations...", file=sys.stderr)
        inject_annotations(zip=z, annotations=annotations, config=cfg, book_metadata=book_metadata)
        print("Injection complete.", file=sys.stderr)

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
