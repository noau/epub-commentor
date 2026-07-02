"""Stage 1 — full-chapter scan.

The scan produces a :class:`~epub_commentor.llm.schema.ChapterMemo` that
captures the chapter's thesis, outline, vocabulary, tone and target audience.
Stage 2 (see :mod:`epub_commentor.llm.block`) consumes the memo together with
the original paragraphs to emit per-block annotations.

Stage 1 is **sequential across chapters** (one chapter at a time) but each
chapter is self-contained, so a single :class:`LLMContext` is sufficient.
Cache key is namespaced so swapping the prompt or the user re-derives the
whole chapter set.

Retry loop mirrors the Stage 2 contract in :mod:`epub_commentor.llm.block`:
on a malformed JSON response, the bad turn plus a corrective user message
is appended and the LLM is asked again, up to ``config.max_scan_retries``
times. Per-attempt failures are logged as ``[[StageError]]``; the final
exhaustion is logged as ``[[FinalError]]`` and raised as
:class:`~epub_commentor.errors.CommentScanFailedError`.
"""

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from xml.etree.ElementTree import Element

from pydantic import ValidationError

from ..config import CommentConfig
from ..errors import CommentScanFailedError
from ..xml import plain_text
from .protocol import LLMProtocol
from .schema import ChapterMemo
from .types import Message, MessageRole

try:
    _VERSION = version("epub-commentor")
except PackageNotFoundError:
    _VERSION = "0.0.0-dev"


def _chapter_hash(chapter_path: Path) -> str:
    return hashlib.sha1(chapter_path.as_posix().encode("utf-8")).hexdigest()[:8]


def _scan_seed(config: CommentConfig, chapter_path: Path) -> str:
    return f"commentor:{_VERSION}:scan:{config.cache_seed_user_id}:{_chapter_hash(chapter_path)}"


def _format_scan_user(
    chapter_title: str,
    book_metadata: dict[str, str],
    chapter_full_text: str,
) -> str:
    meta_lines = "\n".join(f"- {k}: {v}" for k, v in book_metadata.items()) or "(none)"
    return (
        f"Chapter title: {chapter_title}\n"
        f"Book metadata:\n{meta_lines}\n\n"
        f"Chapter full text:\n```\n{chapter_full_text}\n```"
    )


def _raw_excerpt(raw: str, limit: int = 400) -> str:
    """Truncated raw response used for [[StageError]] log sections."""
    return raw[:limit] + ("…" if len(raw) > limit else "")


def _format_validation_error(error: Exception, raw: str) -> str:
    raw_excerpt = raw[:400] + ("…" if len(raw) > 400 else "")
    return (
        "Your previous response could not be parsed as valid ChapterMemo JSON.\n"
        f"Error: {error}\n\n"
        "Raw response (truncated):\n"
        f"```\n{raw_excerpt}\n```\n\n"
        "Please reply with ONLY the corrected JSON object conforming to the schema."
    )


def scan_chapter(
    body: Element,
    chapter_path: Path,
    chapter_title: str,
    book_metadata: dict[str, str],
    llm: LLMProtocol,
    config: CommentConfig,
) -> ChapterMemo:
    """Run Stage 1 for a single chapter.

    The body element is only read (``plain_text``) — its DOM is never mutated
    here. ``chapter_path`` is used purely as a stable identifier for caching;
    no file I/O happens against it.

    Retries up to ``config.max_scan_retries`` times on malformed JSON.
    """
    seed = _scan_seed(config, chapter_path)

    system_text = llm.template("scan").render(
        target_language=config.target_language,
        book_synopsis=config.book_synopsis or "(none)",
    )
    user_text = _format_scan_user(
        chapter_title=chapter_title,
        book_metadata=book_metadata,
        chapter_full_text=plain_text(body),
    )

    messages: list[Message] = [
        Message(MessageRole.SYSTEM, system_text),
        Message(MessageRole.USER, user_text),
    ]
    last_error: Exception | None = None

    with llm.context(cache_seed_content=seed) as ctx:
        for retry in range(config.max_scan_retries):
            raw = ctx.request(messages)
            try:
                return ChapterMemo.model_validate_json(raw)
            except ValidationError as exc:
                last_error = exc
                # Drop this invalid response from the cache: it would
                # otherwise be committed at __exit__ and replayed on
                # any subsequent run that re-enters this exact input.
                ctx.discard_last()
                if ctx.logger is not None:
                    ctx.logger.warning(
                        f"[[StageError]] stage=scan; "
                        f"attempt={retry + 1}/{config.max_scan_retries}; "
                        f"error={type(exc).__name__}: {exc}\n"
                        f"Raw excerpt:\n{_raw_excerpt(raw)}\n"
                    )
                if retry == config.max_scan_retries - 1:
                    break
                messages.append(Message(MessageRole.ASSISTANT, raw))
                messages.append(Message(MessageRole.USER, _format_validation_error(exc, raw)))

    assert last_error is not None  # always set when we exit the retry loop without returning
    if ctx.logger is not None:
        ctx.logger.error(
            f"[[FinalError]] stage=scan; attempts_exhausted=true; exception={type(last_error).__name__}: {last_error}\n"
        )
    raise CommentScanFailedError(
        f"Stage 1 (scan) returned invalid ChapterMemo JSON for "
        f"{chapter_path.as_posix()} after {config.max_scan_retries} attempts: {last_error}"
    ) from last_error


__all__ = ["scan_chapter"]
