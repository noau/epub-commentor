"""Exception hierarchy for the commentary pipeline.

All errors raised from the LLM stages and the validation layer derive from
:class:`CommentorError` so that callers (CLI, tests) can write a single
``except CommentorError`` clause and still benefit from Python's built-in
``ValueError`` semantics (every subclass inherits from ``ValueError``).

The five concrete subclasses correspond to the failure modes the PRD
identifies as recoverable / distinguishable from generic Python errors:

- :class:`CommentInvalidJSONError` — Stage 2 LLM response could not be
  parsed as a valid :class:`~epub_commentor.llm.schema.BlockAnnotation`
  after ``config.max_json_retries`` attempts.
- :class:`CommentOrphanPIdError` — A :class:`~epub_commentor.llm.schema.CommentItem`
  references paragraph indices that fall outside the block, or whose range
  is not contiguous.
- :class:`CommentOverlapError` — Two comments inside the same block share
  one or more paragraph indices.
- :class:`CommentScanFailedError` — Stage 1 LLM response could not be
  parsed as a valid :class:`~epub_commentor.llm.schema.ChapterMemo`.
- :class:`CommentNoParagraphsError` — A chapter contained zero ``<p>``
  elements and the caller asked to process it (e.g. poetry, list-only
  chapters, cover pages). This is a structural problem, not an LLM
  problem, and the caller may choose to skip such chapters.
"""

from __future__ import annotations


class CommentorError(ValueError):
    """Base class for every error the commentary pipeline raises.

    Inheriting from :class:`ValueError` preserves backward compatibility
    with code that catches ``ValueError`` (the previous behaviour) while
    giving new callers a single, stable target to catch.
    """


class CommentInvalidJSONError(CommentorError):
    """Stage 2 could not produce a valid :class:`BlockAnnotation` JSON."""


class CommentOrphanPIdError(CommentorError):
    """A comment references p_ids outside the block, or a non-contiguous range."""


class CommentOverlapError(CommentorError):
    """Two comments inside the same block share one or more p_ids."""


class CommentScanFailedError(CommentorError):
    """Stage 1 could not produce a valid :class:`ChapterMemo` JSON."""


class CommentNoParagraphsError(CommentorError):
    """A chapter contains zero ``<p>`` elements and cannot be annotated."""


class CommentAbortError(KeyboardInterrupt):
    """The user pressed Ctrl-C during the pipeline.

    Inherits from :class:`KeyboardInterrupt` (not :class:`CommentorError`)
    on purpose: the CLI's outer ``except CommentorError`` reports failures
    with exit code 1 and a stack trace, but an abort should produce a
    clean exit code 130 with no traceback. The CLI catches this in a
    dedicated ``except`` clause and returns 130.

    Raised cooperatively by :mod:`epub_commentor.llm._abort`,
    :mod:`epub_commentor.llm.executor`, and
    :mod:`epub_commentor.llm.context` once the SIGINT handler has set
    the abort flag.
    """


__all__ = [
    "CommentAbortError",
    "CommentInvalidJSONError",
    "CommentNoParagraphsError",
    "CommentOverlapError",
    "CommentOrphanPIdError",
    "CommentScanFailedError",
    "CommentorError",
]
