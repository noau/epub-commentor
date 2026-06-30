"""Regression runner for the hand-curated commentary challenge cases.

Each case under ``tests/commentary_challenge/case*.json`` describes one
chapter plus the canned Stage 1 / Stage 2 LLM responses. The runner
plumbs them through :func:`process_chapters` driven by
:class:`tests._mock_llm.MockLLM`, then checks the returned
``ChapterAnnotation.comments`` against the case's ``expected_comments``.

Negative cases (``expected_error``) are expected to raise the named
:class:`~epub_commentor.errors.CommentorError` subclass; the runner
catches and reports it.

Exit code is 0 on full pass, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import fromstring

# allow `import tests._mock_llm` when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epub_commentor.config import CommentConfig  # noqa: E402
from epub_commentor.errors import CommentorError  # noqa: E402
from epub_commentor.pipeline.extract import Chapter  # noqa: E402
from epub_commentor.pipeline.process import process_chapters  # noqa: E402
from epub_commentor.xml.xml_like import XMLLikeNode  # noqa: E402
from tests._mock_llm import MockLLM, json_dumps  # noqa: E402

CHALLENGE_DIR = Path(__file__).resolve().parents[1] / "tests" / "commentary_challenge"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    detail: str


def _build_chapter(case: dict[str, Any]) -> Chapter:
    """Materialise a Chapter from the case's chapter dict."""
    body_xml = "".join(f"<p>{p}</p>" for p in case["chapter"]["paragraphs"])
    root = fromstring(f"<html><body>{body_xml}</body></html>")
    body = root.find("body")
    assert body is not None, "test fixture must contain a body"
    xml_node = XMLLikeNode(BytesIO(b"<html></html>"), is_html_like=True)
    xml_node.element = root
    return Chapter(
        path=Path(case["chapter"]["path"]),
        title=case["chapter"]["title"],
        body=body,
        xml_node=xml_node,
    )


def _build_llm(case: dict[str, Any]) -> MockLLM:
    """Build a MockLLM that emits the case's memo + block_responses.

    ``block_responses`` is a list with one entry per block. We don't have
    a stable way to map response i to block i through the public
    ``responses_by_seed`` API (the seed varies per block), so we wrap
    ``_route`` and look at the call index to pick the right canned
    response.

    For retry cases the response is a *sequence* per block (raw string
    for attempt 1, JSON object for attempt 2+); the wrapping strategy
    is the same.
    """
    memo_json = json_dumps(case["memo"])
    block_responses = case.get("block_responses", [])
    has_retry = "block_responses_retry" in case

    if has_retry:
        # Single block, multi-attempt sequence
        sequence = case["block_responses_retry"]
        # The mock has no built-in "sequence per seed" support, so wrap.
        llm = MockLLM(responses_by_seed={"scan__response": memo_json})
        idx = {"n": 0}

        def route(seed: str | None, messages: list[Any]) -> str:
            # Stage 1 calls (seeds with :scan:) get the memo
            if seed is not None and ":scan:" in seed:
                return memo_json
            # Stage 2 calls get the next entry from the sequence
            i = min(idx["n"], len(sequence) - 1)
            idx["n"] += 1
            item = sequence[i]
            return item if isinstance(item, str) else json_dumps(item)

        llm._route = route  # type: ignore[assignment]
        return llm

    # Multi-block case: each block gets a different canned response
    if block_responses:
        # The case author aligns ``block_responses`` with the chapter's
        # paragraph count / block_size. We dispatch by reading the
        # ``Block index: N`` line that :mod:`llm.block` always includes
        # in the user message, so order of completion is irrelevant.
        n_blocks = len(block_responses)
        llm = MockLLM(responses_by_seed={"scan__response": memo_json})

        def route(seed: str | None, messages: list[Any]) -> str:
            if seed is not None and ":scan:" in seed:
                return memo_json
            # Find the user message that carries "Block index: N"
            block_idx = 0
            for msg in messages:
                text = msg.message
                marker = "Block index: "
                pos = text.find(marker)
                if pos >= 0:
                    tail = text[pos + len(marker):]
                    head = tail.split("\n", 1)[0].strip()
                    block_idx = int(head) // case["block_size"]
                    break
            i = min(block_idx, n_blocks - 1)
            return json_dumps(block_responses[i])

        llm._route = route  # type: ignore[assignment]
        return llm

    return MockLLM(responses_by_seed={"scan__response": memo_json})


def _run_case(case_path: Path) -> CaseResult:
    case = json.loads(case_path.read_text(encoding="utf-8"))
    case_id = case.get("id", case_path.stem)
    chapter = _build_chapter(case)
    config = CommentConfig(block_size=case["block_size"])
    llm = _build_llm(case)

    expected_error = case.get("expected_error")
    if expected_error:
        try:
            process_chapters([chapter], book_metadata={}, llm=llm, config=config)
        except CommentorError as exc:
            ok = type(exc).__name__ == expected_error
            return CaseResult(
                case_id=case_id,
                passed=ok,
                detail="raised expected " + expected_error if ok else f"got {type(exc).__name__}: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return CaseResult(
                case_id=case_id,
                passed=False,
                detail=f"unexpected non-CommentorError: {type(exc).__name__}: {exc}",
            )
        return CaseResult(case_id=case_id, passed=False, detail=f"expected {expected_error} to be raised, got success")

    try:
        anns = process_chapters([chapter], book_metadata={}, llm=llm, config=config)
    except CommentorError as exc:
        return CaseResult(case_id=case_id, passed=False, detail=f"unexpected error: {type(exc).__name__}: {exc}")

    actual = [(c.target_p_ids[0], c.content) for c in anns[0].comments]
    expected = [(c["target_p_ids"][0], c["content"]) for c in case["expected_comments"]]
    # Set comparison — the pipeline sorts by p_id but block concurrency
    # can yield comments in different orders; we just verify the
    # (p_id, content) pairs match.
    if set(actual) != set(expected):
        return CaseResult(
            case_id=case_id,
            passed=False,
            detail=f"contents mismatch:\n  actual:   {sorted(actual)}\n  expected: {sorted(expected)}",
        )

    # Also check the absolute p_id translation matches
    actual_pids = sorted(c.target_p_ids[0] for c in anns[0].comments)
    expected_pids = sorted(c["target_p_ids"][0] for c in case["expected_comments"])
    if actual_pids != expected_pids:
        return CaseResult(
            case_id=case_id,
            passed=False,
            detail=f"p_ids mismatch: {actual_pids} != {expected_pids}",
        )

    return CaseResult(case_id=case_id, passed=True, detail="ok")


def main() -> int:
    if not CHALLENGE_DIR.exists():
        print(f"challenge dir not found: {CHALLENGE_DIR}", file=sys.stderr)
        return 1
    cases = sorted(CHALLENGE_DIR.glob("case*.json"))
    if not cases:
        print(f"no challenge cases under {CHALLENGE_DIR}", file=sys.stderr)
        return 1
    results = [_run_case(p) for p in cases]
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    for r in results:
        marker = "PASS" if r.passed else "FAIL"
        print(f"  [{marker}] {r.case_id}: {r.detail}")
    print(f"\n{passed}/{total} challenge cases passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
