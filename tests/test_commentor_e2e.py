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

import shutil
import zipfile
from pathlib import Path

import pytest
from _mock_llm import MockLLM, json_dumps

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
