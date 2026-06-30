"""Unit tests for :mod:`epub_commentor.pipeline.extract`.

Verifies the chapter-level extraction against a real EPUB asset
(``The little prince.epub``) and confirms the metadata dict has the
reserved ``__opf_path__`` key used by downstream stages.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from epub_commentor.epub.zip import Zip
from epub_commentor.pipeline.extract import Chapter, extract_chapters


class TestExtractChapters:
    def test_extract_returns_chapters_and_metadata(self, tmp_path: Path) -> None:
        asset = Path("tests/assets/The little prince.epub")
        if not asset.exists():
            import pytest

            pytest.skip(f"asset not found: {asset}")

        src = tmp_path / "src.epub"
        out = tmp_path / "out.epub"
        shutil.copy(asset, src)

        with Zip(src, out) as z:
            chapters, metadata = extract_chapters(z)

        assert isinstance(chapters, list)
        assert len(chapters) >= 1
        for ch in chapters:
            assert isinstance(ch, Chapter)
            assert isinstance(ch.path, Path)
            assert ch.title
            assert ch.body is not None
            assert ch.xml_node is not None

        assert isinstance(metadata, dict)
        # Reserved key for the OPF path used by the inject layer
        assert "__opf_path__" in metadata
        assert metadata["__opf_path__"].endswith(".opf")

    def test_chapter_body_iter_paragraphs(self, tmp_path: Path) -> None:
        asset = Path("tests/assets/The little prince.epub")
        if not asset.exists():
            import pytest

            pytest.skip(f"asset not found: {asset}")

        src = tmp_path / "src.epub"
        out = tmp_path / "out.epub"
        shutil.copy(asset, src)

        with Zip(src, out) as z:
            chapters, _ = extract_chapters(z)

        # Every chapter has a body; the iter API should be the way the
        # pipeline walks paragraphs. Pick the first chapter that actually
        # contains a <p>.
        chapters_with_p = [ch for ch in chapters if len(list(ch.body.iter("p"))) > 0]
        assert chapters_with_p, "expected at least one chapter with a <p>"
        sample = chapters_with_p[0]
        paras = list(sample.body.iter("p"))
        # sanity: iter returns the same nodes as findall
        assert len(paras) > 0
        # walking every paragraph yields the same node count via findall
        assert len(paras) == len(sample.body.findall(".//p"))
