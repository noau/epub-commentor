"""Unit tests for Stage 3 — :mod:`epub_commentor.llm.translate`.

Mirrors the three-layer layout of ``test_commentor_block.py``:

* :class:`TestTranslateBlock` — single-block behaviour of
  :func:`translate_block` (happy / retry / exhaustion / p_id layout /
  ``data-p-id`` cleanup) using the shared :class:`MockLLM`.
* :class:`TestTranslateSchema` — pydantic-level invariants of
  :class:`ParagraphTranslation` / :class:`BlockTranslation` /
  :func:`validate_block_translations`.
* :class:`TestTranslateChapters` — orchestration: the chapter-level
  :func:`translate_chapters` integration on
  :class:`~epub_commentor.pipeline.process.ChapterAnnotation`.

The mock's ``:translate:`` seed prefix (set up in ``tests/_mock_llm.py``)
isolates Stage 3 canned responses from Stage 2's, so Stage 2 fixtures
and Stage 3 fixtures can coexist in the same test module without
collisions.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from _mock_llm import MockLLM, json_dumps

from epub_commentor.config import CommentConfig
from epub_commentor.errors import (
    CommentOrphanPIdError,
    CommentOverlapError,
    CommentTranslationFailedError,
    CommentTranslationInvalidJSONError,
)
from epub_commentor.llm.schema import (
    BlockTranslation,
    ParagraphTranslation,
    validate_block_translations,
)
from epub_commentor.llm.translate import (
    _format_translate_user,
    _translate_seed,
    translate_block,
)
from epub_commentor.pipeline.extract import Chapter
from epub_commentor.pipeline.process import (
    ChapterAnnotation,
    translate_chapters,
)
from epub_commentor.xml.xml_like import XMLLikeNode

# -------- fixtures ----------------------------------------------------------------


def _mk_chapter(n_paragraphs: int, body_xml: str | None = None) -> Chapter:
    if body_xml is None:
        body_xml = "".join(f"<p>p{i}</p>" for i in range(n_paragraphs))
    root = fromstring(f"<html><body>{body_xml}</body></html>")
    body = root.find("body")
    assert body is not None
    xml_node = XMLLikeNode(io.BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(path=Path("ch.xhtml"), title="ch", body=body, xml_node=xml_node)


def _mk_annotation(
    chapter: Chapter,
    memo_core: str = "real memo thesis",
) -> ChapterAnnotation:
    """Build a :class:`ChapterAnnotation` for the translate-orchestration
    tests. The actual ``ChapterMemo`` is constructed by pydantic.
    """
    from epub_commentor.llm.schema import ChapterMemo

    return ChapterAnnotation(
        chapter=chapter,
        memo=ChapterMemo(
            core_thesis=memo_core,
            outline=["a", "b", "c"],
            tone="t",
            target_audience="g",
        ),
    )


# -------- TestTranslateBlock --------------------------------------------------------


class TestTranslateBlock:
    """``translate_block`` mirrors ``annotate_block`` 1:1; the tests below
    mirror the Stage 2 fixtures with translate-specific assertions."""

    def test_happy_path_returns_paragraph_translations(self) -> None:
        chapter = _mk_chapter(3)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={
                "translate__response": json_dumps(
                    {
                        "translations": [
                            {"p_id": 0, "text": "T0"},
                            {"p_id": 2, "text": "T2"},
                        ]
                    }
                )
            }
        )
        out = translate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            llm=llm,
            config=CommentConfig(),
        )
        assert len(out) == 2
        assert out[0].p_id == 0 and out[0].text == "T0"
        assert out[1].p_id == 2 and out[1].text == "T2"

    def test_retry_recovers_after_invalid_response(self) -> None:
        chapter = _mk_chapter(2)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={"translate__response": json_dumps({"translations": [{"p_id": 0, "text": "ok"}]})}
        )
        real_route = llm._route
        calls = {"n": 0}

        def flaky_route(seed, messages):
            calls["n"] += 1
            if calls["n"] == 1:
                return "this is not json"
            return real_route(seed, messages)

        llm._route = flaky_route  # type: ignore[assignment]

        out = translate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            llm=llm,
            config=CommentConfig(max_translation_retries=3),
        )
        assert len(out) == 1
        assert out[0].text == "ok"
        assert calls["n"] == 2

    def test_retry_exhausted_raises_invalid_json(self) -> None:
        chapter = _mk_chapter(1)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(default_response="not json")
        with pytest.raises(CommentTranslationInvalidJSONError):
            translate_block(
                block_ps=paras,
                block_start_idx=0,
                chapter_hash="chhash",
                llm=llm,
                config=CommentConfig(max_translation_retries=2),
            )

    def test_block_local_p_ids_returned_unchanged(self) -> None:
        """``translate_block`` returns block-local ``p_id`` values; the
        block→absolute p_id shift happens in ``translate_chapters``.
        """
        chapter = _mk_chapter(6)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={"translate__response": json_dumps({"translations": [{"p_id": 5, "text": "T5"}]})}
        )
        out = translate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            llm=llm,
            config=CommentConfig(),
        )
        assert out[0].p_id == 5  # still block-local

    def test_data_p_id_is_stripped_after_call(self) -> None:
        chapter = _mk_chapter(2)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(responses_by_seed={"translate__response": json_dumps({"translations": []})})
        translate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            llm=llm,
            config=CommentConfig(),
        )
        for p in chapter.body.iter("p"):
            assert "data-p-id" not in p.attrib

    def test_empty_paragraph_text_is_accepted(self) -> None:
        """A purely structural source paragraph (no text at all) must be
        translatable to ``""`` — the prompt explicitly allows empty text so
        the 1:1 paragraph alignment doesn't drift."""
        chapter = _mk_chapter(2)
        paras = list(chapter.body.iter("p"))
        llm = MockLLM(
            responses_by_seed={
                "translate__response": json_dumps(
                    {"translations": [{"p_id": 0, "text": ""}, {"p_id": 1, "text": "T1"}]}
                )
            }
        )
        out = translate_block(
            block_ps=paras,
            block_start_idx=0,
            chapter_hash="chhash",
            llm=llm,
            config=CommentConfig(),
        )
        assert out[0].text == ""
        assert out[1].text == "T1"


