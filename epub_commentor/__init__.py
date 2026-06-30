"""epub-commentor: Add AI-generated commentary to EPUB books.

This package re-exports the LLM client, configuration dataclass, the
top-level pipeline entry points, and the :class:`CommentorError` exception
hierarchy. The single high-level orchestration entry point
:func:`comment_epub` is also re-exported so callers (``scripts/comment_epub.py``,
third-party tools) can import everything from the top level.
"""

from .commentor import CommentorResult, comment_epub
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
    "CommentorResult",
    "LLM",
    "Message",
    "MessageRole",
    "comment_epub",
    "inject_annotations",
]
