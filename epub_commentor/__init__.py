"""epub-commentor: Add AI-generated commentary to EPUB books.

This package re-exports the LLM client, configuration dataclass, the
top-level pipeline entry points, and the :class:`CommentorError` exception
hierarchy. Higher-level orchestration (e.g. ``comment_epub``) will be
added in M6 once the CLI is wired in.
"""

from .config import CommentConfig
from .errors import (
    CommentInvalidJSONError,
    CommentNoParagraphsError,
    CommentorError,
    CommentOrphanPIdError,
    CommentOverlapError,
    CommentScanFailedError,
)
from .llm import LLM, Message, MessageRole
from .pipeline import (
    Chapter,
    ChapterAnnotation,
    inject_annotations,
)

__all__ = [
    "Chapter",
    "ChapterAnnotation",
    "CommentConfig",
    "CommentInvalidJSONError",
    "CommentNoParagraphsError",
    "CommentOverlapError",
    "CommentOrphanPIdError",
    "CommentScanFailedError",
    "CommentorError",
    "LLM",
    "Message",
    "MessageRole",
    "inject_annotations",
]
