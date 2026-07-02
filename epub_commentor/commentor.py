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
from .pipeline import (
    AnnotationFilter,
    ChapterAnnotation,
    ChapterFilter,
    extract_chapters,
    inject_annotations,
    process_chapters,
)
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
    ``chapters_filtered`` counts annotations dropped by the optional
    post-process review gate (e.g. ``--review`` CLI flag); it is 0 when
    no filter was applied or the filter accepted everything.
    """

    output_path: Path
    annotations: list[ChapterAnnotation] = field(default_factory=list)
    chapters_processed: int = 0
    chapters_skipped: int = 0
    chapters_filtered: int = 0
    blocks_skipped: int = 0
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


def _review_gate(
    annotations: list[ChapterAnnotation],
    annotation_filter: AnnotationFilter | None,
    progress_callback: ProgressCallback | None,
) -> list[ChapterAnnotation]:
    """Apply the optional :data:`AnnotationFilter` to ``annotations``.

    Returns ``annotations`` unchanged when:

    - ``annotations`` is empty (nothing to filter), or
    - ``annotation_filter`` is ``None`` (caller opted out — e.g. via
      ``--no-review`` or a non-TTY fallback).

    Otherwise, the filter is invoked and its mask is applied. Smart-
    trigger logic (e.g. "skip the picker when no blocks were skipped")
    lives inside the filter closure built by the CLI; this helper is
    deliberately policy-free.

    Progress lifecycle
    ------------------
    When the filter is about to open an interactive UI (e.g. rich-
    selector), the live Rich progress bar (if any) is closed first so
    that two Live regions don't share terminal ownership. The CLI's
    ``finally`` block calls ``close()`` again — it's idempotent, so the
    double-close is a no-op.

    Mask contract
    -------------
    The returned mask must be a ``list[bool]`` with one entry per
    annotation, in input order. ``True`` keeps the annotation,
    ``False`` drops it. Violations raise :class:`ValueError` (mirroring
    the :data:`ChapterFilter` contract at lines 197-207) so library
    users see a clear traceback instead of silent corruption.
    """
    if not annotations or annotation_filter is None:
        return annotations

    # Close the live Rich progress bar before opening the selector. The
    # attribute probing matches the CLI's finally-block pattern (cli.py:520)
    # so plain-callable progress hooks (no `__self__`) still work.
    if progress_callback is not None and hasattr(progress_callback, "__self__"):
        closer = getattr(progress_callback.__self__, "close", None)  # type: ignore[attr-defined]
        if callable(closer):
            closer()

    mask = annotation_filter(annotations)
    if not isinstance(mask, list) or len(mask) != len(annotations) or not all(isinstance(x, bool) for x in mask):
        _logger.warning(
            "annotation_filter returned an invalid mask (got %s of length %s; expected list[bool] of length %d)",
            type(mask).__name__,
            len(mask) if isinstance(mask, list) else "<not a list>",
            len(annotations),
        )
        raise ValueError(
            f"annotation_filter must return a parallel list[bool] of length "
            f"{len(annotations)}; got {type(mask).__name__} of length "
            f"{len(mask) if isinstance(mask, list) else 'n/a'}"
        )
    return [a for a, keep in zip(annotations, mask) if keep]


def comment_epub(
    source: Path | str,
    output: Path | str | None = None,
    *,
    llm: LLMProtocol,
    config: CommentConfig | None = None,
    progress_callback: ProgressCallback | None = None,
    chapter_filter: ChapterFilter | None = None,
    annotation_filter: AnnotationFilter | None = None,
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
    annotation_filter:
        Optional :data:`~epub_commentor.pipeline.process.AnnotationFilter`
        callback invoked between :func:`process_chapters` and
        :func:`inject_annotations`. Receives the per-chapter
        ``ChapterAnnotation`` list and returns a parallel ``list[bool]``
        mask — ``True`` keeps the annotation (it gets injected),
        ``False`` drops it (the original chapter is written through
        ``Zip.__exit__`` as-is). ``None`` injects everything
        unconditionally. The CLI wires this to ``--review`` /
        ``--no-review``; library users can supply their own selection
        logic (e.g. "drop every annotation with skipped_blocks > 0").

    Returns
    -------
    :class:`CommentorResult`
        Token usage + per-chapter counts. The ``annotations`` field is
        the same in-memory list the pipeline produced; mutating it does
        not retroactively affect the output file. ``chapters_filtered``
        counts annotations dropped by ``annotation_filter`` (0 when no
        filter was applied or the filter accepted everything).
    """
    cfg = config or CommentConfig()
    source_path = Path(source).resolve()
    target_path = Path(output).resolve() if output is not None else _default_output_path(source_path)

    # Install the two-stage SIGINT handler so Ctrl-C aborts the
    # long-running Stage 1 / Stage 2 batches within ~hundred ms by
    # closing in-flight httpx streams and asking the ThreadPoolExecutor
    # to skip pending work. Restored on the way out (success, abort,
    # or any other exception) so a library user calling comment_epub
    # multiple times doesn't leak signal handlers.
    from .llm._abort import install_sigint_handler, restore_sigint_handler

    install_sigint_handler()
    try:
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
            # callback is supplied. process_chapters returns
            # ``(annotations, blocks_skipped)`` — ``blocks_skipped`` counts
            # Stage 2 blocks that exhausted retries and were skipped (only
            # counted when ``config.fail_on_block_error`` is False).
            annotations, blocks_skipped = process_chapters(
                chapters=chapters,
                book_metadata=book_metadata,
                llm=llm,
                config=cfg,
                progress_callback=progress_callback,
            )
            _emit_progress(progress_callback, ProgressEvent(stage="process", current=total, total=total, substage="scan"))

            # Optional post-process review gate. Symmetric to ``chapter_filter``
            # upstream: lets the user pick which generated annotations to inject
            # based on per-chapter stats (skipped blocks, empty blocks,
            # comment counts). ``None`` injects everything; the CLI's
            # ``--review`` / ``--no-review`` flags and the smart-trigger
            # policy live inside the filter closure, not here.
            filtered_annotations = _review_gate(annotations, annotation_filter, progress_callback)
            chapters_filtered = len(annotations) - len(filtered_annotations)

            print("Injecting annotations...", file=sys.stderr)
            inject_annotations(zip=z, annotations=filtered_annotations, config=cfg, book_metadata=book_metadata)
            print("Injection complete.", file=sys.stderr)

        # ``chapters_skipped`` is computed from the *original* annotations
        # (pre-gate) because pipeline skips are a property of the input, not
        # of the user's gate choice. ``chapters_processed`` reflects what
        # actually reached injection.
        chapters_processed, chapters_skipped = _count_chapters(filtered_annotations)

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
            chapters_filtered=chapters_filtered,
            blocks_skipped=blocks_skipped,
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            input_cache_tokens=input_cache_tokens,
            output_tokens=output_tokens,
        )
    finally:
        # Always restore the previous SIGINT handler — even on abort.
        # The abort exception itself propagates out of comment_epub
        # and is caught by the CLI's `except CommentAbortError` clause
        # (cli.py:main).
        restore_sigint_handler()


__all__ = ["CommentorResult", "ProgressCallback", "comment_epub"]
