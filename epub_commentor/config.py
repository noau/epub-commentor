"""User-facing configuration for the annotation pipeline.

This module deliberately lives at the package root (not under ``llm`` or
``pipeline``) so that every layer can import it without risking a cycle:
``llm.memo`` and ``llm.block`` need to know defaults like ``block_size``;
``pipeline.process`` orchestrates them; ``commentor`` exposes the API.

Keeping :class:`CommentConfig` here also makes the public surface in
``epub_commentor/__init__.py`` trivial — one re-export.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .llm.schema import CommentKind, CommentPosition

_DEFAULT_KINDS: tuple[CommentKind, ...] = (
    CommentKind.INTRO,
    CommentKind.SUMMARY,
    CommentKind.NOTE,
)


@dataclass
class CommentConfig:
    """All knobs the annotation pipeline needs at runtime.

    The defaults match the PRD §公开 API; users typically only override
    ``book_synopsis``, ``kinds`` and ``block_size``.
    """

    position: CommentPosition = CommentPosition.BEFORE
    kinds: tuple[CommentKind, ...] = field(default_factory=lambda: _DEFAULT_KINDS)
    block_size: int = 6
    max_json_retries: int = 3
    max_scan_retries: int = 3
    concurrency: int = 4
    cache_seed_user_id: str = "default"
    book_synopsis: str | None = None
    inject_css: bool = True
    css_path_in_epub: Path = field(default_factory=lambda: Path("Styles/commentary.css"))
    target_language: str = "Chinese"
    fail_on_empty_chapter: bool = False
    fail_on_block_error: bool = False
    skip_chapter_on_empty_annotation: bool = False


__all__ = ["CommentConfig"]
