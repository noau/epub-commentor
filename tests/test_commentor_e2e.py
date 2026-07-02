"""End-to-end test of the full pipeline (extract → process → inject)
against a real EPUB, driven by :class:`MockLLM`.

This is the integration smoke for the whole stack. We pick a small
subset of chapters that actually contain ``<p>`` elements (the test
asset's first spine entries are cover pages with no body text), feed
Stage 1 and Stage 2 canned responses, then verify the resulting EPUB
contains the expected CSS, OPF manifest patch, chapter head link and
injected ``<aside>`` elements.
"""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor.cli import _build_ai_annotation_filter, _build_ai_chapter_filter
from epub_commentor.config import CommentConfig
from epub_commentor.epub.zip import Zip
from epub_commentor.pipeline.extract import extract_chapters
from epub_commentor.pipeline.inject import inject_annotations
from epub_commentor.pipeline.process import process_chapters


def _memo_json() -> str:
    return json_dumps(
        {
            "core_thesis": "test memo",
            "outline": ["a", "b", "c"],
            "tone": "t",
            "target_audience": "g",
        }
    )


def _annotations_json() -> str:
    return json_dumps(
        {
            "comments": [
                {
                    "target_p_ids": [0],
                    "position": "before",
                    "kind": "intro",
                    "content": "E2E test intro.",
                }
            ]
        }
    )


def _chapter_selection_keep_all(n: int, *, reason: str = "substantial narrative chapter") -> str:
    """Build a :class:`ChapterSelectionBatch` (for ``--ai-select``) that keeps every entry."""
    return json_dumps({"selections": [{"index": i, "include": True, "reason": reason} for i in range(n)]})


def _chapter_selection_drop_half(n: int) -> str:
    """Build a :class:`ChapterSelectionBatch` that drops every other entry."""
    return json_dumps(
        {
            "selections": [
                {
                    "index": i,
                    "include": (i % 2 == 0),
                    "reason": "narrative chapter" if i % 2 == 0 else "structural / front-matter",
                }
                for i in range(n)
            ]
        }
    )


def _annotation_selection_keep_all(n: int, *, reason: str = "good commentary") -> str:
    """Build an :class:`AnnotationSelectionBatch` (for ``--ai-review``) that keeps every entry."""
    return json_dumps({"selections": [{"chapter_index": i, "include": True, "reason": reason} for i in range(n)]})


class TestEndToEndPipeline:
    def test_extract_process_inject_round_trip(self, tmp_path: Path) -> None:
        asset = Path("tests/assets/The little prince.epub")
        if not asset.exists():
            pytest.skip(f"asset not found: {asset}")

        src = tmp_path / "src.epub"
        out = tmp_path / "out.epub"
        shutil.copy(asset, src)

        # 1. extract
        with Zip(src, out) as z:
            chapters, metadata = extract_chapters(z)

        # 2. process with the mock LLM
        chapters_with_p = [ch for ch in chapters if len(list(ch.body.iter("p"))) > 0]
        assert len(chapters_with_p) >= 2, "need at least 2 chapters with <p>"
        target = chapters_with_p[:2]
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": _annotations_json(),
            }
        )
        config = CommentConfig(block_size=20)
        annotations, _ = process_chapters(target, book_metadata=metadata, llm=llm, config=config)
        assert len(annotations) == 2
        for ann in annotations:
            assert ann.memo.core_thesis == "test memo"
            assert len(ann.comments) == 1
            assert ann.comments[0].content == "E2E test intro."

        # 3. inject
        with Zip(src, out) as z:
            inject_annotations(z, annotations, config, metadata)

        # 4. verify all 4 invariants on the output ZIP
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            # CSS present and bytes match the bundled file
            from importlib import resources

            assert "Styles/commentary.css" in names
            on_disk = resources.files("epub_commentor.data").joinpath("commentary.css").read_bytes()
            assert zf.read("Styles/commentary.css") == on_disk

            # OPF manifest has the new item
            opf_xml = zf.read(metadata["__opf_path__"]).decode("utf-8", errors="replace")
            assert "commentary-css" in opf_xml

            # Each chapter has the <link> in its <head>
            for ch in target:
                ch_xml = zf.read(ch.path.as_posix()).decode("utf-8", errors="replace")
                assert "commentary.css" in ch_xml
                # No residual data-p-id
                assert "data-p-id" not in ch_xml
                # <aside> count matches the in-memory count after injection
                expected_asides = sum(1 for _ in ch.body.iter("aside"))
                assert ch_xml.count("<aside") == expected_asides


