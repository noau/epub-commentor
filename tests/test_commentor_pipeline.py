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
from epub_commentor.errors import CommentInvalidJSONError, CommentScanFailedError
from epub_commentor.llm.memo import scan_chapter
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
        anns, blocks_skipped = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1
        assert blocks_skipped == 0
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
        anns, _ = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(block_size=4))
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
        anns, _ = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig(block_size=4))
        # 2 comments (one per block), sorted by absolute p_id
        abs_pids = [c.target_p_ids[0] for c in anns[0].comments]
        assert abs_pids == [0, 4]
        # All comments carry the same content
        assert all(c.content == "samelabel" for c in anns[0].comments)

    def test_invalid_block_raises_invalid_json(self) -> None:
        """When ``fail_on_block_error=True``, Stage 2 retry exhaustion
        still raises ``CommentInvalidJSONError`` (legacy behaviour)."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
            },
            default_response="not json",
        )
        with pytest.raises(CommentInvalidJSONError):
            process_chapters(
                [chapter],
                book_metadata={},
                llm=llm,
                config=CommentConfig(max_json_retries=2, fail_on_block_error=True),
            )

    def test_invalid_block_skipped_by_default(self) -> None:
        """When ``fail_on_block_error=False`` (default), Stage 2 retry
        exhaustion is logged and the block is skipped; the chapter is
        returned with an empty comments list and ``blocks_skipped == 1``."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
            },
            default_response="not json",
        )
        anns, blocks_skipped = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_json_retries=2),
        )
        assert len(anns) == 1
        assert blocks_skipped == 1
        assert anns[0].comments == []
        # memo is still the real Stage 1 result (the chapter was scanned
        # successfully; only Stage 2 failed)
        assert not anns[0].memo.core_thesis.startswith("(chapter skipped")

    def test_empty_chapter_skipped_by_default(self) -> None:
        # 0 paragraphs
        chapter = _mk_chapter(0)
        llm = MockLLM()  # nothing should be called
        anns, blocks_skipped = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1
        assert blocks_skipped == 0
        assert anns[0].comments == []
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")

    def test_scan_failure_skipped_by_default(self) -> None:
        """When Stage 1 returns malformed JSON and
        ``fail_on_block_error=False``, the chapter is marked as skipped
        (via the same placeholder memo the zero-<p> path uses) so it
        counts in ``chapters_skipped``."""
        chapter = _mk_chapter(3)
        llm = MockLLM(default_response="not json")
        anns, blocks_skipped = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1
        assert blocks_skipped == 0
        assert anns[0].comments == []
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")

    def test_scan_failure_retries_then_skips(self) -> None:
        """scan_chapter should now honour ``config.max_scan_retries``:
        after retries are exhausted, the soft-skip path produces a
        sentinel annotation (chapters_skipped += 1)."""
        chapter = _mk_chapter(3)
        llm = MockLLM(default_response="not json")
        anns, blocks_skipped = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_scan_retries=2),
        )
        assert len(anns) == 1
        assert blocks_skipped == 0
        assert anns[0].comments == []
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")

    def test_skip_chapter_on_empty_annotation_triggers_on_block_failure(self) -> None:
        """With ``skip_chapter_on_empty_annotation=True``, a single block
        failure taints the whole chapter — it's marked as skipped so the
        user can re-run with --interactive to retry just this chapter."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={"scan__response": _memo_json()},
            default_response="not json",
        )
        anns, blocks_skipped = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_json_retries=1, skip_chapter_on_empty_annotation=True),
        )
        assert len(anns) == 1
        # block failed; even though blocks_skipped still counts the failure,
        # the whole chapter is also marked skipped for retry purposes.
        assert blocks_skipped == 1
        assert anns[0].comments == []
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")

    def test_skip_chapter_on_empty_annotation_triggers_on_empty_block(self) -> None:
        """With ``skip_chapter_on_empty_annotation=True``, a successful
        block that returns an empty ``comments`` list also taints the
        whole chapter."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps({"comments": []}),
            },
        )
        anns, blocks_skipped = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(skip_chapter_on_empty_annotation=True),
        )
        assert len(anns) == 1
        assert blocks_skipped == 0  # no block "failed", just returned empty
        assert anns[0].comments == []
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")

    def test_skip_chapter_on_empty_annotation_default_keeps_partial_success(self) -> None:
        """Without the flag, a partial-success chapter (some blocks
        succeed, some fail) keeps its successful comments and is NOT
        marked as skipped."""
        # 8 paragraphs at block_size=4 → 2 blocks. Block 0 fails; block 1
        # succeeds with one comment. Without the new flag, the chapter
        # ends up with one comment, not a skipped sentinel.
        chapter = _mk_chapter(8)

        call_counter = {"n": 0}

        def flaky_route(seed, messages):
            # First annotate call returns garbage; subsequent ones succeed.
            if ":annotate:" in seed and call_counter["n"] == 0:
                call_counter["n"] += 1
                return "not json"
            if ":annotate:" in seed:
                return json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "ok"}]}
                )
            # scan path
            return _memo_json()

        llm = MockLLM(responses_by_seed={"scan__response": _memo_json()})
        llm._route = flaky_route  # type: ignore[assignment]

        anns, blocks_skipped = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_json_retries=1, block_size=4),
        )
        assert len(anns) == 1
        assert blocks_skipped == 1
        # Without skip_chapter_on_empty_annotation, the chapter keeps
        # the partial success and is NOT marked skipped.
        assert anns[0].comments != []
        assert not anns[0].memo.core_thesis.startswith("(chapter skipped")


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
        anns, _ = process_chapters(
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
        anns, _ = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert len(anns) == 1

    def test_warn_events_emitted_on_block_skip(self) -> None:
        """When a Stage 2 block fails and ``fail_on_block_error=False``,
        a ``stage="warn"`` event is emitted to the progress callback
        (which the rich display renders via ``Console.log``)."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={"scan__response": _memo_json()},
            default_response="not json",
        )
        events: list[ProgressEvent] = []
        process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_json_retries=1),
            progress_callback=events.append,
        )
        warn_events = [e for e in events if e.stage == "warn"]
        assert len(warn_events) == 1
        assert "block" in (warn_events[0].message or "")
        # The warn message must include the exception class name so the
        # user can see *why* the block was skipped without opening the
        # debug log file.
        assert "CommentInvalidJSONError" in warn_events[0].message

    def test_warn_event_emitted_on_empty_chapter(self) -> None:
        """An empty chapter (zero <p>) emits a ``stage="warn"`` event
        so rich users see the skip (it previously only logged to the
        Python logger)."""
        chapter = _mk_chapter(0)
        llm = MockLLM()  # nothing should be called
        events: list[ProgressEvent] = []
        process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(),
            progress_callback=events.append,
        )
        warn_events = [e for e in events if e.stage == "warn"]
        assert len(warn_events) == 1
        assert "<p>" in (warn_events[0].message or "")

    def test_warn_event_emitted_on_scan_failure(self) -> None:
        """A Stage 1 scan failure emits a warn event whose message
        includes the underlying exception class name."""
        chapter = _mk_chapter(3)
        llm = MockLLM(default_response="not json")
        events: list[ProgressEvent] = []
        process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_scan_retries=1),
            progress_callback=events.append,
        )
        warn_events = [e for e in events if e.stage == "warn"]
        assert len(warn_events) == 1
        assert "scan failed" in warn_events[0].message
        assert "CommentScanFailedError" in warn_events[0].message


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


class TestScanChapter:
    """Direct unit tests for :func:`epub_commentor.llm.memo.scan_chapter`.

    Mirrors ``TestAnnotateBlock``'s retry contract: scan_chapter should
    honour ``config.max_scan_retries``, recover when the LLM produces
    valid JSON on a later attempt, and raise ``CommentScanFailedError``
    only after all retries are exhausted.
    """

    def test_retry_recovers_after_invalid_response(self) -> None:
        chapter = _mk_chapter(2)
        llm = MockLLM(responses_by_seed={"scan__response": _memo_json()})
        real_route = llm._route
        calls = {"n": 0}

        def flaky_route(seed, messages):
            if ":scan:" in seed:
                calls["n"] += 1
                if calls["n"] == 1:
                    return "not json"
            return real_route(seed, messages)

        llm._route = flaky_route  # type: ignore[assignment]

        memo = scan_chapter(
            body=chapter.body,
            chapter_path=chapter.path,
            chapter_title=chapter.title,
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_scan_retries=3),
        )
        assert memo.core_thesis == "x"
        assert calls["n"] == 2  # 1 bad + 1 good

    def test_retry_exhausted_raises_scan_failed(self) -> None:
        chapter = _mk_chapter(2)
        llm = MockLLM(default_response="not json")
        with pytest.raises(CommentScanFailedError):
            scan_chapter(
                body=chapter.body,
                chapter_path=chapter.path,
                chapter_title=chapter.title,
                book_metadata={},
                llm=llm,
                config=CommentConfig(max_scan_retries=2),
            )


class TestChapterAnnotationFields:
    """Per-chapter ``skipped_blocks`` / ``has_empty_blocks`` fields.

    These two counters are surfaced to the post-process review gate so the
    user can see how many Stage 2 blocks failed JSON validation
    (``skipped_blocks``) versus returned a valid-but-empty
    ``{"comments": []}`` response (``has_empty_blocks``). Both default to
    0; both should be set on the ``ChapterAnnotation`` regardless of
    whether ``config.skip_chapter_on_empty_annotation`` is True.
    """

    def test_skipped_blocks_populated_on_json_failure(self) -> None:
        """``skipped_blocks`` is incremented when a Stage 2 block's JSON
        validation exhausts retries."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={"scan__response": _memo_json()},
            default_response="not json",
        )
        anns, _ = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_json_retries=2),
        )
        assert anns[0].skipped_blocks == 1
        # Empty LLM responses don't fire here — only JSON-validation
        # failures do.
        assert anns[0].has_empty_blocks == 0

    def test_has_empty_blocks_populated_on_empty_response(self) -> None:
        """``has_empty_blocks`` is incremented when Stage 2 returns a valid
        but empty ``{"comments": []}`` response (no JSON validation
        error)."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps({"comments": []}),
            },
        )
        anns, _ = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert anns[0].comments == []
        # Empty response, no failure — the field should still fire because
        # the LLM produced nothing usable.
        assert anns[0].has_empty_blocks == 1
        assert anns[0].skipped_blocks == 0

    def test_both_counters_default_zero_on_clean_run(self) -> None:
        """A clean Stage 2 run leaves both counters at 0."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={
                "scan__response": _memo_json(),
                "annotate__response": json_dumps(
                    {"comments": [{"target_p_ids": [0], "position": "before", "kind": "note", "content": "x"}]}
                ),
            },
        )
        anns, _ = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert anns[0].skipped_blocks == 0
        assert anns[0].has_empty_blocks == 0

    def test_empty_chapter_bypasses_stage_two(self) -> None:
        """Chapters with zero ``<p>`` skip Stage 2 entirely; both
        counters should remain 0 (the chapter is counted in
        ``chapters_skipped`` via the placeholder memo instead)."""
        chapter = _mk_chapter(0)
        llm = MockLLM()
        anns, _ = process_chapters([chapter], book_metadata={}, llm=llm, config=CommentConfig())
        assert anns[0].skipped_blocks == 0
        assert anns[0].has_empty_blocks == 0
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")

    def test_counters_preserved_when_skip_chapter_on_empty(self) -> None:
        """When ``skip_chapter_on_empty_annotation=True`` taints a
        chapter, the per-chapter counters must still be populated on
        the (now-empty-comments) annotation so the review gate can show
        them."""
        chapter = _mk_chapter(2)
        llm = MockLLM(
            responses_by_seed={"scan__response": _memo_json()},
            default_response="not json",
        )
        anns, _ = process_chapters(
            [chapter],
            book_metadata={},
            llm=llm,
            config=CommentConfig(max_json_retries=2, skip_chapter_on_empty_annotation=True),
        )
        # Comments cleared because the chapter was tainted, but the
        # counter survives.
        assert anns[0].comments == []
        assert anns[0].skipped_blocks == 1
        assert anns[0].has_empty_blocks == 0
        # Placeholder memo still applies.
        assert anns[0].memo.core_thesis.startswith("(chapter skipped")
