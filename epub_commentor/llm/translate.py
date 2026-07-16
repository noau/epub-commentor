"""Stage 3 — per-block paragraph translation.

This module mirrors :mod:`epub_commentor.llm.block` 1:1 so the retry /
cache / abort / salvage infrastructure that Stage 2 relies on carries
over with zero plumbing changes:

- The same :class:`LLMContext` wraps one logical translation call.
- The same cache seed prefix scheme isolates translations from
  annotations (``:translate:`` vs ``:annotate:``).
- The same retry loop replays the bad response plus a corrective user
  message, capped at ``config.max_translation_retries``.
- The same ``data-p-id`` markers let the LLM refer to paragraphs by
  block-local index; we strip them again before returning.

Translation language is always :attr:`CommentConfig.target_language`
(so commentary and translation stay in lockstep); no separate language
knob lives on :class:`CommentConfig`.

The LLM is asked for a :class:`~epub_commentor.llm.schema.BlockTranslation`
(a list of :class:`~epub_commentor.llm.schema.ParagraphTranslation`
items, each with a block-local ``p_id`` and translated ``text``).
Validating + cleanup of the parsed response happens here so callers
receive a list of safe-to-inject translations with relative ``p_id``
values; the chapter-level
:func:`epub_commentor.pipeline.process.translate_chapters` shifts
those to absolute chapter indices.
"""

import hashlib
from importlib.metadata import PackageNotFoundError, version
from xml.etree.ElementTree import Element, tostring

from pydantic import ValidationError

from ..config import CommentConfig
from ..errors import (
    CommentOrphanPIdError,
    CommentOverlapError,
    CommentTranslationInvalidJSONError,
)
from .protocol import LLMProtocol
from .schema import BlockTranslation, validate_block_translations
from .types import Message, MessageRole

try:
    _VERSION = version("epub-commentor")
except PackageNotFoundError:
    _VERSION = "0.0.0-dev"

_DATA_P_ID = "data-p-id"


def _block_hash(block_ps: list[Element], block_start_idx: int) -> str:
    """Stable short hash over a block's paragraph texts.

    Identical to :func:`epub_commentor.llm.block._block_hash` — same
    source text + same offset always hashes the same. That symmetry
    means flipping the stage prefix in the seed from ``:annotate:`` to
    ``:translate:`` is the only difference between Stage 2 and
    Stage 3 cache keys.
    """
    head = [p.text or "" for p in block_ps]
    payload = f"{block_start_idx}:{head}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]


def _translate_seed(
    config: CommentConfig,
    chapter_hash: str,
    block_start_idx: int,
    block_ps: list[Element],
) -> str:
    """Build the per-call cache seed that isolates Stage 3 from Stage 2.

    Format mirrors :func:`epub_commentor.llm.block._annotate_seed`
    exactly, swapping only the stage prefix from ``:annotate:`` to
    ``:translate:``. Different stage marker ⇒ Stage 3 cache hits
    never collide with Stage 2 hits.
    """
    return (
        f"commentor:{_VERSION}:translate:"
        f"{config.cache_seed_user_id}:{chapter_hash}:"
        f"{block_start_idx}:{_block_hash(block_ps, block_start_idx)}"
    )


def _format_translate_user(
    target_language: str,
    block_index: int,
    block_html: str,
) -> str:
    """Build the user message shown to the translator."""
    return (
        f"Target language: {target_language}\n\n"
        f"Block index: {block_index}\n"
        f'Block HTML (paragraphs are tagged data-p-id="0..N"):\n'
        f"```html\n{block_html}\n```"
    )


def _format_validation_error(error: Exception, raw: str) -> str:
    """Build a corrective user message replayed to the LLM on JSON failure."""
    raw_excerpt = raw[:400] + ("…" if len(raw) > 400 else "")
    return (
        "Your previous response could not be parsed as valid translation JSON.\n"
        f"Error: {error}\n\n"
        "Raw response (truncated):\n"
        f"```\n{raw_excerpt}\n```\n\n"
        "Please reply with ONLY the corrected JSON object."
    )


def _raw_excerpt(raw: str, limit: int = 400) -> str:
    """Truncated raw response used for ``[[StageError]]`` log sections."""
    return raw[:limit] + ("…" if len(raw) > limit else "")


def _set_data_p_ids(block_ps: list[Element]) -> None:
    for idx, p in enumerate(block_ps):
        p.set(_DATA_P_ID, str(idx))


def _strip_data_p_ids(block_ps: list[Element]) -> None:
    for p in block_ps:
        p.attrib.pop(_DATA_P_ID, None)


def _block_html(block_ps: list[Element]) -> str:
    return "\n".join(tostring(p, encoding="unicode") for p in block_ps)


def translate_block(
    block_ps: list[Element],
    block_start_idx: int,
    chapter_hash: str,
    llm: LLMProtocol,
    config: CommentConfig,
) -> list:
    """Run Stage 3 for a single block of paragraphs.

    Mirrors :func:`epub_commentor.llm.block.annotate_block` 1:1 so the
    LLM-stage machinery (cache, abort polling, retry/salvage logging)
    carries over. Mutates ``block_ps`` to add ``data-p-id`` and strips
    it before returning so the DOM remains clean for downstream
    injection. The caller (:func:`translate_chapters`) shifts block-local
    ``p_id`` values to absolute chapter indices afterwards.
    """
    if not block_ps:
        return []

    seed = _translate_seed(config, chapter_hash, block_start_idx, block_ps)

    system_text = llm.template("translate").render(
        target_language=config.target_language,
        block_size=len(block_ps),
    )

    _set_data_p_ids(block_ps)
    block_html = _block_html(block_ps)
    user_text = _format_translate_user(
        target_language=config.target_language,
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
            for retry in range(config.max_translation_retries):
                raw = ctx.request(messages)
                try:
                    parsed = BlockTranslation.model_validate_json(raw)
                    return validate_block_translations(parsed, block_size=len(block_ps))
                except (ValidationError, CommentOrphanPIdError, CommentOverlapError) as exc:
                    last_error = exc
                    # Drop this invalid response from the cache: it would
                    # otherwise be committed at __exit__ and replayed on
                    # any subsequent run that re-enters this exact input.
                    ctx.discard_last()
                    if ctx.logger is not None:
                        ctx.logger.warning(
                            f"[[StageError]] stage=translate; "
                            f"attempt={retry + 1}/{config.max_translation_retries}; "
                            f"error={type(exc).__name__}: {exc}\n"
                            f"Raw excerpt:\n{_raw_excerpt(raw)}\n"
                        )
                    if retry == config.max_translation_retries - 1:
                        break
                    messages.append(Message(MessageRole.ASSISTANT, raw))
                    messages.append(Message(MessageRole.USER, _format_validation_error(exc, raw)))
    finally:
        _strip_data_p_ids(block_ps)

    assert last_error is not None  # always set when we exit the retry loop without returning
    if ctx.logger is not None:
        ctx.logger.error(
            f"[[FinalError]] stage=translate; attempts_exhausted=true; "
            f"exception={type(last_error).__name__}: {last_error}\n"
        )
    raise CommentTranslationInvalidJSONError(
        f"Stage 3 (translate) could not parse a valid BlockTranslation after "
        f"{config.max_translation_retries} attempts: {last_error}"
    ) from last_error


__all__ = ["translate_block"]