class TestEndToEndAiSelect:
    """End-to-end exercise of the M9 AI batch gates against a real EPUB.

    These tests piggy-back on :class:`TestEndToEndPipeline`'s asset
    (``tests/assets/The little prince.epub``) so we exercise the same
    XML / OPF quirks the production CLI will hit. The LLM is the
    :class:`MockLLM` with cached responses for the ``:select:`` and
    ``:review:`` cache-seed prefixes. We pick a small chapter slice
    so the run finishes in well under a second.
    """

    @staticmethod
    def _build_args(**flags: bool) -> argparse.Namespace:
        """Build an :class:`argparse.Namespace` shaped like ``cli._build_parser`` output."""
        defaults = {
            "interactive": False,
            "ai_select": False,
            "review": False,
            "no_review": False,
            "ai_review": False,
        }
        defaults.update(flags)
        return argparse.Namespace(**defaults)

    def test_ai_select_drops_subset_and_runs_full_pipeline(self, tmp_path: Path) -> None:
        """``--ai-select`` pre-filter trims chapters; the rest of the
        pipeline (process + inject) runs against the survivors only and
        the dropped chapters' bytes flow through unchanged."""
        asset = Path("tests/assets/The little prince.epub")
        if not asset.exists():
            pytest.skip(f"asset not found: {asset}")

        src = tmp_path / "src.epub"
        out = tmp_path / "out.epub"
        shutil.copy(asset, src)

        with Zip(src, out) as z:
            chapters, metadata = extract_chapters(z)
        chapters_with_p = [ch for ch in chapters if len(list(ch.body.iter("p"))) > 0]
        # Limit to 4 chapters so the test finishes fast and the selection
        # batch is small enough to author by hand.
        target = chapters_with_p[:4]
        assert len(target) >= 2, "need at least 2 chapters with <p> for AI gate"

        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": _annotations_json(),
                "select__response": _chapter_selection_drop_half(len(target)),
            }
        )
        config = CommentConfig(block_size=20)

        args = self._build_args(ai_select=True)
        chapter_filter = _build_ai_chapter_filter(args, llm, config)
        assert chapter_filter is not None

        # Drive the full pipeline (extract → process → inject) using the
        # same APIs `comment_epub` uses under the hood.
        kept = [ch for ch, keep in zip(target, chapter_filter(target, metadata)) if keep]
        assert len(kept) == len(target) // 2 + len(target) % 2  # every-other drop

        annotations, _ = process_chapters(kept, book_metadata=metadata, llm=llm, config=config)
        assert len(annotations) == len(kept)
        with Zip(src, out) as z:
            inject_annotations(z, annotations, config, metadata)

        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "Styles/commentary.css" in names
            # CSS was injected — proves the inject stage completed against
            # the AI-trimmed chapter set.

    def test_ai_review_keeps_all_and_decisions_populated(self, tmp_path: Path) -> None:
        """``--ai-review`` post-filter accepts everything; ``CommentorResult``
        surfaces the full decision map (kept + reason per chapter) so the
        summary panel has data to render.

        The whole-book review LLM call expects one ``selections`` entry
        per consulted chapter (placeholder memos + zero-comment chapters
        are auto-dropped before the call). Driving this through the full
        ``comment_epub`` API would mean authoring a selection batch for
        every chapter in the book; we instead drive the gate directly
        via ``_review_gate`` against a sliced annotation list so the mock
        response stays tiny.
        """
        asset = Path("tests/assets/The little prince.epub")
        if not asset.exists():
            pytest.skip(f"asset not found: {asset}")

        src = tmp_path / "src.epub"
        out = tmp_path / "out.epub"
        shutil.copy(asset, src)

        with Zip(src, out) as z:
            chapters, metadata = extract_chapters(z)
        chapters_with_p = [ch for ch in chapters if len(list(ch.body.iter("p"))) > 0]
        slice_size = 3
        target = chapters_with_p[:slice_size]

        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": _annotations_json(),
                "review__response": _annotation_selection_keep_all(slice_size, reason="good commentary"),
            }
        )
        config = CommentConfig(block_size=20)
        args = self._build_args(ai_review=True)
        annotation_filter = _build_ai_annotation_filter(args, llm, config)
        assert annotation_filter is not None

        annotations, _ = process_chapters(target, book_metadata=metadata, llm=llm, config=config)
        assert len(annotations) == slice_size

        # Drive the gate directly so the review LLM call sees exactly
        # the slice's worth of chapters (rather than the full 31 chapters
        # `comment_epub` would extract from the book).
        from epub_commentor.commentor import _review_gate

        kept = _review_gate(
            annotations, annotation_filter=annotation_filter, progress_callback=None, book_metadata=metadata
        )

        # The post-filter accepted everything → annotation count unchanged.
        assert len(kept) == len(annotations)
        # The closure populated the module-level sink → CLI's main() can
        # pick it up and copy it into CommentorResult. Read it back via
        # the same import path main() uses.
        from epub_commentor.commentor import _AI_DECISION_SINKS

        decisions = _AI_DECISION_SINKS["review"]
        assert len(decisions) == slice_size
        assert all(include for _idx, (_title, include, _reason) in decisions.items())
        sample_reason = next(iter(decisions.values()))[2]
        assert sample_reason == "good commentary"
