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
    CommentAbortError,
    CommentInvalidJSONError,
    CommentNoParagraphsError,
    CommentorError,
    CommentOrphanPIdError,
    CommentOverlapError,
    CommentScanFailedError,
)
from .llm import LLM, Message, MessageRole
from .pipeline import (
    AnnotationFilter,
    Chapter,
    ChapterAnnotation,
    ChapterFilter,
    inject_annotations,
)
from .progress import ProgressCallback, ProgressEvent, make_default_progress_callback

__all__ = [
    "AnnotationFilter",
    "Chapter",
    "ChapterAnnotation",
    "ChapterFilter",
    "CommentAbortError",
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
    "ProgressCallback",
    "ProgressEvent",
    "comment_epub",
    "inject_annotations",
    "make_default_progress_callback",
]
