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

    The four ``ai_*`` fields are book-level LLM gates that drive the
    ``--ai-select`` (pre-filter) and ``--ai-review`` (post-filter) modes.
    They are intentionally **not** exposed as CLI flags in v1 — operators
    tune them via ``format.json`` rather than the command line, so the
    CLI surface stays lean. The CLI's argparse layer only needs to know
    whether the gates are on or off; once on, every numeric knob flows
    through ``_split_format_config`` into this dataclass automatically.
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
    # ---- Book-level AI gates (--ai-select / --ai-review) ----
    ai_select_min_body_chars: int = 200
    """Max characters of body preview fed to the ``--ai-select`` LLM call
    per chapter. Keeps the prompt cheap (~2.4K tokens for a 28-chapter
    book vs. ~165K for a full Stage 1 scan)."""
    ai_review_min_comments_per_chapter: int = 1
    """Chapters whose Stage 2 output has fewer comments than this are
    auto-dropped by ``--ai-review`` (no LLM call). 1 means "drop
    zero-comment chapters"; raise to drop sparser chapters too."""
    ai_select_max_retries: int = 3
    """Independent retry budget for the ``--ai-select`` LLM call."""
    ai_review_max_retries: int = 3
    """Independent retry budget for the ``--ai-review`` LLM call."""
    enable_translation: bool = False
    """When ``True``, run Stage 3 (per-block paragraph translation) after
    Stage 2 + the annotation review gate. Translation language is taken
    from :attr:`target_language` so commentary and translation stay in
    lockstep — there is no separate ``translation_target_language`` knob
    to keep the CLI surface lean. Off by default; original text is
    preserved untouched, translations are inserted as ``<p class="translation">``
    right after each source paragraph."""
    max_translation_retries: int = 3
    """Independent retry budget for the Stage 3 LLM call. Mirrors
    :attr:`max_json_retries` / :attr:`max_scan_retries` so a flaky
    translation provider can be tuned without touching Stage 2 budgets."""
    fail_on_translation_error: bool = False
    """When ``True``, raise
    :class:`~epub_commentor.errors.CommentTranslationFailedError` on
    Stage 3 retry exhaustion. Default ``False`` logs a warning and
    drops the failed block (``annotation.translation_blocks_skipped`` is
    incremented; other blocks still get translated) — same dual policy
    Stage 2 uses via :attr:`fail_on_block_error`."""



__all__ = ["CommentConfig"]
