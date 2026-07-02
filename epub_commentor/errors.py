"""Exception hierarchy for the commentary pipeline.

All errors raised from the LLM stages and the validation layer derive from
:class:`CommentorError` so that callers (CLI, tests) can write a single
``except CommentorError`` clause and still benefit from Python's built-in
``ValueError`` semantics (every subclass inherits from ``ValueError``).

The seven concrete subclasses correspond to the failure modes the PRD
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
- :class:`CommentSelectFailedError` — The book-level pre-filter LLM call
  (``--ai-select``) could not produce a valid
  :class:`~epub_commentor.llm.schema.ChapterSelectionBatch` after
  ``config.ai_select_max_retries`` attempts.
- :class:`CommentReviewFailedError` — The book-level post-filter LLM call
  (``--ai-review``) could not produce a valid
  :class:`~epub_commentor.llm.schema.AnnotationSelectionBatch` after
  ``config.ai_review_max_retries`` attempts.
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


class CommentSelectFailedError(CommentorError):
    """``--ai-select`` could not produce a valid ChapterSelectionBatch.

    Raised after ``config.ai_select_max_retries`` exhausted attempts at
    producing a JSON ``selections`` list whose indices match the input
    chapters. The retry loop in :mod:`epub_commentor.llm.select` logs each
    attempt as ``[[StageError]]`` and the exhaustion as ``[[FinalError]]``
    before raising.
    """


class CommentReviewFailedError(CommentorError):
    """``--ai-review`` could not produce a valid AnnotationSelectionBatch.

    Raised after ``config.ai_review_max_retries`` exhausted attempts at
    producing a JSON ``selections`` list whose ``chapter_index`` values
    match the input annotations. The retry loop in
    :mod:`epub_commentor.llm.review` logs each attempt as
    ``[[StageError]]`` and the exhaustion as ``[[FinalError]]`` before
    raising.
    """


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
    "CommentReviewFailedError",
    "CommentScanFailedError",
    "CommentSelectFailedError",
    "CommentorError",
]
