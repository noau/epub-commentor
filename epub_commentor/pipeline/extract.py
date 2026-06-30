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

from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ElementTree as ET

from ..epub.common import find_opf_path
from ..epub.metadata import read_metadata
from ..epub.spines import search_spine_paths
from ..epub.zip import Zip
from ..xml import XMLLikeNode, find_first


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


def _first_text(elem: Element) -> str:
    """Recursively collect the first chunk of text inside ``elem``."""
    if elem.text and elem.text.strip():
        return elem.text.strip()
    for child in elem:
        chunk = _first_text(child)
        if chunk:
            return chunk
    return ""


def _derive_title(body: Element, chapter_path: Path) -> str:
    """Pick a human-readable title for the chapter.

    Falls back through: ``<title>`` → first heading → file stem. Always
    returns a non-empty string.
    """
    html = body.getroot() if isinstance(body, ET) else None
    root = html if html is not None else body
    for heading_tag in ("title", "h1", "h2", "h3"):
        node = find_first(root, heading_tag)
        if node is not None:
            text = _first_text(node)
            if text:
                return text
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


__all__ = ["Chapter", "extract_chapters"]
