"""Partial-success salvaging for Stage 2 ``BlockAnnotation`` objects.

When the multi-turn retry loop in
:func:`epub_commentor.llm.block.annotate_block` exhausts its attempts, a
single broken :class:`~epub_commentor.llm.schema.CommentItem` (out-of-
range p_ids, non-contiguous range, same-kind overlap) would otherwise
discard the whole block — even when the other 5 of 6 comments are
perfectly valid. This module salvages whatever is recoverable:

* **Out-of-range p_ids** — dropped. We don't know which paragraph the
  LLM meant to cover, so a guess is more likely to be wrong than right.
* **Non-contiguous p_ids** — collapsed to a single anchor by
  :func:`_fix_anchor`: ``before`` → ``min(pids)``, ``after`` → ``max(pids)``.
  This matches the natural reading of "the comment sits just before/
  after the most relevant paragraph".
* **Same-kind overlap** — the second-and-later comment is dropped.
  The first claim wins, mirroring ``validate_block_annotations``.

Salvage runs **only** after the retry loop has been exhausted, never
during. Mid-retry, the LLM should still be given a chance to fix
itself; salvage is the last-resort path that lets a "mostly good"
block contribute a few comments instead of zero.

The function never mutates ``parsed.comments`` shape; it copies each
accepted comment by reference (pydantic models are cheap to pass
around) and may rewrite ``target_p_ids`` in place on the salvaged
copies. The caller is free to read or further transform the returned
list.

Returns ``None`` when no comment is salvageable — in which case the
caller should raise the original error as before.
"""

from __future__ import annotations

from .schema import BlockAnnotation, CommentItem, CommentKind, CommentPosition


def _fix_anchor(pids: list[int], position: CommentPosition) -> list[int]:
    """Collapse a non-contiguous p_id range to a single anchor paragraph.

    ``position=before`` returns ``[min(pids)]`` (the aside sits just
    before the most-relevant paragraph); ``position=after`` returns
    ``[max(pids)]`` (just after). When the list is already length 1
    the function is a no-op identity — it's safe to call
    unconditionally on every comment as long as the contiguity check
    has already been run elsewhere.
    """
    if not pids:
        return pids
    if position == CommentPosition.BEFORE:
        return [min(pids)]
    return [max(pids)]


def salvage_block_annotations(
    parsed: BlockAnnotation,
    block_size: int,
) -> list[CommentItem] | None:
    """Per-comment salvage: keep the recoverable, drop the broken.

    Returns a list of valid :class:`CommentItem` objects (the same
    instances ``parsed.comments`` holds, possibly with
    ``target_p_ids`` rewritten in place) or ``None`` if every comment
    is unrecoverable.

    Order is preserved relative to the input — the first claim on a
    contested p_id wins, the second (and later) same-kind overlap is
    dropped. Different kinds may still share p_ids, matching the
    strict validator's policy.
    """
    valid: list[CommentItem] = []
    used: dict[CommentKind, set[int]] = {}

    for c in parsed.comments:
        if not c.target_p_ids:
            # Empty p_id list — invalid input we can't repair. Skip.
            continue
        # 1. Range check: drop out-of-range comments outright.
        if any(p < 0 or p >= block_size for p in c.target_p_ids):
            continue

        # 2. Contiguity: collapse non-contiguous ranges to an anchor.
        sorted_pids = sorted(c.target_p_ids)
        expected = list(range(sorted_pids[0], sorted_pids[-1] + 1))
        if sorted_pids != expected:
            c.target_p_ids = _fix_anchor(c.target_p_ids, c.position)
            sorted_pids = c.target_p_ids  # now length 1

        # 3. Same-kind overlap: drop the second (and later) claim.
        bucket = used.setdefault(c.kind, set())
        if any(p in bucket for p in c.target_p_ids):
            continue
        bucket.update(c.target_p_ids)

        valid.append(c)

    return valid if valid else None


__all__ = ["salvage_block_annotations"]
