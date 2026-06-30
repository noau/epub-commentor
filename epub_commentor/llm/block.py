"""Stage 2 — per-block annotation.

Stage 2 takes a chunk of ``block_size`` consecutive ``<p>`` elements inside
one chapter, attaches local ``data-p-id`` markers so the LLM can refer to
paragraphs by index, and asks for a JSON list of comments. After parsing
we strip the markers again — they are only meaningful inside this function.

The retry loop replays the assistant's bad response plus a terse error
message back to the model so it can self-correct, capped at
``config.max_json_retries``.
"""

import hashlib
from importlib.metadata import PackageNotFoundError, version
from xml.etree.ElementTree import Element, tostring

from pydantic import ValidationError

from ..config import CommentConfig
from ..errors import CommentInvalidJSONError, CommentOrphanPIdError, CommentOverlapError
from .protocol import LLMProtocol
from .schema import BlockAnnotation, ChapterMemo, validate_block_annotations
from .types import Message, MessageRole

try:
    _VERSION = version("epub-commentor")
except PackageNotFoundError:
    _VERSION = "0.0.0-dev"

_DATA_P_ID = "data-p-id"


def _block_hash(block_ps: list[Element], block_start_idx: int) -> str:
    head = [p.text or "" for p in block_ps]
    payload = f"{block_start_idx}:{head}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]


def _annotate_seed(
    config: CommentConfig,
    chapter_hash: str,
    block_start_idx: int,
    block_ps: list[Element],
) -> str:
    return (
        f"commentor:{_VERSION}:annotate:"
        f"{config.cache_seed_user_id}:{chapter_hash}:"
        f"{block_start_idx}:{_block_hash(block_ps, block_start_idx)}"
    )


def _format_annotate_user(
    book_synopsis: str,
    memo: ChapterMemo,
    block_index: int,
    block_html: str,
) -> str:
    memo_json = memo.model_dump_json(ensure_ascii=False, indent=2)
    return (
        f"Book synopsis:\n{book_synopsis}\n\n"
        f"Chapter memo:\n```json\n{memo_json}\n```\n\n"
        f"Block index: {block_index}\n"
        f'Block HTML (paragraphs are tagged data-p-id="0..N"):\n'
        f"```html\n{block_html}\n```"
    )


def _format_validation_error(error: Exception, raw: str) -> str:
    raw_excerpt = raw[:400] + ("…" if len(raw) > 400 else "")
    return (
        "Your previous response could not be parsed as valid annotation JSON.\n"
        f"Error: {error}\n\n"
        "Raw response (truncated):\n"
        f"```\n{raw_excerpt}\n```\n\n"
        "Please reply with ONLY the corrected JSON object."
    )


def _set_data_p_ids(block_ps: list[Element]) -> None:
    for idx, p in enumerate(block_ps):
        p.set(_DATA_P_ID, str(idx))


def _strip_data_p_ids(block_ps: list[Element]) -> None:
    for p in block_ps:
        p.attrib.pop(_DATA_P_ID, None)


def _block_html(block_ps: list[Element]) -> str:
    return "\n".join(tostring(p, encoding="unicode") for p in block_ps)


def annotate_block(
    block_ps: list[Element],
    block_start_idx: int,
    chapter_hash: str,
    memo: ChapterMemo,
    llm: LLMProtocol,
    config: CommentConfig,
) -> list:
    """Run Stage 2 for a single block of paragraphs.

    Mutates ``block_ps`` to add ``data-p-id`` and strips it before returning
    so the DOM remains clean for downstream injection. The caller is
    responsible for any outer cleanup (e.g. ID deduplication after all
    blocks for a chapter are processed).
    """
    if not block_ps:
        return []

    seed = _annotate_seed(config, chapter_hash, block_start_idx, block_ps)
    allowed_kinds_csv = ",".join(k.value for k in config.kinds)

    system_text = llm.template("annotate").render(
        target_language=config.target_language,
        default_position=config.position.value,
        allowed_kinds_csv=allowed_kinds_csv,
        block_size=len(block_ps),
    )

    _set_data_p_ids(block_ps)
    block_html = _block_html(block_ps)
    user_text = _format_annotate_user(
        book_synopsis=config.book_synopsis or "(none)",
        memo=memo,
        block_index=block_start_idx,
        block_html=block_html,
    )

    last_error: Exception | None = None

    try:
        with llm.context(cache_seed_content=seed) as ctx:
            messages: list[Message] = [
                Message(MessageRole.SYSTEM, system_text),
                Message(MessageRole.USER, user_text),
            ]
            for retry in range(config.max_json_retries):
                raw = ctx.request(messages)
                try:
                    parsed = BlockAnnotation.model_validate_json(raw)
                    return validate_block_annotations(parsed, block_size=len(block_ps))
                except (ValidationError, CommentOrphanPIdError, CommentOverlapError) as exc:
                    last_error = exc
                    if retry == config.max_json_retries - 1:
                        break
                    messages.append(Message(MessageRole.ASSISTANT, raw))
                    messages.append(Message(MessageRole.USER, _format_validation_error(exc, raw)))
    finally:
        _strip_data_p_ids(block_ps)

    assert last_error is not None  # always set when we exit the retry loop without returning
    raise CommentInvalidJSONError(
        f"Stage 2 (annotate) could not parse a valid BlockAnnotation after "
        f"{config.max_json_retries} attempts: {last_error}"
    ) from last_error


# Ensure the public schema re-exports flow through this module
__all__ = ["annotate_block"]