# -------- TestTranslateSchema -------------------------------------------------------


class TestTranslateSchema:
    """Stand-alone schema-layer tests. Targets: pydantic constraints on
    :class:`ParagraphTranslation` (min_length=0, max_length=8000) and the
    semantic invariants enforced by :func:`validate_block_translations`.
    """

    def test_validate_accepts_block_local_p_ids(self) -> None:
        bt = BlockTranslation(
            translations=[
                ParagraphTranslation(p_id=0, text="a"),
                ParagraphTranslation(p_id=1, text="b"),
            ]
        )
        out = validate_block_translations(bt, block_size=2)
        assert len(out) == 2

    def test_validate_rejects_out_of_range_p_id(self) -> None:
        bt = BlockTranslation(translations=[ParagraphTranslation(p_id=2, text="x")])
        with pytest.raises(CommentOrphanPIdError):
            validate_block_translations(bt, block_size=2)

    def test_validate_rejects_duplicate_p_id(self) -> None:
        bt = BlockTranslation(
            translations=[
                ParagraphTranslation(p_id=0, text="a"),
                ParagraphTranslation(p_id=0, text="a-dup"),
            ]
        )
        with pytest.raises(CommentOverlapError):
            validate_block_translations(bt, block_size=2)

    def test_validate_allows_skipping_p_ids(self) -> None:
        """Unlike Stage 2's contiguity requirement, Stage 3 lets the LLM
        omit paragraphs (return them as ``""`` or skip entirely). A
        missing p_id in the middle of the block is allowed."""
        bt = BlockTranslation(
            translations=[
                ParagraphTranslation(p_id=0, text="a"),
                ParagraphTranslation(p_id=3, text="d"),
            ]
        )
        out = validate_block_translations(bt, block_size=5)
        assert [tr.p_id for tr in out] == [0, 3]

    def test_paragraph_translation_allows_empty_text(self) -> None:
        """Structural paragraphs (e.g. just an image inside a <p>) must
        be allowed to translate to ``""``."""
        tr = ParagraphTranslation(p_id=0, text="")
        assert tr.text == ""

    def test_paragraph_translation_rejects_oversized_text(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ParagraphTranslation(p_id=0, text="x" * 8001)


# -------- TestTranslateChapters -----------------------------------------------------


class TestTranslateChapters:
    """Integration-style tests for the chapter-level orchestrator.

    The Stage 2 ``process_chapters`` is bypassed: we feed
    :func:`translate_chapters` a hand-built list of
    :class:`ChapterAnnotation`. Each chapter carries a placeholder
    ``ChapterMemo`` with a non-skip ``core_thesis`` so Stage 3 actually
    runs.
    """

    def test_disabled_returns_zero_without_calling_llm(self) -> None:
        ann = _mk_annotation(_mk_chapter(3))
        llm = MockLLM()  # would raise if called
        skipped = translate_chapters([ann], llm=llm, config=CommentConfig(enable_translation=False))
        assert skipped == 0
        assert ann.translations == []
        assert llm.call_count == 0

    def test_placeholder_memo_is_skipped(self) -> None:
        """A chapter whose Stage 1 returned the placeholder memo (scan
        failure) must be skipped — no LLM call, no translations."""
        chapter = _mk_chapter(3)
        from epub_commentor.llm.schema import ChapterMemo

        ann = ChapterAnnotation(
            chapter=chapter,
            memo=ChapterMemo(
                core_thesis="(chapter skipped — no <p> elements)",
                outline=["(skipped)", "(skipped)", "(skipped)"],
                tone="(unknown)",
                target_audience="(unknown)",
            ),
        )
        llm = MockLLM()  # would raise if called
        skipped = translate_chapters([ann], llm=llm, config=CommentConfig(enable_translation=True))
        assert skipped == 0
        assert ann.translations == []
        assert llm.call_count == 0

    def test_zero_paragraph_chapter_is_skipped(self) -> None:
        chapter = _mk_chapter(0, body_xml="")
        # Body is non-empty (the outer <html><body>) but has no <p>.
        from epub_commentor.llm.schema import ChapterMemo

        ann = ChapterAnnotation(
            chapter=chapter,
            memo=ChapterMemo(
                core_thesis="zero-p chapter",
                outline=["a", "b", "c"],
                tone="t",
                target_audience="g",
            ),
        )
        llm = MockLLM()
        skipped = translate_chapters([ann], llm=llm, config=CommentConfig(enable_translation=True))
        assert skipped == 0
        assert llm.call_count == 0

    def test_translates_all_paragraphs(self) -> None:
        chapter = _mk_chapter(3)
        ann = _mk_annotation(chapter)
        llm = MockLLM(
            responses_by_seed={
                "translate__response": json_dumps(
                    {
                        "translations": [
                            {"p_id": 0, "text": "T0"},
                            {"p_id": 1, "text": "T1"},
                            {"p_id": 2, "text": "T2"},
                        ]
                    }
                )
            }
        )
        skipped = translate_chapters([ann], llm=llm, config=CommentConfig(enable_translation=True))
        assert skipped == 0
        assert [tr.p_id for tr in ann.translations] == [0, 1, 2]
        assert [tr.text for tr in ann.translations] == ["T0", "T1", "T2"]

    def test_block_local_p_ids_are_shifted_to_absolute(self) -> None:
        """A chapter with >``block_size`` paragraphs is split into multiple
        blocks; each block calls the LLM with ``p_id`` 0..block_size-1.
        :func:`translate_chapters` must add the block-start offset back so
        ``annotation.translations`` carries absolute indices matching
        ``chapter.body.iter('p')`` order.
        """
        # 6 paragraphs, block_size=2 → blocks at [0,2), [2,4), [4,6).
        chapter = _mk_chapter(6)
        ann = _mk_annotation(chapter)

        # Three responses — same canned JSON for every block; the seed
        # differs (block_start_idx differs) so the mock still routes to
        # ``translate__response`` for each call.
        llm = MockLLM(
            responses_by_seed={
                "translate__response": json_dumps(
                    {
                        "translations": [
                            {"p_id": 0, "text": "t0"},
                            {"p_id": 1, "text": "t1"},
                        ]
                    }
                )
            }
        )
        skipped = translate_chapters(
            [ann],
            llm=llm,
            config=CommentConfig(enable_translation=True, block_size=2, concurrency=2),
        )
        assert skipped == 0
        # Should have 2 entries per block × 3 blocks = 6 translations
        assert len(ann.translations) == 6
        # Sorted by absolute p_id 0..5
        assert [tr.p_id for tr in ann.translations] == [0, 1, 2, 3, 4, 5]
        # block 0 → paragraphs 0,1; block 1 → paragraphs 2,3; block 2 → paragraphs 4,5
        # They all get the same canned "t0"/"t1" text but at different absolute slots
        by_pid = {tr.p_id: tr.text for tr in ann.translations}
        assert by_pid[0] == "t0" and by_pid[1] == "t1"
        assert by_pid[2] == "t0" and by_pid[3] == "t1"
        assert by_pid[4] == "t0" and by_pid[5] == "t1"

    def test_soft_skip_failure_does_not_block_other_blocks(self) -> None:
        """With ``fail_on_translation_error=False`` (default), a Stage 3
        block whose LLM response cannot be parsed is dropped (and counted
        via ``translation_blocks_skipped``) — the other blocks still
        produce translations and are stored on the annotation."""
        chapter = _mk_chapter(4)
        ann = _mk_annotation(chapter)

        # One canned good response; everything else returns invalid JSON.
        llm = MockLLM(
            responses_by_seed={
                "translate__response": json_dumps({"translations": [{"p_id": 0, "text": "ok"}]})
            },
            default_response="not-json-at-all",
        )

        # Force ONLY the first Stage 3 call (block 0) to fail by toggling
        # a one-shot flag. ``concurrency=1`` runs blocks strictly
        # sequentially, so the first ``:translate:`` call is always
        # block 0; we still gate the seed prefix so any non-Stage 3 call
        # would not consume the slot.
        real_route = llm._route
        first_block_done = {"v": False}

        def fail_first_translate_call(seed, messages):
            if not first_block_done["v"] and (seed or "").find(":translate:") >= 0:
                first_block_done["v"] = True
                return "not-json-at-all"
            return real_route(seed, messages)

        llm._route = fail_first_translate_call  # type: ignore[assignment]

        skipped = translate_chapters(
            [ann],
            llm=llm,
            config=CommentConfig(
                enable_translation=True,
                block_size=1,
                concurrency=1,  # sequential -> block 0 always first
                max_translation_retries=1,  # exhaust fast
            ),
        )
        assert skipped == 1
        assert ann.translation_blocks_skipped == 1
        # 3 other blocks (p_id 1, 2, 3) succeeded
        assert len(ann.translations) == 3  # type: ignore[arg-type]
        assert sorted(tr.p_id for tr in ann.translations) == [1, 2, 3]  # type: ignore[union-attr,arg-type]

    def test_hard_fail_raises_translation_failed(self) -> None:
        """``fail_on_translation_error=True`` propagates the bad block as
        :class:`CommentTranslationFailedError` instead of swallowing it."""
        chapter = _mk_chapter(2)
        ann = _mk_annotation(chapter)
        llm = MockLLM(default_response="not-json")
        with pytest.raises(CommentTranslationFailedError):
            translate_chapters(
                [ann],
                llm=llm,
                config=CommentConfig(
                    enable_translation=True,
                    block_size=1,
                    concurrency=1,
                    max_translation_retries=1,
                    fail_on_translation_error=True,
                ),
            )


# -------- TestSeedFormatting -------------------------------------------------------


class TestSeedFormatting:
    """Cache-seed formatting: Stage 3 must use a distinct prefix so its
    entries never collide with Stage 2 (annotate) entries."""

    def test_seed_uses_translate_prefix(self) -> None:
        chapter = _mk_chapter(2)
        paras = list(chapter.body.iter("p"))
        seed = _translate_seed(
            config=CommentConfig(cache_seed_user_id="u1"),
            chapter_hash="ch1",
            block_start_idx=2,
            block_ps=paras,
        )
        assert ":translate:" in seed
        assert ":annotate:" not in seed
        # Includes the user id and chapter hash
        assert "u1" in seed
        assert "ch1" in seed

    def test_user_message_includes_target_language(self) -> None:
        """The user message carries the target language to the LLM so it
        has unambiguous instructions; the language comes from
        ``config.target_language``."""
        text = _format_translate_user(
            target_language="English",
            block_index=0,
            block_html='<p data-p-id="0">hi</p>',
        )
        assert "English" in text
        assert "Block index: 0" in text
        assert "data-p-id" in text
