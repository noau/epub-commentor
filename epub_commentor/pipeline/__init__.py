"""Annotation pipeline.

The package has three layers, each owning one stage of the work:

- :mod:`epub_commentor.pipeline.extract` — read an EPUB and pull out the
  chapter list, parsed DOMs and book metadata.
- :mod:`epub_commentor.pipeline.process` — drive the two LLM stages
  (full-chapter scan, then per-block annotation).
- :mod:`epub_commentor.pipeline.inject` — splice the resulting
  ``<aside>`` elements back into the chapter DOMs and wire the
  ``commentary.css`` stylesheet.
"""

from .extract import Chapter, ChapterFilter, extract_chapters
from .inject import (
    inject_annotations,
    inject_chapter,
    inject_chapter_head_link,
    inject_comment,
    inject_css_zip,
    inject_opf,
)
from .process import ChapterAnnotation, process_chapters

__all__ = [
    "Chapter",
    "ChapterAnnotation",
    "ChapterFilter",
    "extract_chapters",
    "inject_annotations",
    "inject_chapter",
    "inject_chapter_head_link",
    "inject_comment",
    "inject_css_zip",
    "inject_opf",
    "process_chapters",
]
