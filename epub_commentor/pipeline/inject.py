"""Splice LLM-produced commentary into chapter DOMs and wire the CSS file.

The inject layer is the third and final stage of the pipeline. It takes the
``ChapterAnnotation`` objects produced by :mod:`~epub_commentor.pipeline.process`
and mutates the in-memory chapter DOMs in place:

1. Build a child→parent map of each chapter body so nested ``<p>`` elements
   can be mapped to their body-direct ancestor (avoids splicing an
   ``<aside>`` into a ``<blockquote>`` deep inside the body).
2. For each ``CommentItem``, insert an ``<aside class="commentary commentary-{kind}">``
   right before or after the anchor paragraph, anchoring on
   ``target_p_ids[0]`` (the rest of the contiguous range is documented
   in the aside's text but not used for placement).
3. Strip any residual ``data-p-id`` attributes and run
   :func:`deduplicate_ids_in_element` so the new ``cmt-...`` ids never
   collide with the book's existing id namespace.
4. Add a ``<link rel="stylesheet">`` to each chapter's ``<head>`` so the
   reader can pick up ``commentary.css``.
5. Patch the OPF ``<manifest>`` to register the CSS file, and add the CSS
   file itself to the target ZIP via :meth:`Zip.add`.
6. Serialise every mutated chapter back to the target ZIP.

All chapter mutations are pure in-memory until the orchestrator explicitly
calls :meth:`XMLLikeNode.save` through :meth:`Zip.replace`; this makes
injection safe to retry on failure (OPF and CSS are committed first, so
a chapter-level failure never leaves a half-written file behind).
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement

from ..config import CommentConfig
from ..epub.common import find_opf_path
from ..epub.zip import Zip
from ..llm.schema import CommentKind, CommentPosition
from ..xml import deduplicate_ids_in_element, find_first, iter_with_stack
from ..xml.xml_like import XMLLikeNode
from .extract import Chapter
from .process import ChapterAnnotation

# Tag / attribute names used inside chapters and the OPF manifest.
_ASIDE_TAG = "aside"
_PARAGRAPH_TAG = "p"
_HEAD_TAG = "head"
_LINK_TAG = "link"
_BREAK_TAG = "br"

# CSS-related constants. The OPF item id and the link's rel value are
# fixed so a re-run on an already-injected EPUB is idempotent.
_COMMENTARY_CLASS = "commentary"
_CSS_ITEM_ID = "commentary-css"
_CSS_MEDIA_TYPE = "text/css"
_CSS_REL = "stylesheet"
_MANIFEST_TAG = "manifest"
_ITEM_TAG = "item"

# Marker attributes kept internal to the pipeline; we strip them in
# inject_chapter as a defensive safety net (annotate_block already strips
# its own copies, but bugs in future stages shouldn't leak through).
_DATA_P_ID = "data-p-id"

# Reserved metadata key under which extract_chapters stashes the OPF path
# so we don't have to re-parse the container just to find it.
_OPF_PATH_KEY = "__opf_path__"


def _build_parent_map(body: Element) -> dict[Element, Element]:
    """Return a ``child → parent`` map for every element under ``body``.

    ElementTree's ``Element`` does not provide a ``getparent()`` method, so
    we walk the tree once with :func:`iter_with_stack` and record each
    parent→child relationship.
    """
    parent_map: dict[Element, Element] = {}
    for _, node in iter_with_stack(body):
        for child in node:
            parent_map[child] = node
    return parent_map


def _find_body_direct_ancestor(
    target_p: Element,
    body: Element,
    parent_map: dict[Element, Element],
) -> Element:
    """Walk ``target_p`` up the parent chain to the body-direct container.

    Returns the **first element whose parent is ``body``**. For a
    ``<p>`` directly inside ``<body>`` that is the ``<p>`` itself. For a
    ``<p>`` nested inside ``<blockquote><div>`` this returns the
    ``<blockquote>`` so the injected ``<aside>`` lands as a sibling of
    the block container, never inside it.

    Falls back to ``body`` if the walk exhausts without finding a
    body-level ancestor (degenerate 0-paragraph chapter case).
    """
    cursor: Element | None = target_p
    while cursor is not None and cursor is not body:
        parent = parent_map.get(cursor)
        if parent is body:
            return cursor
        cursor = parent
    return body


def _split_paragraphs(text: str) -> list[str]:
    """Split ``text`` on blank lines into paragraph chunks."""
    return [chunk for chunk in text.split("\n\n") if chunk.strip()]


def _make_aside_simple(kind: CommentKind, content: str, cmt_id: str) -> Element:
    """Build an ``<aside>`` with one ``<p>`` per blank-line-separated chunk.

    Single ``\\n`` inside a chunk becomes a ``<br/>`` so the LLM can
    author multi-line notes without us imposing paragraph boundaries.
    """
    aside = Element(
        _ASIDE_TAG,
        {
            "class": f"{_COMMENTARY_CLASS} {_COMMENTARY_CLASS}-{kind.value}",
            "id": f"cmt-{cmt_id}",
        },
    )
    for chunk in _split_paragraphs(content):
        p = SubElement(aside, _PARAGRAPH_TAG)
        lines = chunk.split("\n")
        p.text = lines[0]
        for line in lines[1:]:
            br = SubElement(p, _BREAK_TAG)
            br.tail = line
    return aside


def inject_comment(
    body: Element,
    target_p: Element,
    position: CommentPosition,
    kind: CommentKind,
    content: str,
    cmt_id: str,
    parent_map: dict[Element, Element],
) -> None:
    """Insert one ``<aside>`` adjacent to ``target_p`` according to ``position``.

    ``parent_map`` must cover the entire ``body`` subtree (typically built
    once per chapter via :func:`_build_parent_map` and shared across all
    comments in that chapter). The caller is responsible for handing in a
    stable id (used for the DOM ``id="cmt-..."`` attribute).
    """
    anchor = _find_body_direct_ancestor(target_p, body, parent_map)
    aside = _make_aside_simple(kind, content, cmt_id)

    if anchor is body:
        # Degenerate case: the body has no children, or the target is
        # somehow the body itself. Append the aside as a direct body child.
        body.append(aside)
        return

    parent = parent_map.get(anchor, body)
    insert_at = list(parent).index(anchor)
    if position is CommentPosition.AFTER:
        insert_at += 1
    parent.insert(insert_at, aside)


def inject_chapter(
    annotation: ChapterAnnotation,
    parent_map_holder: dict[int, dict[Element, Element]] | None = None,
) -> None:
    """Mutate ``annotation.chapter.body`` in place to add every comment.

    The mapping from ``CommentItem.target_p_ids[0]`` to an actual
    ``<p>`` element is ``list(body.iter("p"))[pid]`` — the absolute
    paragraph index produced by :mod:`epub_commentor.pipeline.process`.
    Callers may pass a ``parent_map_holder`` to amortise the O(N) parent
    map build across the whole chapter loop (keyed on ``id(body)``).
    """
    body = annotation.chapter.body
    paragraphs = list(body.iter(_PARAGRAPH_TAG))
    cache_key = id(body)
    if parent_map_holder is not None and cache_key in parent_map_holder:
        parent_map = parent_map_holder[cache_key]
    else:
        parent_map = _build_parent_map(body)
        if parent_map_holder is not None:
            parent_map_holder[cache_key] = parent_map

    for comment in annotation.comments:
        if not comment.target_p_ids:
            continue
        target_idx = comment.target_p_ids[0]
        if target_idx < 0 or target_idx >= len(paragraphs):
            # Defensive: a comment whose anchor is out of range is silently
            # skipped. process.py's range check + our absolute p_id invariant
            # should make this branch unreachable, but an LLM that produces
            # malformed JSON after retries could land here.
            continue
        target_p = paragraphs[target_idx]
        cmt_id = f"{annotation.chapter.path.stem}-{target_idx}-{comment.kind.value}"
        inject_comment(
            body=body,
            target_p=target_p,
            position=comment.position,
            kind=comment.kind,
            content=comment.content,
            cmt_id=cmt_id,
            parent_map=parent_map,
        )

    # Defensive strip of any leftover marker. M2's annotate_block already
    # strips its own, but extra passes (M5 mock runs, manual test rigs)
    # could leave one behind.
    for p in paragraphs:
        p.attrib.pop(_DATA_P_ID, None)

    deduplicate_ids_in_element(body)


def inject_chapter_head_link(chapter: Chapter, css_href: str) -> None:
    """Add a ``<link rel="stylesheet">`` to ``chapter``'s ``<head>``.

    Idempotent: if a link with the same ``href`` and ``rel="stylesheet"``
    is already present, this is a no-op. If the chapter has no ``<head>``
    (rare for XHTML but possible for malformed HTML), one is created as
    the first child of the root.
    """
    root = chapter.xml_node.element
    head = find_first(root, _HEAD_TAG)
    if head is None:
        head = Element(_HEAD_TAG)
        root.insert(0, head)
    for existing in head.findall(_LINK_TAG):
        if existing.get("rel") == _CSS_REL and existing.get("href") == css_href:
            return
    SubElement(
        head,
        _LINK_TAG,
        {"rel": _CSS_REL, "type": _CSS_MEDIA_TYPE, "href": css_href},
    )


def _add_css_item_to_manifest(opf_xml_node: XMLLikeNode, css_href: str) -> bool:
    """Append ``<item id="commentary-css" .../>`` to the OPF ``<manifest>``.

    Returns ``True`` if a new item was added, ``False`` if one already
    existed (idempotent re-injection). The ``<manifest>`` lookup uses the
    same ``endswith("manifest")`` suffix match as
    :mod:`epub_commentor.epub.metadata` so namespace prefixes don't
    trip us up.
    """
    root = opf_xml_node.element
    manifest: Element | None = None
    for child in root:
        if child.tag.endswith(_MANIFEST_TAG):
            manifest = child
            break
    if manifest is None:
        return False
    for item in manifest.findall(_ITEM_TAG):
        if item.get("id") == _CSS_ITEM_ID:
            return False
    SubElement(
        manifest,
        _ITEM_TAG,
        {"id": _CSS_ITEM_ID, "href": css_href, "media-type": _CSS_MEDIA_TYPE},
    )
    return True


def inject_opf(zip: Zip, opf_path: Path, css_href: str) -> bool:
    """Add the CSS ``<item>`` to the OPF manifest. Idempotent.

    Mirrors the read → mutate → save pattern from
    :func:`epub_commentor.epub.metadata.write_metadata`. Returns
    ``True`` if the manifest was patched, ``False`` if it was already
    wired (or if no ``<manifest>`` was found).
    """
    with zip.read(opf_path) as f:
        opf_xml = XMLLikeNode(f, is_html_like=False)
    added = _add_css_item_to_manifest(opf_xml, css_href)
    if not added:
        return False
    with zip.replace(opf_path) as f:
        opf_xml.save(f)
    return True


def inject_css_zip(zip: Zip, css_path: Path, css_bytes: bytes) -> None:
    """Add the CSS file as a fresh entry in the target ZIP."""
    zip.add(css_path, css_bytes)


def _load_commentary_css() -> bytes:
    """Read the bundled ``commentary.css`` from package data."""
    return resources.files("epub_commentor.data").joinpath("commentary.css").read_bytes()


def _relative_href(target: Path, base: Path) -> str:
    """Return ``target`` relative to ``base`` using POSIX separators.

    Paths inside an EPUB are always forward-slash separated, so any
    Windows-style backslashes from :func:`os.path.relpath` are rewritten.
    """
    return os.path.relpath(target.as_posix(), base.as_posix()).replace("\\", "/")


def _compute_css_hrefs(
    opf_path: Path,
    css_path_in_epub: Path,
    chapter_paths: list[Path],
) -> tuple[str, dict[Path, str]]:
    """Compute the OPF-relative and per-chapter-relative hrefs for the CSS.

    The OPF ``<item>`` href is relative to the OPF's directory. Each
    chapter's ``<link>`` href is relative to that chapter's directory.
    For an EPUB with OPF at root and CSS at ``Styles/commentary.css``,
    both hrefs collapse to the same string.
    """
    css_href_for_opf = _relative_href(css_path_in_epub, opf_path.parent)
    chapter_hrefs = {ch_path: _relative_href(css_path_in_epub, ch_path.parent) for ch_path in chapter_paths}
    return css_href_for_opf, chapter_hrefs


def inject_annotations(
    zip: Zip,
    annotations: list[ChapterAnnotation],
    config: CommentConfig,
    book_metadata: dict[str, str],
) -> None:
    """Wire CSS + splice asides + write every chapter back to ``zip``.

    Order of operations (matters for crash-safety):

    1. CSS bytes added to the target ZIP.
    2. OPF manifest patched (commit global EPUB metadata first).
    3. Per-chapter ``<aside>`` splicing loop (in-memory only).
    4. Per-chapter ``<link>`` in ``<head>`` loop (in-memory only).
    5. Per-chapter ``XMLLikeNode.save`` via ``zip.replace`` (commits the
       mutated DOMs). Skips duplicate paths defensively.

    A failure between step 5 and the final commit leaves the EPUB with
    the CSS / OPF changes intact, which is the safe partial state: the
    book still opens, the only loss is one chapter's comments.
    """
    opf_path_str = book_metadata.get(_OPF_PATH_KEY)
    opf_path = Path(opf_path_str) if opf_path_str is not None else find_opf_path(zip)

    chapter_paths = [annotation.chapter.path for annotation in annotations]
    css_href_for_opf, chapter_hrefs = _compute_css_hrefs(opf_path, config.css_path_in_epub, chapter_paths)

    if config.inject_css:
        inject_css_zip(zip, config.css_path_in_epub, _load_commentary_css())
        inject_opf(zip, opf_path, css_href_for_opf)

    parent_map_holder: dict[int, dict[Element, Element]] = {}
    for annotation in annotations:
        inject_chapter(annotation, parent_map_holder=parent_map_holder)

    for annotation in annotations:
        href = chapter_hrefs[annotation.chapter.path]
        inject_chapter_head_link(annotation.chapter, href)

    # Commit each chapter exactly once. The chapter's xml_node holds the
    # mutated tree; save() writes it back through zip.replace.
    seen: set[Path] = set()
    for annotation in annotations:
        ch = annotation.chapter
        if ch.path in seen:
            continue
        seen.add(ch.path)
        with zip.replace(ch.path) as f:
            ch.xml_node.save(f)


__all__ = [
    "inject_annotations",
    "inject_chapter",
    "inject_chapter_head_link",
    "inject_comment",
    "inject_css_zip",
    "inject_opf",
]
