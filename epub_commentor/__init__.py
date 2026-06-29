"""epub-commentor: Add AI-generated commentary to EPUB books.

This package re-exports the LLM client and message types. Higher-level
commentary orchestration lives in :mod:`epub_commentor.commentor` and is
exported here once implemented.
"""

from .llm import LLM, Message, MessageRole

__all__ = [
    "LLM",
    "Message",
    "MessageRole",
]
