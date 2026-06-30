"""epub-commentor: Add AI-generated commentary to EPUB books.

This package re-exports the LLM client, configuration dataclass, and the
top-level pipeline entry points. Higher-level orchestration (e.g.
``comment_epub``) will be added in M6 once the CLI is wired in.
"""

from .config import CommentConfig
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
    "LLM",
    "Message",
    "MessageRole",
    "inject_annotations",
]
