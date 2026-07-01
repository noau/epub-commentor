"""Unit tests for :mod:`epub_commentor.pipeline.extract`.

Verifies the chapter-level extraction against a real EPUB asset
(``The little prince.epub``) and confirms the metadata dict has the
reserved ``__opf_path__`` key used by downstream stages.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from xml.etree.ElementTree import Element, fromstring

from epub_commentor.epub.zip import Zip
from epub_commentor.pipeline.extract import Chapter, _derive_title, extract_chapters

_NS_TAG_RE = re.compile(r"^\{[^}]+\}")


def _strip_ns(elem: Element) -> Element:
    """Recursively strip namespace prefixes from ``elem``'s tag and attribs.

    Mirrors what ``XMLLikeNode._extract_and_clean_namespaces`` does to
    real EPUB chapters; without this, ``<h1>`` parsed by stdlib
    ``fromstring`` carries the ``{http://www.w3.org/1999/xhtml}h1`` tag
    and ``find_first(root, "h1")`` will never match it.
    """
    for node in elem.iter():
        node.tag = _NS_TAG_RE.sub("", node.tag)
        for key in list(node.attrib.keys()):
            stripped = _NS_TAG_RE.sub("", key)
            if stripped != key:
                node.attrib[stripped] = node.attrib.pop(key)
    return elem


def _body(xml_fragment: str) -> Element:
    """Wrap an XHTML fragment in a ``<body>`` and parse it, with namespaces
    stripped so the element matches what ``_parse_chapter`` would hand to
    ``_derive_title`` after ``XMLLikeNode`` parsing.
    """
    return _strip_ns(
        fromstring(f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{xml_fragment}</body></html>').find(
            "{http://www.w3.org/1999/xhtml}body"
        )
    )


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


class TestDeriveTitle:
    """Unit tests for ``_derive_title`` with synthetic XHTML fragments.

    These run without any EPUB asset — the helper takes a body element
    directly, so we can exercise the tier logic in isolation.
    """

    _CH = Path("ch.xhtml")

    # ---- Tier 1: heading scan ----

    def test_h1_wins(self) -> None:
        body = _body("<h1>The Real Title</h1><p>...</p>")
        assert _derive_title(body, self._CH) == "The Real Title"

    def test_h2_used_when_h1_missing(self) -> None:
        body = _body("<h2>Second</h2><p>...</p>")
        assert _derive_title(body, self._CH) == "Second"

    def test_h3_used_when_h1_and_h2_missing(self) -> None:
        body = _body("<h3>Third</h3>")
        assert _derive_title(body, self._CH) == "Third"

    def test_empty_h1_falls_through_to_h2(self) -> None:
        body = _body("<h1></h1><h1>   </h1><h2>Real</h2>")
        assert _derive_title(body, self._CH) == "Real"

    def test_h1_longer_than_60_chars_rejected_falls_to_h2(self) -> None:
        long_h1 = "x" * 80
        body = _body(f"<h1>{long_h1}</h1><h2>Short</h2>")
        assert _derive_title(body, self._CH) == "Short"

    def test_h1_exactly_60_chars_accepted(self) -> None:
        body = _body(f"<h1>{'a' * 60}</h1>")
        assert _derive_title(body, self._CH) == "a" * 60

    def test_nested_bold_in_h1_returns_h1_text(self) -> None:
        # _first_text returns the first non-blank chunk; for "Book " (h1.text)
        # that wins before recursing into <b>.
        body = _body("<h1>Book <b>Title</b></h1>")
        assert _derive_title(body, self._CH) == "Book"

    def test_head_title_does_not_win(self) -> None:
        # _derive_title is called with a body element (per _parse_chapter).
        # Even if a <head><title> exists in the document, the search root
        # is the body, so the book name never leaks through.
        doc = fromstring(
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            "<head><title>Book Name</title></head>"
            "<body><h2>Chapter Title</h2></body>"
            "</html>"
        )
        body_node = _strip_ns(doc.find("{http://www.w3.org/1999/xhtml}body"))
        assert _derive_title(body_node, self._CH) == "Chapter Title"

    # ---- Tier 2: bold inline scan ----

    def test_strong_wins_when_no_headings(self) -> None:
        body = _body("<p>Intro <strong>Real Title</strong> tail</p>")
        assert _derive_title(body, self._CH) == "Real Title"

    def test_b_tag_wins_when_no_headings(self) -> None:
        body = _body("<p><b>Bold Title</b></p>")
        assert _derive_title(body, self._CH) == "Bold Title"

    def test_font_weight_bold_string(self) -> None:
        body = _body('<p style="font-weight: bold">Styled Title</p>')
        assert _derive_title(body, self._CH) == "Styled Title"

    def test_font_weight_700(self) -> None:
        body = _body('<p style="font-weight: 700">Numeric Bold</p>')
        assert _derive_title(body, self._CH) == "Numeric Bold"

    def test_font_weight_no_space(self) -> None:
        body = _body('<p style="font-weight:bold">NoSpace</p>')
        assert _derive_title(body, self._CH) == "NoSpace"

    def test_font_weight_among_other_declarations(self) -> None:
        body = _body('<p style="font-size:1.2em; font-weight: 700; color:red">Mixed</p>')
        assert _derive_title(body, self._CH) == "Mixed"

    def test_case_insensitive_style_value(self) -> None:
        body = _body('<p style="FONT-WEIGHT: BOLD">Shouty</p>')
        assert _derive_title(body, self._CH) == "Shouty"

    def test_uppercase_style_attr_name(self) -> None:
        # xml.etree.ElementTree preserves attribute-name case.
        body = _body('<p Style="font-weight: bold">Caps</p>')
        assert _derive_title(body, self._CH) == "Caps"

    # ---- Tier 2 (cont.): class-based bold ----

    def test_class_bold_wins_when_no_headings(self) -> None:
        # The example that prompted the extension: <span class="bold">.
        body = _body('<p><span class="bold">3 老栗树</span></p>')
        assert _derive_title(body, self._CH) == "3 老栗树"

    def test_class_strong_wins_when_no_headings(self) -> None:
        body = _body('<p><span class="strong">Header</span></p>')
        assert _derive_title(body, self._CH) == "Header"

    def test_class_heavy_wins_when_no_headings(self) -> None:
        body = _body('<p><span class="heavy">Heavy Title</span></p>')
        assert _derive_title(body, self._CH) == "Heavy Title"

    def test_class_title_wins_when_no_headings(self) -> None:
        body = _body('<p class="title">Chapter One</p>')
        assert _derive_title(body, self._CH) == "Chapter One"

    def test_class_header_wins_when_no_headings(self) -> None:
        body = _body('<p class="header">Overview</p>')
        assert _derive_title(body, self._CH) == "Overview"

    def test_class_case_insensitive(self) -> None:
        # Both the attribute name (CLASS=) and the token (Bold) vary in case.
        body = _body('<p CLASS="Bold">Shouty</p>')
        assert _derive_title(body, self._CH) == "Shouty"

    def test_class_one_of_many_tokens(self) -> None:
        # The recognised token sits alongside unrelated tokens.
        body = _body('<p class="intro bold lead">Lead In</p>')
        assert _derive_title(body, self._CH) == "Lead In"

    def test_class_substring_not_matched(self) -> None:
        # "bold-italic" is one token; it's not the same as "bold", so the
        # match is rejected. This is intentional — substring matches would
        # false-positive on arbitrarily-named classes.
        body = _body('<p class="bold-italic">Decorative</p>')
        result = _derive_title(body, self._CH)
        assert result.endswith("... (no title)")

    def test_class_unrecognised_does_not_win(self) -> None:
        body = _body('<p class="emphasis subtle">Quiet</p>')
        result = _derive_title(body, self._CH)
        assert result.endswith("... (no title)")

    def test_font_weight_500_is_not_bold(self) -> None:
        # 500 (medium) is intentionally rejected; falls through to tier 3.
        # ``plain_text`` concatenates the two <p> bodies without a separator,
        # so the preview string may contain the word "Medium" — we just
        # assert the result came from tier 3.
        body = _body('<p style="font-weight: 500">Medium</p><p>Preview text starts here.</p>')
        result = _derive_title(body, self._CH)
        assert result.endswith("... (no title)")

    def test_bold_text_longer_than_60_chars_rejected(self) -> None:
        # Tier 2 finds the 70-char <b> but rejects it for being too long.
        # Tier 3 then takes the first 15 chars of the body, which include
        # the leading bold text. We assert the result came from tier 3.
        body = _body(f"<p><b>{'x' * 70}</b></p><p>Fallback preview text starts here and keeps going</p>")
        result = _derive_title(body, self._CH)
        assert result.endswith("... (no title)")

    def test_heading_beats_bold(self) -> None:
        # Tier 1 has priority: if a short h1 exists, we never look at bold.
        body = _body("<h1>Real</h1><p><b>Also Bold</b></p>")
        assert _derive_title(body, self._CH) == "Real"

    # ---- Tier 3: plain_text preview ----

    def test_tier3_preview_when_nothing_else_matches(self) -> None:
        body = _body("<p>abcdefghij klmnop</p>")
        assert _derive_title(body, self._CH) == "abcdefghij klmn... (no title)"

    def test_tier3_truncates_at_fifteen(self) -> None:
        body = _body("<p>" + ("a" * 50) + "</p>")
        assert _derive_title(body, self._CH) == ("a" * 15) + "... (no title)"

    def test_tier3_handles_short_body(self) -> None:
        body = _body("<p>Hi</p>")
        assert _derive_title(body, self._CH) == "Hi... (no title)"

    def test_tier3_strips_leading_whitespace(self) -> None:
        # Stripping happens before slicing so a long whitespace prefix
        # doesn't shrink the preview to almost nothing.
        body = _body("<p>              abcdef ghijklmnop</p>")
        assert _derive_title(body, self._CH) == "abcdef ghijklmn... (no title)"

    def test_tier3_falls_back_to_stem_when_body_empty(self) -> None:
        body = _body("")
        # plain_text of an empty body is "", the preview guard fails, and the
        # final guard returns the file stem.
        assert _derive_title(body, Path("chapter_one.xhtml")) == "chapter_one"
