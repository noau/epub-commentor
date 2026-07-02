"""``--ai-select`` — book-level pre-filter.

Drives a single LLM call per book that decides which chapters deserve
AI-generated commentary. Receives the spine-ordered chapter list with
titles and short body previews, returns a parallel ``list[bool]`` mask
alongside a per-index ``reason`` map that the CLI surfaces in the
post-run summary panel.

Cache naming follows the same convention as :mod:`epub_commentor.llm.memo`
(``commentor:{VERSION}:select:{user_id}:{book_hash}``), so swapping the
prompt or the user_id re-derives the whole book without colliding with
the per-chapter Stage 1 cache.

Retry loop mirrors :func:`epub_commentor.llm.memo.scan_chapter`: on a
malformed JSON response, the bad turn plus a corrective user message is
appended and the LLM is asked again, up to
``config.ai_select_max_retries`` times. Per-attempt failures are logged as
``[[StageError]]``; the final exhaustion is logged as ``[[FinalError]]``
and raised as :class:`~epub_commentor.errors.CommentSelectFailedError`.
"""

from __future__ import annotations

import hashlib
import logging
from importlib.metadata import PackageNotFoundError, version

from pydantic import ValidationError

from ..config import CommentConfig
from ..errors import CommentSelectFailedError
from ..pipeline.extract import Chapter
from ..utils import normalize_whitespace
from ..xml.xml import plain_text
from .protocol import LLMProtocol
from .schema import ChapterSelectionBatch
from .types import Message, MessageRole

try:
    _VERSION = version("epub-commentor")
except PackageNotFoundError:
    _VERSION = "0.0.0-dev"

_logger = logging.getLogger(__name__)

# Local copies so a stray reserved metadata key from the upstream pipeline
# never leaks into the LLM prompt. Mirrors the constant defined in
# ``pipeline.process``; kept inline to avoid a cross-package import cycle.
_RESERVED_METADATA_KEYS = frozenset({"__opf_path__"})


def _book_hash(chapters: list[Chapter]) -> str:
    """SHA-1 over the sorted list of chapter paths, first 12 hex chars.

    Twelve hex chars (48 bits of entropy) is enough to keep the
    book-level cache key collision-free across a personal library of
    tens of thousands of books while still being short enough to read
    in a log line.
    """
    joined = "|".join(sorted(c.path.as_posix() for c in chapters))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _select_seed(config: CommentConfig, book_hash: str) -> str:
    return f"commentor:{_VERSION}:select:{config.cache_seed_user_id}:{book_hash}"


def _chapter_preview(chapter: Chapter, max_chars: int) -> str:
    """First ``max_chars`` characters of the chapter body, whitespace-
    normalised so the LLM never sees a 200-character run of newlines.

    Falls back to the chapter title when the body has no text content
    (image-only chapters, pure structural pages).
    """
    text = normalize_whitespace(plain_text(chapter.body)).strip()
    if not text:
        return f"(no body text — title: {chapter.title})"
    return text[:max_chars]


def _paragraph_count(chapter: Chapter) -> int:
    return sum(1 for _ in chapter.body.iter("p"))


def _format_select_user(
    chapters: list[Chapter],
    book_metadata: dict[str, str],
    preview_chars: int,
) -> str:
    # Strip reserved keys here so this helper is safe to call directly
    # from tests; the ``select_chapters`` call site already strips but
    # defence-in-depth is cheap.
    safe_meta = {k: v for k, v in book_metadata.items() if k not in _RESERVED_METADATA_KEYS}
    meta_lines = "\n".join(f"- {k}: {v}" for k, v in safe_meta.items()) or "(none)"
    blocks: list[str] = []
    for i, ch in enumerate(chapters):
        preview = _chapter_preview(ch, preview_chars)
        n_p = _paragraph_count(ch)
        blocks.append(f"{i}. {ch.title}  (paragraphs: {n_p}, preview: {preview})")
    chapters_block = "\n\n".join(blocks)
    return f"Book metadata:\n{meta_lines}\n\nChapters ({len(chapters)} total, spine-ordered):\n\n{chapters_block}"


def _raw_excerpt(raw: str, limit: int = 400) -> str:
    """Truncated raw response used for [[StageError]] log sections."""
    return raw[:limit] + ("…" if len(raw) > limit else "")


