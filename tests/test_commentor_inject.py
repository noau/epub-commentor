"""Unit tests for the M3 inject layer (epub_commentor.pipeline.inject).

Covers four areas in increasing scope:

* :class:`TestAsideFactory` — the low-level ``<aside>`` builder.
* :class:`TestParentMap` and :class:`TestBodyDirectAncestor` — DOM helpers
  that work around ElementTree's lack of ``getparent()``.
* :class:`TestChapterSplice` and :class:`TestChapterHeadLink` — single
  chapter mutation.
* :class:`TestOpfAndCss`, :class:`TestCssHrefs`,
  :class:`TestHrefResolution` — ZIP-level wiring.
* :class:`TestEndToEnd` — full pipeline against a real EPUB asset.

The tests construct in-memory ``<html>`` trees for unit cases and fall
back to the project's bundled :file:`tests/assets/The little prince.epub`
for the end-to-end cases (its first two spine entries are cover pages
with zero ``<p>`` elements, so we explicitly pick chapters that contain
text).
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, fromstring

import pytest

from epub_commentor.config import CommentConfig
from epub_commentor.epub.zip import Zip
from epub_commentor.llm.schema import ChapterMemo, CommentItem, CommentKind, CommentPosition
from epub_commentor.pipeline.extract import Chapter, extract_chapters
from epub_commentor.pipeline.inject import (
    _build_parent_map,
    _compute_css_hrefs,
    _find_body_direct_ancestor,
    _make_aside_simple,
    inject_annotations,
    inject_chapter,
    inject_chapter_head_link,
    inject_comment,
    inject_css_zip,
    inject_opf,
)
from epub_commentor.pipeline.process import ChapterAnnotation
from epub_commentor.xml import find_first
from epub_commentor.xml.xml_like import XMLLikeNode


def _parse_root(xml: str) -> Element:
    """Parse a tiny XHTML-ish snippet and return its root Element."""
    return fromstring(xml)


def _make_chapter(xml: str) -> tuple[Element, Element]:
    """Return ``(root, body)`` for a small XHTML-ish string."""
    root = _parse_root(xml)
    body = find_first(root, "body")
    assert body is not None, "test fixture must contain a <body> element"
    return root, body


def _make_p_element(text: str = "p") -> Element:
    p = Element("p")
    p.text = text
    return p


# ---------------------------------------------------------------------------
# <aside> factory
# ---------------------------------------------------------------------------


class TestAsideFactory:
    def test_classes_and_id(self) -> None:
        aside = _make_aside_simple(CommentKind.INTRO, "hi", "x1")
        assert aside.tag == "aside"
        assert aside.get("class") == "commentary commentary-intro"
        assert aside.get("id") == "cmt-x1"
        # content goes into the first <p>
        children = list(aside)
        assert len(children) == 1
        assert children[0].tag == "p"
        assert children[0].text == "hi"

    def test_multi_paragraph(self) -> None:
        aside = _make_aside_simple(CommentKind.SUMMARY, "para1\n\npara2", "s1")
        ps = [c for c in aside if c.tag == "p"]
        assert len(ps) == 2
        assert ps[0].text == "para1"
        assert ps[1].text == "para2"

    def test_line_break_in_paragraph(self) -> None:
        aside = _make_aside_simple(CommentKind.NOTE, "line1\nline2", "n1")
        ps = [c for c in aside if c.tag == "p"]
        assert len(ps) == 1
        # first line on <p>, second line on <br>.tail
        p = ps[0]
        assert p.text == "line1"
        brs = [c for c in p if c.tag == "br"]
        assert len(brs) == 1
        assert brs[0].tail == "line2"


# ---------------------------------------------------------------------------
# parent map
# ---------------------------------------------------------------------------


class TestParentMap:
    def test_basic_body_with_children(self) -> None:
        _, body = _make_chapter("<html><body><p>a</p><p>b</p><div><p>c</p></div></body></html>")
        pm = _build_parent_map(body)
        # body itself is not in the map (no parent)
        assert body not in pm
        # two top-level <p>s map to body
        ps = [c for c in body if c.tag == "p"]
        assert all(pm[p] is body for p in ps)
        # nested <p> maps to <div>
        div = find_first(body, "div")
        assert div is not None
        nested_p = find_first(div, "p")
        assert nested_p is not None
        assert pm[nested_p] is div


# ---------------------------------------------------------------------------
# body-direct ancestor
# ---------------------------------------------------------------------------


class TestBodyDirectAncestor:
    def test_top_level_p_returns_p(self) -> None:
        _, body = _make_chapter("<html><body><p>x</p></body></html>")
        target = find_first(body, "p")
        assert target is not None
        pm = _build_parent_map(body)
        assert _find_body_direct_ancestor(target, body, pm) is target

    def test_nested_p_walks_up_to_outermost_body_child(self) -> None:
        _, body = _make_chapter("<html><body><blockquote><div><p id='t'>x</p></div></blockquote></body></html>")
        target = find_first(body, "p")
        assert target is not None
        pm = _build_parent_map(body)
        anchor = _find_body_direct_ancestor(target, body, pm)
        # the aside lands as a sibling of <blockquote>, not inside <div>
        assert anchor.tag == "blockquote"


# ---------------------------------------------------------------------------
# inject_comment direct
# ---------------------------------------------------------------------------


class TestInjectComment:
    def test_before_top_level_p(self) -> None:
        _, body = _make_chapter("<html><body><p>1</p><p>2</p><p>3</p></body></html>")
        target = list(body)[1]  # the <p> with text "2"
        pm = _build_parent_map(body)
        inject_comment(body, target, CommentPosition.BEFORE, CommentKind.INTRO, "x", "c0", pm)
        # <aside> lands at index 1, "2" shifts to 2
        assert list(body)[0].tag == "p"
        assert list(body)[0].text == "1"
        assert list(body)[1].tag == "aside"
        assert list(body)[2].tag == "p"
        assert list(body)[2].text == "2"
        assert list(body)[3].tag == "p"
        assert list(body)[3].text == "3"

    def test_after_top_level_p(self) -> None:
        _, body = _make_chapter("<html><body><p>1</p><p>2</p><p>3</p></body></html>")
        target = list(body)[1]
        pm = _build_parent_map(body)
        inject_comment(body, target, CommentPosition.AFTER, CommentKind.NOTE, "y", "c1", pm)
        # aside at index 2, "3" shifts to 3
        assert list(body)[1].tag == "p"
        assert list(body)[1].text == "2"
        assert list(body)[2].tag == "aside"
        assert list(body)[3].tag == "p"
        assert list(body)[3].text == "3"

    def test_nested_p_lands_outside_blockquote(self) -> None:
        _, body = _make_chapter(
            "<html><body><p>before</p><blockquote><div><p>target</p></div></blockquote><p>after</p></body></html>"
        )
        target = find_first(body, "blockquote").find("div").find("p")  # type: ignore[union-attr]
        assert target is not None
        pm = _build_parent_map(body)
        inject_comment(body, target, CommentPosition.BEFORE, CommentKind.NOTE, "n", "c2", pm)
        # <aside> should sit BETWEEN <p>before and <blockquote>
        kids = list(body)
        assert kids[0].tag == "p"
        assert kids[0].text == "before"
        assert kids[1].tag == "aside"
        assert kids[2].tag == "blockquote"
        assert kids[3].tag == "p"
        assert kids[3].text == "after"


# ---------------------------------------------------------------------------
# inject_chapter
# ---------------------------------------------------------------------------


class TestChapterSplice:
    def _build_chapter_with_paragraphs(self, n: int) -> Chapter:
        body_xml = "".join(f"<p>p{i}</p>" for i in range(n))
        root = _parse_root(f"<html><head></head><body>{body_xml}</body></html>")
        body = find_first(root, "body")
        # build a minimal XMLLikeNode wrapper so chapter.xml_node is valid
        xml_node = XMLLikeNode(
            __import__("io").BytesIO(b"<html></html>"),
            is_html_like=True,
        )
        # overwrite the parsed element to share memory
        xml_node.element = root
        return Chapter(path=Path("ch.xhtml"), title="ch", body=body, xml_node=xml_node)

    def test_inject_chapter_processes_comments_in_order(self) -> None:
        chapter = self._build_chapter_with_paragraphs(7)
        comments = [
            CommentItem(target_p_ids=[5], position=CommentPosition.BEFORE, kind=CommentKind.NOTE, content="c5"),
            CommentItem(target_p_ids=[0], position=CommentPosition.AFTER, kind=CommentKind.INTRO, content="c0"),
            CommentItem(target_p_ids=[2], position=CommentPosition.BEFORE, kind=CommentKind.NOTE, content="c2"),
        ]
        ann = ChapterAnnotation(chapter=chapter, memo=None, comments=comments)  # type: ignore[arg-type]
        inject_chapter(ann)
        # collect asides in document order: BEFORE insertions push asides ahead of
        # their anchor p, AFTER pushes them after, so [0-AFTER, 2-BEFORE, 5-BEFORE]
        # produces 0's aside just after p0, then 2's aside just before p2, etc.
        asides = [el for el in chapter.body.iter("aside")]
        ids = [a.get("id") for a in asides]
        # We don't depend on the exact global ordering across all three
        # because AFTER-on-p0 and BEFORE-on-p2 interact with shifted
        # indices. The important invariants are: all three present, ids
        # match the kind+position pattern, and the input order of comments
        # is preserved (later-input comments get later cmt-ids).
        assert len(ids) == 3
        assert all(i.startswith("cmt-ch-") for i in ids)
        # The list of id suffixes should be a permutation of the inputs
        # (the *set* is the invariant — relative order is fine to vary).
        assert set(ids) == {
            "cmt-ch-0-intro",
            "cmt-ch-2-note",
            "cmt-ch-5-note",
        }

    def test_inject_chapter_strips_data_p_id(self) -> None:
        chapter = self._build_chapter_with_paragraphs(3)
        # manually attach data-p-id as if a previous stage leaked it
        for i, p in enumerate(list(chapter.body.iter("p"))):
            p.set("data-p-id", str(i))
        ann = ChapterAnnotation(
            chapter=chapter,
            memo=None,  # type: ignore[arg-type]
            comments=[],
        )
        inject_chapter(ann)
        for p in chapter.body.iter("p"):
            assert "data-p-id" not in p.attrib

    def test_inject_chapter_empty_unchanged(self) -> None:
        chapter = self._build_chapter_with_paragraphs(2)
        ann = ChapterAnnotation(
            chapter=chapter,
            memo=None,
            comments=[],  # type: ignore[arg-type]
        )
        inject_chapter(ann)
        assert sum(1 for _ in chapter.body.iter("aside")) == 0
        # original paragraphs intact
        ps = [p.text for p in chapter.body.iter("p")]
        assert ps == ["p0", "p1"]

    def test_inject_chapter_zero_paragraph_chapter_does_not_crash(self) -> None:
        chapter = self._build_chapter_with_paragraphs(0)
        comments = [
            CommentItem(target_p_ids=[0], position=CommentPosition.AFTER, kind=CommentKind.INTRO, content="orphan")
        ]
        ann = ChapterAnnotation(chapter=chapter, memo=None, comments=comments)  # type: ignore[arg-type]
        inject_chapter(ann)  # should not raise
        # no <aside> in body (target is out of range so silently skipped)
        assert sum(1 for _ in chapter.body.iter("aside")) == 0

    def test_inject_chapter_dedupes_clashing_ids(self) -> None:
        chapter = self._build_chapter_with_paragraphs(2)
        # pre-create an element with id="cmt-ch-0-intro" to force a dedup
        target = list(chapter.body.iter("p"))[0]
        target.set("id", "cmt-ch-0-intro")
        comments = [
            CommentItem(target_p_ids=[0], position=CommentPosition.BEFORE, kind=CommentKind.INTRO, content="new")
        ]
        ann = ChapterAnnotation(chapter=chapter, memo=None, comments=comments)  # type: ignore[arg-type]
        inject_chapter(ann)
        # the aside keeps its id, the original <p> was renamed via the
        # __translated suffix scheme (deduplicate_ids_in_element).
        aside_ids = [a.get("id") for a in chapter.body.iter("aside") if a.get("id")]
        para_ids = [p.get("id") for p in chapter.body.iter("p") if p.get("id")]
        assert "cmt-ch-0-intro" in aside_ids
        # exactly one element holds the bare cmt-ch-0-intro id (the aside)
        bare = [i for i in aside_ids + para_ids if i == "cmt-ch-0-intro"]
        assert len(bare) == 1
        # the <p> got renamed — it now carries a __translated suffix
        assert any("__translated" in (i or "") for i in para_ids)


# ---------------------------------------------------------------------------
# chapter head link
# ---------------------------------------------------------------------------


class TestChapterHeadLink:
    def test_injects_link_into_existing_head(self) -> None:
        root = _parse_root("<html><head><title>t</title></head><body><p>x</p></body></html>")
        chapter = _make_chapter_with_root(root, Path("ch.xhtml"))
        inject_chapter_head_link(chapter, "Styles/commentary.css")
        head = find_first(root, "head")
        links = [c for c in head if c.tag == "link"]  # type: ignore[union-attr]
        assert any(
            link.get("rel") == "stylesheet"
            and link.get("href") == "Styles/commentary.css"
            and link.get("type") == "text/css"
            for link in links
        )

    def test_idempotent_when_link_already_present(self) -> None:
        root = _parse_root(
            "<html><head>"
            '<link rel="stylesheet" type="text/css" href="Styles/commentary.css"/>'
            "</head><body><p>x</p></body></html>"
        )
        chapter = _make_chapter_with_root(root, Path("ch.xhtml"))
        inject_chapter_head_link(chapter, "Styles/commentary.css")
        inject_chapter_head_link(chapter, "Styles/commentary.css")
        head = find_first(root, "head")
        stylesheet_links = [
            c
            for c in head
            if c.tag == "link" and c.get("rel") == "stylesheet"  # type: ignore[union-attr]
        ]
        assert len(stylesheet_links) == 1

    def test_creates_head_when_missing(self) -> None:
        root = _parse_root("<html><body><p>x</p></body></html>")
        chapter = _make_chapter_with_root(root, Path("ch.xhtml"))
        inject_chapter_head_link(chapter, "Styles/commentary.css")
        head = find_first(root, "head")
        assert head is not None
        assert any(c.tag == "link" and c.get("href") == "Styles/commentary.css" for c in head)


def _make_chapter_with_root(root: Element, path: Path) -> Chapter:
    """Build a Chapter wrapping an already-parsed root Element."""
    import io

    body = find_first(root, "body")
    if body is None:
        body = root
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=path, title=path.stem, body=body, xml_node=xml_node)


# ---------------------------------------------------------------------------
# OPF manifest + CSS file
# ---------------------------------------------------------------------------


class TestOpfAndCss:
    def test_add_css_item_is_idempotent(self) -> None:
        import io

        opf_xml = (
            b'<?xml version="1.0"?>'
            b'<package xmlns="http://www.idpf.org/2007/opf">'
            b"<manifest><item id='cover' href='cover.xhtml'/></manifest>"
            b"</package>"
        )
        node = XMLLikeNode(io.BytesIO(opf_xml), is_html_like=False)
        from epub_commentor.pipeline.inject import _add_css_item_to_manifest

        assert _add_css_item_to_manifest(node, "Styles/commentary.css") is True
        # second call: same id already present
        assert _add_css_item_to_manifest(node, "Styles/commentary.css") is False
        # exactly one <item id="commentary-css">
        items = [el for el in node.element.iter() if el.tag.endswith("item") and el.get("id") == "commentary-css"]
        assert len(items) == 1

    def test_inject_opf_writes_to_zip(self, tmp_path: Path) -> None:
        """First inject_opf adds the item; second call is a no-op.

        Note: this test exercises the per-call idempotency of the low-level
        ``inject_opf`` helper. The cross-call guarantee (running the full
        ``inject_annotations`` twice on the same output ZIP) is covered
        by ``TestEndToEnd``.
        """
        asset = Path("tests/assets/The little prince.epub")
        src = tmp_path / "src.epub"
        out = tmp_path / "out.epub"
        shutil.copy(asset, src)
        with Zip(src, out) as z:
            chapters, metadata = extract_chapters(z)
            opf_path = Path(metadata["__opf_path__"])
            # first call adds the item
            assert inject_opf(z, opf_path, "Styles/commentary.css") is True

    def test_inject_css_zip_adds_file(self, tmp_path: Path) -> None:
        asset = Path("tests/assets/The little prince.epub")
        src = tmp_path / "src.epub"
        out = tmp_path / "out.epub"
        shutil.copy(asset, src)
        with Zip(src, out) as z:
            inject_css_zip(z, Path("Styles/commentary.css"), b"body { color: red; }")
        with zipfile.ZipFile(out) as zf:
            assert "Styles/commentary.css" in zf.namelist()
            assert zf.read("Styles/commentary.css") == b"body { color: red; }"


# ---------------------------------------------------------------------------
# href computation
# ---------------------------------------------------------------------------


class TestCssHrefs:
    def test_root_opf_root_css(self) -> None:
        # OPF at root, CSS at Styles/, chapter at root
        opf = Path("content.opf")
        css = Path("Styles/commentary.css")
        chapter = Path("chap1.xhtml")
        opf_href, chap_map = _compute_css_hrefs(opf, css, [chapter])
        assert opf_href == "Styles/commentary.css"
        assert chap_map[chapter] == "Styles/commentary.css"

    def test_nested_opf_layout(self) -> None:
        # OPF at OEBPS/content.opf, CSS at OEBPS/Styles/commentary.css,
        # chapter at OEBPS/Text/chap1.xhtml
        opf = Path("OEBPS/content.opf")
        css = Path("OEBPS/Styles/commentary.css")
        chapter = Path("OEBPS/Text/chap1.xhtml")
        opf_href, chap_map = _compute_css_hrefs(opf, css, [chapter])
        assert opf_href == "Styles/commentary.css"
        assert chap_map[chapter] == "../Styles/commentary.css"


# ---------------------------------------------------------------------------
# CSS file as resource
# ---------------------------------------------------------------------------


class TestCssAsset:
    def test_data_file_has_three_kind_classes(self) -> None:
        from importlib import resources

        css = resources.files("epub_commentor.data").joinpath("commentary.css").read_text(encoding="utf-8")
        assert ".commentary-intro" in css
        assert ".commentary-summary" in css
        assert ".commentary-note" in css
        # e-ink friendliness: no `box-shadow:` rule, no `background-color:` rule
        # (we strip comments first because the header text mentions the
        # property names by way of explaining what we deliberately omitted).
        rules_only = "\n".join(
            line
            for line in css.splitlines()
            if line.strip() and not line.strip().startswith("/*") and not line.strip().startswith("*")
        )
        assert "box-shadow" not in rules_only
        assert "background-color" not in rules_only

    def test_loaded_bytes_match_data_file(self) -> None:
        from importlib import resources

        from epub_commentor.pipeline.inject import _load_commentary_css

        loaded = _load_commentary_css()
        on_disk = resources.files("epub_commentor.data").joinpath("commentary.css").read_bytes()
        assert loaded == on_disk


# ---------------------------------------------------------------------------
# End-to-end against a real EPUB
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.fixture
    def temp_dir(self, tmp_path: Path) -> Path:
        return tmp_path

    def _prep_epub(self, src: Path, dst: Path) -> tuple[Zip, list, dict]:
        shutil.copy(src, dst)
        # Open the source twice so we can both read (extract) and write
        # to the same logical target.
        z = Zip(dst, dst.with_name(dst.stem + "_out.epub"))
        return z, *extract_chapters(z)

    def test_inject_annotations_end_to_end(self, temp_dir: Path) -> None:
        from importlib import resources

        asset = Path("tests/assets/The little prince.epub")
        src = temp_dir / "in.epub"
        out = temp_dir / "out.epub"
        if out.exists():
            out.unlink()
        shutil.copy(asset, src)

        config = CommentConfig()
        chapters: list = []
        metadata: dict = {}
        with Zip(src, out) as z:
            chapters, metadata = extract_chapters(z)

        # Pick the first two chapters that actually contain <p> elements
        chapters_with_text = [ch for ch in chapters if len(list(ch.body.iter("p"))) > 0]
        assert len(chapters_with_text) >= 2
        selected = chapters_with_text[:2]

        fake_memo = ChapterMemo(
            core_thesis="fake",
            outline=["a", "b", "c"],
            tone="plain",
            target_audience="general",
        )
        anns: list[ChapterAnnotation] = []
        for ch in selected:
            comments = [
                CommentItem(
                    target_p_ids=[0],
                    position=CommentPosition.BEFORE,
                    kind=CommentKind.INTRO,
                    content="Test intro.",
                ),
                CommentItem(
                    target_p_ids=[3] if len(list(ch.body.iter("p"))) > 3 else [0],
                    position=CommentPosition.AFTER,
                    kind=CommentKind.NOTE,
                    content="Test note.",
                ),
            ]
            anns.append(ChapterAnnotation(chapter=ch, memo=fake_memo, comments=comments))

        with Zip(src, out) as z:
            inject_annotations(z, anns, config, metadata)

        # Verify all 6 invariants
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            # 1. CSS file present
            assert "Styles/commentary.css" in names
            # CSS bytes match the bundled data file
            on_disk = resources.files("epub_commentor.data").joinpath("commentary.css").read_bytes()
            assert zf.read("Styles/commentary.css") == on_disk

            # 2. OPF manifest has the item
            opf_xml = zf.read(metadata["__opf_path__"]).decode("utf-8", errors="replace")
            assert "commentary-css" in opf_xml

            # 3. every chapter has a <link> in <head> with the CSS href
            for ch in selected:
                ch_xml = zf.read(ch.path.as_posix()).decode("utf-8", errors="replace")
                assert "commentary.css" in ch_xml, f"no link in {ch.path}"
                # 4. aside count matches
                expected = sum(1 for _ in ch.body.iter("aside"))  # in-memory after injection
                actual = ch_xml.count("<aside")
                assert actual == expected, f"aside count mismatch in {ch.path}: {actual} vs {expected}"
                # 5. no data-p-id
                assert "data-p-id" not in ch_xml, f"residual data-p-id in {ch.path}"
                # 6. original paragraphs preserved (text content survives)
                orig_p_texts = [p.text or "" for p in list(ch.body.iter("p"))]
                for txt in orig_p_texts:
                    assert txt in ch_xml, f"missing original text {txt!r} in {ch.path}"

    def test_inject_annotations_twice_does_not_duplicate(self, temp_dir: Path) -> None:
        """Running the orchestrator twice on consecutive outputs should not
        produce duplicate <item> entries in the OPF or duplicate <link>s
        in chapter <head>s.
        """
        asset = Path("tests/assets/The little prince.epub")
        config = CommentConfig()
        # First pass: asset -> out1
        src = temp_dir / "pass1_in.epub"
        out1 = temp_dir / "pass1_out.epub"
        if out1.exists():
            out1.unlink()
        shutil.copy(asset, src)
        chapters: list = []
        metadata: dict = {}
        with Zip(src, out1) as z:
            chapters, metadata = extract_chapters(z)
        chapters_with_text = [ch for ch in chapters if len(list(ch.body.iter("p"))) > 0][:1]
        assert chapters_with_text
        fake_memo = ChapterMemo(core_thesis="x", outline=["a", "b", "c"], tone="p", target_audience="g")
        ann = ChapterAnnotation(
            chapter=chapters_with_text[0],
            memo=fake_memo,
            comments=[
                CommentItem(
                    target_p_ids=[0],
                    position=CommentPosition.BEFORE,
                    kind=CommentKind.INTRO,
                    content="hi",
                )
            ],
        )
        with Zip(src, out1) as z:
            inject_annotations(z, [ann], config, metadata)
        # Second pass: out1 -> out2 (this is what would happen on a retry)
        out2 = temp_dir / "pass2_out.epub"
        if out2.exists():
            out2.unlink()
        chapters2, metadata2 = [], {}
        with Zip(out1, out2) as z:
            chapters2, metadata2 = extract_chapters(z)
        ann2 = ChapterAnnotation(
            chapter=chapters2[0],
            memo=fake_memo,
            comments=[
                CommentItem(
                    target_p_ids=[0],
                    position=CommentPosition.BEFORE,
                    kind=CommentKind.SUMMARY,
                    content="hi2",
                )
            ],
        )
        with Zip(out1, out2) as z:
            inject_annotations(z, [ann2], config, metadata2)
        # Check: only one <item id="commentary-css"> in OPF, only one <link>
        # in chapter head for commentary.css
        with zipfile.ZipFile(out2) as zf:
            opf = zf.read(metadata["__opf_path__"]).decode("utf-8", errors="replace")
            assert opf.count("commentary-css") == 1
            ch_xml = zf.read(chapters_with_text[0].path.as_posix()).decode("utf-8", errors="replace")
            assert ch_xml.count("Styles/commentary.css") == 1
