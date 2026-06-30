"""Annotation pipeline.

The package has three layers, each owning one stage of the work:

- :mod:`epub_commentor.pipeline.extract` — read an EPUB and pull out the
  chapter list, parsed DOMs and book metadata.
- :mod:`epub_commentor.pipeline.process` — drive the two LLM stages
  (full-chapter scan, then per-block annotation).
- :mod:`epub_commentor.pipeline.inject` — splice the resulting
  ``<aside>`` elements back into the chapter DOMs.
"""

from .extract import Chapter, extract_chapters
from .process import ChapterAnnotation, process_chapters

__all__ = ["Chapter", "ChapterAnnotation", "extract_chapters", "process_chapters"]
