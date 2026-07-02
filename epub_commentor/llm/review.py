"""``--ai-review`` — book-level post-filter.

Drives a single LLM call per book that decides which chapters' generated
annotations should be injected into the final EPUB. Receives the per-
chapter :class:`ChapterAnnotation` list (memo + comments), returns a
parallel ``list[bool]`` mask alongside a per-index ``reason`` map that
the CLI surfaces in the post-run summary panel.

Cache naming follows the same convention as :mod:`epub_commentor.llm.memo`
(``commentor:{VERSION}:review:{user_id}:{book_hash}``), so swapping the
prompt or the user_id re-derives the whole book without colliding with
the per-chapter Stage 1 cache.

Chapters that bypassed Stage 2 (zero ``<p>`` elements or Stage 1 scan
failure — both flagged by the ``"(chapter skipped"`` memo prefix) and
chapters whose Stage 2 produced zero comments are auto-dropped with a
deterministic reason **without consulting the LLM**: there is nothing
for the LLM to judge, and including them in the prompt would burn tokens
on empty inputs.

Retry loop mirrors :func:`epub_commentor.llm.select.select_chapters`: on
a malformed JSON response, the bad turn plus a corrective user message is
appended and the LLM is asked again, up to
``config.ai_review_max_retries`` times. Per-attempt failures are logged
as ``[[StageError]]``; the final exhaustion is logged as
``[[FinalError]]`` and raised as
:class:`~epub_commentor.errors.CommentReviewFailedError`.
"""

from __future__ import annotations

import hashlib
import logging
from importlib.metadata import PackageNotFoundError, version

from pydantic import ValidationError

from ..config import CommentConfig
from ..errors import CommentReviewFailedError
from ..pipeline.process import ChapterAnnotation
from ..utils import normalize_whitespace
from .protocol import LLMProtocol
from .schema import AnnotationSelectionBatch, ChapterMemo
from .types import Message, MessageRole

try:
    _VERSION = version("epub-commentor")
except PackageNotFoundError:
    _VERSION = "0.0.0-dev"

_logger = logging.getLogger(__name__)

# Local copies so a stray reserved metadata key from the upstream pipeline
# never leaks into the LLM prompt.
_RESERVED_METADATA_KEYS = frozenset({"__opf_path__"})

# Mirrors the constant in :mod:`epub_commentor.commentor` — chapters
# whose Stage 1 produced the placeholder memo carry this prefix on
# ``core_thesis``.
_SKIPPED_PREFIX = "(chapter skipped"


def _book_hash_from_annotations(annotations: list[ChapterAnnotation]) -> str:
    """SHA-1 over sorted chapter paths, first 12 hex chars.

    Identical shape to :func:`epub_commentor.llm.select._book_hash` but
    takes the annotation list (which carries ``annotation.chapter``).
    12 hex chars gives 48 bits of entropy, sufficient for personal
    libraries of tens of thousands of books.
    """
    joined = "|".join(sorted(a.chapter.path.as_posix() for a in annotations))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _review_seed(config: CommentConfig, book_hash: str) -> str:
    return f"commentor:{_VERSION}:review:{config.cache_seed_user_id}:{book_hash}"


def _memo_summary(memo: ChapterMemo, max_chars: int) -> str:
    """Compact rendering of a chapter memo for the review prompt.

    Concatenates the thesis with the outline bullets; truncates to
    ``max_chars`` so a verbose memo doesn't blow up the prompt.
    """
    parts: list[str] = [memo.core_thesis.strip()]
    if memo.outline:
        parts.append("Outline: " + "; ".join(memo.outline))
    text = " | ".join(p for p in parts if p)
    return normalize_whitespace(text)[:max_chars]


def _comment_snippet(content: str, max_chars: int) -> str:
    return normalize_whitespace(content)[:max_chars]


def _is_skipped_memo(memo: ChapterMemo) -> bool:
    return memo.core_thesis.startswith(_SKIPPED_PREFIX)


def _format_review_user(
    annotations: list[ChapterAnnotation],
    book_metadata: dict[str, str],
) -> str:
    safe_meta = {k: v for k, v in book_metadata.items() if k not in _RESERVED_METADATA_KEYS}
    meta_lines = "\n".join(f"- {k}: {v}" for k, v in safe_meta.items()) or "(none)"
    blocks: list[str] = []
    for i, ann in enumerate(annotations):
        memo_line = _memo_summary(ann.memo, max_chars=200)
        if ann.comments:
            comment_lines = "\n".join(
                f"  - kind={c.kind.value} | {_comment_snippet(c.content, max_chars=120)}" for c in ann.comments
            )
        else:
            comment_lines = "  (no comments)"
        blocks.append(f"{i}. {ann.chapter.title}\n   memo: {memo_line}\n   comments:\n{comment_lines}")
    chapters_block = "\n\n".join(blocks)
    return f"Book metadata:\n{meta_lines}\n\nChapters ({len(annotations)} total, spine-ordered):\n\n{chapters_block}"


def _raw_excerpt(raw: str, limit: int = 400) -> str:
    return raw[:limit] + ("…" if len(raw) > limit else "")


def _format_validation_error(error: Exception, raw: str) -> str:
    raw_excerpt = _raw_excerpt(raw)
    err_text = str(error)
    prefix = "Value error, "
    if err_text.startswith(prefix):
        err_text = err_text[len(prefix) :]
    return (
        "Your previous response could not be parsed as valid AnnotationSelectionBatch JSON.\n"
        f"Error: {err_text}\n\n"
        "Raw response (truncated):\n"
        f"```\n{raw_excerpt}\n```\n\n"
        "Please reply with ONLY the corrected JSON object conforming to the schema. "
        "Return one entry per chapter in the input list, with `chapter_index` matching "
        "the input position (0, 1, 2, ...), in ascending order, with no duplicates."
    )