def _format_validation_error(error: Exception, raw: str) -> str:
    raw_excerpt = _raw_excerpt(raw)
    # Pydantic wraps `ValueError` subclasses raised inside `model_validator`
    # in a `ValidationError`. The original message is preserved after the
    # "Value error, " prefix on the first error's `msg` field — strip it
    # so the corrective message reads naturally to the LLM.
    err_text = str(error)
    prefix = "Value error, "
    if err_text.startswith(prefix):
        err_text = err_text[len(prefix) :]
    return (
        "Your previous response could not be parsed as valid ChapterSelectionBatch JSON.\n"
        f"Error: {err_text}\n\n"
        "Raw response (truncated):\n"
        f"```\n{raw_excerpt}\n```\n\n"
        "Please reply with ONLY the corrected JSON object conforming to the schema. "
        "Return one entry per chapter in the input list, with `index` matching the input "
        "position (0, 1, 2, ...), in ascending order, with no duplicates."
    )


def select_chapters(
    chapters: list[Chapter],
    book_metadata: dict[str, str],
    llm: LLMProtocol,
    config: CommentConfig,
) -> tuple[list[bool], dict[int, str]]:
    """Decide which chapters deserve Stage 1 + Stage 2 commentary.

    Returns ``(mask, reasons)`` where ``mask[i]`` is ``True`` if the
    chapter at spine position ``i`` should be processed and ``reasons[i]``
    is the LLM's one-sentence explanation for that verdict (used by the
    CLI summary panel).

    A single LLM call drives the decision for the whole book; the cache
    seed is namespaced under ``:select:`` so it cannot collide with
    Stage 1 (``scan``) or Stage 2 (``annotate``) cache entries.

    Raises :class:`~epub_commentor.errors.CommentSelectFailedError` after
    ``config.ai_select_max_retries`` attempts at producing a valid
    :class:`~epub_commentor.llm.schema.ChapterSelectionBatch`.
    """
    if not chapters:
        return [], {}

    book_hash = _book_hash(chapters)
    seed = _select_seed(config, book_hash)
    preview_chars = max(1, int(getattr(config, "ai_select_min_body_chars", 200) or 200))
    max_retries = max(1, int(getattr(config, "ai_select_max_retries", 3) or 3))

    system_text = llm.template("select").render(
        target_language=config.target_language,
        book_synopsis=config.book_synopsis or "(none)",
    )
    prompt_metadata = {k: v for k, v in book_metadata.items() if k not in _RESERVED_METADATA_KEYS}
    user_text = _format_select_user(chapters, prompt_metadata, preview_chars)

    messages: list[Message] = [
        Message(MessageRole.SYSTEM, system_text),
        Message(MessageRole.USER, user_text),
    ]
    last_error: Exception | None = None

    with llm.context(cache_seed_content=seed) as ctx:
        for retry in range(max_retries):
            raw = ctx.request(messages)
            try:
                parsed = ChapterSelectionBatch.model_validate_json(raw)
                # The pydantic validator enforces uniqueness, ascending
                # order, and contiguous [0, len(selections)) indices —
                # but it cannot know the *expected* length. Enforce it
                # here so a too-short response triggers a retry.
                if len(parsed.selections) != len(chapters):
                    raise ValueError(
                        f"ChapterSelectionBatch length mismatch: got {len(parsed.selections)} "
                        f"selections for {len(chapters)} chapters; expected exactly one per chapter"
                    )
                mask = [False] * len(chapters)
                reasons: dict[int, str] = {}
                for sel in parsed.selections:
                    mask[sel.index] = sel.include
                    reasons[sel.index] = sel.reason
                # Fill in any chapter the LLM forgot (shouldn't happen
                # given the validator, but defensive — never silently
                # let a True default slip in).
                for i in range(len(chapters)):
                    if i not in reasons:
                        mask[i] = False
                        reasons[i] = "(LLM omitted this chapter — defaulted to skip)"
                return mask, reasons
            except (ValidationError, ValueError) as exc:
                last_error = exc
                # Drop this invalid response from the cache: it would
                # otherwise be committed at __exit__ and replayed on
                # any subsequent run that re-enters this exact input.
                ctx.discard_last()
                if ctx.logger is not None:
                    ctx.logger.warning(
                        f"[[StageError]] stage=select; "
                        f"attempt={retry + 1}/{max_retries}; "
                        f"error={type(exc).__name__}: {exc}\n"
                        f"Raw excerpt:\n{_raw_excerpt(raw)}\n"
                    )
                if retry == max_retries - 1:
                    break
                messages.append(Message(MessageRole.ASSISTANT, raw))
                messages.append(Message(MessageRole.USER, _format_validation_error(exc, raw)))

    assert last_error is not None  # always set when we exit the retry loop without returning
    if ctx.logger is not None:
        ctx.logger.error(
            f"[[FinalError]] stage=select; attempts_exhausted=true; "
            f"exception={type(last_error).__name__}: {last_error}\n"
        )
    raise CommentSelectFailedError(
        f"--ai-select could not produce a valid ChapterSelectionBatch for "
        f"{len(chapters)} chapter(s) after {max_retries} attempts: {last_error}"
    ) from last_error


__all__ = ["select_chapters"]
