"""Stage 1 — full-chapter scan.

The scan produces a :class:`~epub_commentor.llm.schema.ChapterMemo` that
captures the chapter's thesis, outline, vocabulary, tone and target audience.
Stage 2 (see :mod:`epub_commentor.llm.block`) consumes the memo together with
the original paragraphs to emit per-block annotations.

Stage 1 is **sequential across chapters** (one chapter at a time) but each
chapter is self-contained, so a single :class:`LLMContext` is sufficient.
Cache key is namespaced so swapping the prompt or the user re-derives the
whole chapter set.
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

    with llm.context(cache_seed_content=seed) as ctx:
        raw = ctx.request(
            [
                Message(MessageRole.SYSTEM, system_text),
                Message(MessageRole.USER, user_text),
            ]
        )

    try:
        return ChapterMemo.model_validate_json(raw)
    except ValidationError as error:
        raise CommentScanFailedError(
            f"Stage 1 (scan) returned invalid ChapterMemo JSON for {chapter_path.as_posix()}: {error}"
        ) from error


__all__ = ["scan_chapter"]