def review_annotations(
    annotations: list[ChapterAnnotation],
    book_metadata: dict[str, str],
    llm: LLMProtocol,
    config: CommentConfig,
) -> tuple[list[bool], dict[int, str]]:
    """Decide which chapters' annotations should be injected.

    Returns ``(mask, reasons)`` where ``mask[i]`` is ``True`` if the
    chapter at spine position ``i`` should be kept (its annotations are
    injected) and ``reasons[i]`` explains the verdict for the CLI
    summary panel.

    A single LLM call drives the decision for the whole book; the cache
    seed is namespaced under ``:review:`` so it cannot collide with
    Stage 1 (``scan``), Stage 2 (``annotate``), or the pre-filter
    (``select``) cache entries.

    Chapters whose memo starts with the placeholder prefix (skipped
    chapters) or whose ``comments`` list is empty are auto-dropped with
    a deterministic reason and excluded from the LLM prompt. The returned
    mask always has exactly ``len(annotations)`` entries, so the caller's
    downstream pipeline walks it in spine order without re-indexing.

    Raises :class:`~epub_commentor.errors.CommentReviewFailedError` after
    ``config.ai_review_max_retries`` attempts at producing a valid
    :class:`~epub_commentor.llm.schema.AnnotationSelectionBatch`.
    """
    if not annotations:
        return [], {}

    book_hash = _book_hash_from_annotations(annotations)
    seed = _review_seed(config, book_hash)
    max_retries = max(1, int(getattr(config, "ai_review_max_retries", 3) or 3))
    min_comments = max(1, int(getattr(config, "ai_review_min_comments_per_chapter", 1) or 1))

    # Pre-classify each chapter: skip (placeholder / few comments) vs. consult.
    auto_drop_reasons: dict[int, str] = {}
    consult_indices: list[int] = []
    for i, ann in enumerate(annotations):
        if _is_skipped_memo(ann.memo):
            auto_drop_reasons[i] = "chapter skipped at Stage 1 (no <p> or scan failed)"
        elif len(ann.comments) < min_comments:
            auto_drop_reasons[i] = (
                f"chapter produced {len(ann.comments)} comment(s) after Stage 2; "
                f"below ai_review_min_comments_per_chapter={min_comments}"
            )
        else:
            consult_indices.append(i)

    mask = [False] * len(annotations)
    reasons: dict[int, str] = {}
    for i, reason in auto_drop_reasons.items():
        mask[i] = False
        reasons[i] = reason

    if not consult_indices:
        # Nothing for the LLM to judge — skip the network round-trip.
        return mask, reasons

    system_text = llm.template("review").render(
        target_language=config.target_language,
        book_synopsis=config.book_synopsis or "(none)",
    )

    # Build the user prompt from the consultation subset so the LLM sees
    # only chapters it actually needs to judge; we keep its indices
    # consistent with the original spine position so the verdict map
    # back-translates cleanly.
    consult_annotations = [annotations[i] for i in consult_indices]
    user_text = _format_review_user(consult_annotations, book_metadata)

    messages: list[Message] = [
        Message(MessageRole.SYSTEM, system_text),
        Message(MessageRole.USER, user_text),
    ]
    last_error: Exception | None = None

    with llm.context(cache_seed_content=seed) as ctx:
        for retry in range(max_retries):
            raw = ctx.request(messages)
            try:
                parsed = AnnotationSelectionBatch.model_validate_json(raw)
                if len(parsed.selections) != len(consult_indices):
                    raise ValueError(
                        f"AnnotationSelectionBatch length mismatch: got {len(parsed.selections)} "
                        f"selections for {len(consult_indices)} consulted chapters"
                    )
                # Map the consulted subset's selections back into the
                # spine-order mask; the auto-drop entries are already set.
                for sel, consult_idx in zip(parsed.selections, consult_indices):
                    mask[consult_idx] = sel.include
                    reasons[consult_idx] = sel.reason
                return mask, reasons
            except (ValidationError, ValueError) as exc:
                last_error = exc
                # Drop this invalid response from the cache: it would
                # otherwise be committed at __exit__ and replayed on
                # any subsequent run that re-enters this exact input.
                ctx.discard_last()
                if ctx.logger is not None:
                    ctx.logger.warning(
                        f"[[StageError]] stage=review; "
                        f"attempt={retry + 1}/{max_retries}; "
                        f"error={type(exc).__name__}: {exc}\n"
                        f"Raw excerpt:\n{_raw_excerpt(raw)}\n"
                    )
                if retry == max_retries - 1:
                    break
                messages.append(Message(MessageRole.ASSISTANT, raw))
                messages.append(Message(MessageRole.USER, _format_validation_error(exc, raw)))

    assert last_error is not None
    if ctx.logger is not None:
        ctx.logger.error(
            f"[[FinalError]] stage=review; attempts_exhausted=true; "
            f"exception={type(last_error).__name__}: {last_error}\n"
        )
    raise CommentReviewFailedError(
        f"--ai-review could not produce a valid AnnotationSelectionBatch for "
        f"{len(consult_indices)} consulted chapter(s) after {max_retries} attempts: {last_error}"
    ) from last_error


__all__ = ["review_annotations"]
