"""End-to-end tests for :mod:`epub_commentor.pipeline.process` driven by
the :class:`MockLLM` test double.

Covers:

* Full chapter processing produces a :class:`ChapterAnnotation` with
  the expected memo and comments.
* Block-local p_ids in Stage 2 are translated to absolute paragraph
  indices in the returned ``ChapterAnnotation.comments``.
* Multi-block chapters handle the offset correctly.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor import comment_epub
from epub_commentor.config import CommentConfig
from epub_commentor.errors import CommentInvalidJSONError
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.pipeline.process import ChapterAnnotation, process_chapters
from epub_commentor.progress import ProgressEvent
from epub_commentor.xml.xml_like import XMLLikeNode


def _mk_chapter(n_paragraphs: int, path: str = "ch.xhtml") -> Chapter:
    body_xml = "".join(f"<p>p{i}</p>" for i in range(n_paragraphs))
    root = fromstring(f"<html><body>{body_xml}</body></html>")
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=Path(path), title=path, body=body, xml_node=xml_node)


def _memo_json() -> str:
    return json_dumps(
        {
            "core_thesis": "x",
            "outline": ["a", "b", "c"],
            "tone": "t",
            "target_audience": "g",
        }
    )


class TestProcessChapters:
    def test_single_chapter_single_block(self) -> None:
        chapter = _mk_chapter(3)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1
        ann = anns[0]
        assert isinstance(ann, ChapterAnnotation)
        assert ann.memo.core_thesis == "x"
        assert len(ann.comments) == 1
        assert ann.comments[0].target_p_ids == [0]

    def test_block_local_p_ids_translated_to_absolute(self) -> None:
        # 8 paragraphs, block_size=4 -> 2 blocks of (p0..p3) and (p4..p7)
        # The mock Stage 2 returns block-local p_id=0 for block 1, which
        # should land at absolute index 4.
        chapter = _mk_chapter(8)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(block_size=4))
        # Both blocks return one comment; the block-0 comment is at p0,
        # the block-1 comment is at p4 (absolute).
        abs_pids = sorted(c.target_p_ids[0] for c in anns[0].comments)
        assert abs_pids == [0, 4]

    def test_comments_sorted_by_p_id(self) -> None:
        chapter = _mk_chapter(8)
        # Block 0 returns a comment on p_id 3, Block 1 on p_id 0.
        # Both blocks share the same `annotate__response` because the
        # mock dispatches by stage prefix. The contents are identical
        # ("samelabel") for both; what we verify is the absolute p_id
        # translation: block-local 3 in block 0 -> absolute 3, and
        # block-local 0 in block 1 -> absolute 4.
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "samelabel"}]}
                ),
            }
        )
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(block_size=4))
        # 2 comments (one per block), sorted by absolute p_id
        abs_pids = [c.target_p_ids[0] for c in anns[0].comments]
        assert abs_pids == [0, 4]
        # All comments carry the same content
        assert all(c.content == "samelabel" for c in anns[0].comments)

    def test_invalid_block_raises_invalid_json(self) -> None:
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
            },
            default_response="not json",
        )
        with pytest.raises(CommentInvalidJSONError):
            process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(max_json_retries=2))

    def test_empty_chapter_skipped_by_default(self) -> None:
        # 0 paragraphs
        chapter = _mk_chapter(0)
        llm = MockLLM()  # nothing should be called
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1
        assert anns[0].comments == []
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")


class TestProgressEvents:
    def test_chapter_scan_event_emitted(self) -> None:
        chapter = _mk_chapter(3)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )

        events: list[ProgressEvent] = []
        process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(),
            progress_callback=events.append,
        )

        scan_events = [e for e in events if e.stage == "process" and e.substage == "scan"]
        assert len(scan_events) == 1
        assert scan_events[0].current == 1
        assert scan_events[0].total == 1
        assert scan_events[0].message == "ch.xhtml"

    def test_block_annotate_events_emitted(self) -> None:
        # 8 paragraphs at block_size=4 → 2 blocks → 2 annotate events
        chapter = _mk_chapter(8)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )

        events: list[ProgressEvent] = []
        process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(block_size=4),
            progress_callback=events.append,
        )

        annotate_events = [e for e in events if e.stage == "process" and e.substage == "annotate"]
        # 1 zero-of-N event after splitting + 1 per block completion
        assert len(annotate_events) == 3
        assert annotate_events[0].current == 0
        assert annotate_events[0].total == 2
        assert annotate_events[-1].current == 2
        assert annotate_events[-1].total == 2

    def test_callback_exception_does_not_crash(self) -> None:
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )

        def bad(_event: ProgressEvent) -> None:
            raise RuntimeError("boom")

        # Should not raise; the callback error is logged + swallowed.
        anns = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(),
            progress_callback=bad,
        )
        assert len(anns) == 1

    def test_no_callback_keeps_legacy_behavior(self) -> None:
        chapter = _mk_chapter(3)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "c"}]}
                ),
            }
        )
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1


class TestChapterFilter:
    """``comment_epub`` honours a ``chapter_filter`` callback."""

    _ASSET = Path("tests/assets/The little prince.epub")

    def _copy_asset(self, tmp_path: Path) -> tuple[Path, Path]:
        if not self._ASSET.exists():
            pytest.skip(f"asset not found: {self._ASSET}")
        src = tmp_path / "src.epub"
        out = tmp_path / "annotated.epub"
        shutil.copy(self._ASSET, src)
        return src, out

    @staticmethod
    def _annotate_response() -> str:
        return json_dumps({"comments": [{"target_p_ids": [0], "position": "before", "kind": "intro", "content": "x"}]})

    def test_mask_keeps_subset(self, tmp_path: Path) -> None:
        """A bool mask of length N yields ``sum(mask)`` annotations."""
        src, out = self._copy_asset(tmp_path)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": self._annotate_response(),
            }
        )

        # Discover the actual chapter count so we can build a valid mask.
        from epub_commentor.epub.zip import Zip
        from epub_commentor.pipeline.extract import extract_chapters

        with Zip(src, out) as z:
            chapters, _ = extract_chapters(z)
        n = len(chapters)
        assert n >= 3, f"test asset needs >=3 chapters, got {n}"

        keep_first_last = [True] + [False] * (n - 2) + [True] if n >= 2 else [True] * n

        def _filter(_chapters: list[Chapter]) -> list[bool]:
            return keep_first_last

        result = comment_epub(
            src,
            out,
            llm=llm,
            config=CommentConfig(block_size=20),
            chapter_filter=_filter,
        )
        assert len(result.annotations) == sum(keep_first_last)

    def test_mask_all_false_runs_without_raising(self, tmp_path: Path) -> None:
        """Dropping every chapter returns an empty ``CommentorResult`` cleanly."""
        src, out = self._copy_asset(tmp_path)
        llm = MockLLM(responses_by_seed={"scan__response": _memo_json()})

        result = comment_epub(
            src,
            out,
            llm=llm,
            config=CommentConfig(block_size=20),
            chapter_filter=lambda cs: [False] * len(cs),
        )
        assert result.chapters_processed == 0
        assert result.chapters_skipped == 0
        assert result.annotations == []
        assert result.total_comments == 0

    def test_mask_length_mismatch_raises(self, tmp_path: Path) -> None:
        """Wrong-length mask is a programmer error: ``ValueError``."""
        src, out = self._copy_asset(tmp_path)
        llm = MockLLM(responses_by_seed={"scan__response": _memo_json()})

        with pytest.raises(ValueError, match=r"parallel list\[bool\]"):
            comment_epub(
                src,
                out,
                llm=llm,
                config=CommentConfig(block_size=20),
                chapter_filter=lambda cs: [True, False],
            )

    def test_mask_non_bool_elements_raise(self, tmp_path: Path) -> None:
        """Non-bool elements are rejected just like wrong length."""
        src, out = self._copy_asset(tmp_path)
        llm = MockLLM(responses_by_seed={"scan__response": _memo_json()})

        def _bad(_cs: list[Chapter]) -> list[int]:
            return [1, 0, 1]

        with pytest.raises(ValueError, match=r"parallel list\[bool\]"):
            comment_epub(
                src,
                out,
                llm=llm,
                config=CommentConfig(block_size=20),
                chapter_filter=_bad,
            )

    def test_callback_receives_spine_ordered_list(self, tmp_path: Path) -> None:
        """The callback sees chapters in spine order — same as ``extract_chapters``."""
        src, out = self._copy_asset(tmp_path)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": self._annotate_response(),
            }
        )

        from epub_commentor.epub.zip import Zip
        from epub_commentor.pipeline.extract import extract_chapters

        with Zip(src, out) as z:
            original, _ = extract_chapters(z)

        seen: list[str] = []

        def _record(cs: list[Chapter]) -> list[bool]:
            seen.extend(c.path.as_posix() for c in cs)
            return [True] * len(cs)

        comment_epub(
            src,
            out,
            llm=llm,
            config=CommentConfig(block_size=20),
            chapter_filter=_record,
        )
        assert seen == [c.path.as_posix() for c in original]
