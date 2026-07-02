"""Extract chapter DOMs and book metadata from an opened EPUB.

The extractor takes a live :class:`~epub_commentor.epub.Zip` and parses every
spine XHTML/HTML chapter into an :class:`XMLLikeNode`, keeping a reference to
the in-memory ``body`` element so the process layer can run on it without
re-reading the source. Book metadata is flattened to a ``dict[str, str]`` for
the LLM prompt.

We deliberately do **not** open the source ZIP ourselves — the caller owns
its lifecycle because the same ZIP will also be used by the inject layer
for ``zip.replace(...)``.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element

from ..epub.common import find_opf_path
from ..epub.metadata import read_metadata
from ..epub.spines import search_spine_paths
from ..epub.zip import Zip
from ..utils import normalize_whitespace
from ..xml import XMLLikeNode, find_first, plain_text


@dataclass
class Chapter:
    """One chapter parsed out of the EPUB.

    ``xml_node`` is kept alive so :meth:`XMLLikeNode.save` can serialise the
    mutated DOM back into the target ZIP later.
    """

    path: Path
    title: str
    body: Element
    xml_node: XMLLikeNode = field(repr=False)


# Type alias for an optional chapter-filter callback supplied by the caller.
# The callback receives the spine-ordered list of chapters and returns a
# parallel ``list[bool]`` mask: ``mask[i] = True`` keeps chapter ``i``,
# ``mask[i] = False`` drops it from the run. Dropped chapters are never
# passed to ``process_chapters``; their bytes flow through ``Zip.__exit__``
# as-is, so no restoration logic is required.
ChapterFilter = Callable[[list[Chapter]], list[bool]]


def _first_text(elem: Element) -> str:
    """Recursively collect the first chunk of text inside ``elem``.

    The returned text has its internal whitespace (incl. embedded ``\\n``)
    collapsed to single spaces so titles never carry raw line breaks.
    """
    if elem.text and elem.text.strip():
        return normalize_whitespace(elem.text).strip()
    for child in elem:
        chunk = _first_text(child)
        if chunk:
            return chunk
    return ""


# Title extraction tuning. See ``_derive_title`` for the tier contract.
_TITLE_MAX_LEN = 60
_TITLE_PREVIEW_LEN = 15
_NO_TITLE_SUFFIX = "... (no title)"

# Anchored on the property name + ':' so we tolerate any combination of
# whitespace, multi-property declarations, and case variations:
#   "font-weight:bold" | "font-weight: bold" | "font-weight: 700"
#   "FONT-WEIGHT: BOLD" | "font-size:1.2em; font-weight:700; color:red"
# Matches the keyword values ``bold`` / ``bolder`` or the numeric weights
# 600/700/800/900. 500 (medium) is intentionally not matched.
_BOLD_PATTERN = re.compile(
    r"font-weight\s*:\s*(?:bold|bolder|[6-9]00)\b",
    re.IGNORECASE,
)

# Class tokens (whitespace-separated, matched case-insensitively) that
# signal "this element is styled as bold / a heading / a title". Add more
# names here when they show up in real EPUBs that we want to recognise.
# "bold" / "strong" / "heavy" are direct weight signals; "title" /
# "header" are role signals that typically come with bold styling.
_BOLD_CLASS_TOKENS: frozenset[str] = frozenset(
    {
        "bold",
        "strong",
        "heavy",
        "title",
        "header",
    }
)


def _get_attr_ci(elem: Element, name: str) -> str | None:
    """Return the value of ``elem.attrib[name]`` (case-insensitive lookup).

    ``xml.etree.ElementTree`` preserves attribute-name case, and EPUBs in
    the wild use ``Style=`` / ``STYLE=`` as well as the canonical
    ``style=``. The common-case fast path (lowercase attribute name) is
    a single dict get; we only fall back to a linear scan when the lookup
    misses.
    """
    value = elem.attrib.get(name)
    if value is not None:
        return value
    lower = name.lower()
    for key, val in elem.attrib.items():
        if key.lower() == lower:
            return val
    return None


def _is_bold_inline(elem: Element) -> bool:
    """True iff ``elem`` is a bold inline element.

    Matches the semantic tags ``<b>`` and ``<strong>``, plus any element
    whose ``class`` attribute lists a token from
    :data:`_BOLD_CLASS_TOKENS` (e.g. ``<span class="bold">``), plus any
    element whose ``style`` attribute declares
    ``font-weight: bold|bolder|600-900``. Attribute names and values are
    compared case-insensitively, because ``xml.etree.ElementTree``
    preserves attribute name case and EPUBs in the wild use
    ``Style=`` / ``STYLE=`` / ``CLASS=`` as well as the canonical lower
    case.

    Limitation: the camelCase ``fontWeight`` form is not recognised. EPUBs
    use kebab-case ``font-weight`` in practice.
    """
    if elem.tag.lower() in ("b", "strong"):
        return True

    class_attr = _get_attr_ci(elem, "class")
    if class_attr is not None:
        tokens = class_attr.lower().split()
        if any(token in _BOLD_CLASS_TOKENS for token in tokens):
            return True

    style = _get_attr_ci(elem, "style")
    if style is None:
        return False
    return _BOLD_PATTERN.search(style) is not None


def _first_bold_inline(root: Element) -> Element | None:
    """Depth-first search for the first bold-inline element under ``root``."""
    if _is_bold_inline(root):
        return root
    for child in root:
        hit = _first_bold_inline(child)
        if hit is not None:
            return hit
    return None


def _derive_title(root: Element, chapter_path: Path) -> str:
    """Pick a human-readable title for the chapter using a three-tier chain.

    Tier 1 — heading scan: ``h1`` → ``h2`` → ``h3``. The first heading whose
    text is non-empty AND ``<=`` :data:`_TITLE_MAX_LEN` characters wins.

    Tier 2 — bold inline scan: the first ``<b>`` / ``<strong>`` / element
    whose ``class`` lists a token from :data:`_BOLD_CLASS_TOKENS` /
    element with a ``font-weight: bold|bolder|600-900`` style, again
    constrained by :data:`_TITLE_MAX_LEN`.

    Tier 3 — preview: ``plain_text(root)[:_TITLE_PREVIEW_LEN].strip()``
    concatenated with :data:`_NO_TITLE_SUFFIX`.

    Final guard: ``chapter_path.stem`` if every tier yields empty. Always
    returns a non-empty string.
    """
    # Tier 1 — heading scan
    for heading_tag in ("h1", "h2", "h3"):
        node = find_first(root, heading_tag)
        if node is None:
            continue
        text = _first_text(node)
        if text and len(text) <= _TITLE_MAX_LEN:
            return text

    # Tier 2 — bold inline scan
    node = _first_bold_inline(root)
    if node is not None:
        text = _first_text(node)
        if text and len(text) <= _TITLE_MAX_LEN:
            return text

    # Tier 3 — preview. Normalize whitespace (collapsing embedded newlines)
    # and strip first so leading whitespace in the body doesn't shrink the
    # preview to almost nothing; slice the first :data:`_TITLE_PREVIEW_LEN`
    # characters of the cleaned text afterwards.
    preview = normalize_whitespace(plain_text(root)).strip()[:_TITLE_PREVIEW_LEN]
    if preview:
        return f"{preview}{_NO_TITLE_SUFFIX}"

    return chapter_path.stem


def _parse_chapter(zip: Zip, chapter_path: Path) -> Chapter:
    with zip.read(chapter_path) as f:
        xml_node = XMLLikeNode(f, is_html_like=True)
    body = find_first(xml_node.element, "body")
    if body is None:
        # Fall back to root when the file has no body element (rare).
        body = xml_node.element
    title = _derive_title(body, chapter_path)
    return Chapter(path=chapter_path, title=title, body=body, xml_node=xml_node)


def extract_chapters(zip: Zip) -> tuple[list[Chapter], dict[str, str]]:
    """Read every spine chapter and flatten the book metadata.

    Returns a tuple of ``(chapters, metadata_dict)``. The metadata dict
    maps the OPF tag name (e.g. ``"title"``, ``"creator"``) to its first
    non-empty text value; only the first occurrence is kept because
    duplicates usually carry redundant information.
    """
    chapters: list[Chapter] = []
    for chapter_path, _media_type in search_spine_paths(zip):
        chapters.append(_parse_chapter(zip, chapter_path))

    metadata_fields, _context = read_metadata(zip)

    # We still need the OPF path for downstream injection; include it under
    # a reserved key so the process layer can pass it along to inject.
    metadata_dict: dict[str, str] = {field.tag_name: field.text for field in metadata_fields if field.text}
    # Always expose the OPF path as the last metadata entry; the process
    # layer strips it before sending to the LLM.
    metadata_dict["__opf_path__"] = find_opf_path(zip).as_posix()

    return chapters, metadata_dict


__all__ = ["Chapter", "ChapterFilter", "extract_chapters"]
