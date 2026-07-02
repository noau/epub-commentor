"""Command-line entry point for ``epub-commentor``.

This module is registered as the ``epub-commentor`` console script in
``pyproject.toml``. Its responsibilities are deliberately narrow:

1. Parse command-line arguments (argparse).
2. Load the LLM from ``format.json`` (or whatever path ``--format-json``
   points to).
3. Translate flags into a :class:`CommentConfig`.
4. Hand off to :func:`~epub_commentor.commentor.comment_epub` and print
   the resulting token / chapter summary.

Anything richer (interactive REPL, JSON output mode, ...etc.) belongs in
a separate subcommand module so the main entry point stays greppable.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import fields as dataclass_fields
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .commentor import ChapterFilter, CommentorResult, comment_epub
from .config import CommentConfig
from .errors import CommentAbortError, CommentorError
from .llm import LLM
from .llm._api_key import EPUB_COMMENTOR_API_KEY_ENV_VAR, resolve_api_key
from .llm.review import review_annotations
from .llm.schema import CommentKind, CommentPosition
from .llm.select import select_chapters
from .logging_setup import setup_root_logger
from .pipeline import AnnotationFilter, ChapterAnnotation
from .pipeline.extract import Chapter
from .progress import make_default_progress_callback
from .utils import normalize_whitespace
from .xml import plain_text

# Reused from commentor.py to detect placeholder memos for the review
# picker's pre-deselect logic.
_SKIPPED_PREFIX = "(chapter skipped"

# AI decisions are written into ``commentor._AI_DECISION_SINKS`` directly
# (see ``_build_ai_chapter_filter`` / ``_build_ai_annotation_filter``);
# we no longer need a parallel scratchpad here.


def _build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser.

    Flag names mirror the :class:`CommentConfig` fields so the user can
    pass exactly the knobs the config exposes — nothing more.
    """
    parser = argparse.ArgumentParser(
        prog="epub-commentor",
        description="Add AI-generated commentary (introductions, summaries, marginalia) to an EPUB.",
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Path to the source EPUB file. The original is read but never modified.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Where to write the annotated EPUB. Default: <source-stem>.commented.epub next to the source.",
    )
    parser.add_argument(
        "--format-json",
        type=Path,
        default=None,
        help="Path to a format.json file with the LLM credentials (default: ./format.json next to the source or cwd).",
    )
    parser.add_argument(
        "--synopsis",
        type=str,
        default=None,
        help="Free-form book synopsis forwarded to Stage 1 to anchor the commentary tone.",
    )
    parser.add_argument(
        "--block-size",
        type=int,
        default=None,
        help="Paragraphs per Stage 2 block (default: 6).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Number of Stage 2 worker threads (default: 4).",
    )
    parser.add_argument(
        "--rpm-limit",
        type=int,
        default=None,
        help=(
            "Max LLM requests per 60s sliding window. Default: no limit. "
            "Use for free tiers that publish a hard per-key ceiling."
        ),
    )
    parser.add_argument(
        "--tpm-limit",
        type=int,
        default=None,
        help=(
            "Max estimated LLM tokens per 60s sliding window (default: no limit). "
            "Estimated via tiktoken with a 1.2x safety buffer; tune via "
            "'token_count_buffer' in format.json if you observe 429s."
        ),
    )
    parser.add_argument(
        "--request-concurrency",
        type=int,
        default=None,
        help=(
            "Max simultaneous in-flight LLM HTTP requests (default: no limit). "
            "Match this to the provider's server-side hard cap, e.g. 2 for "
            "GLM-4-flash-250414 free tier."
        ),
    )
    parser.add_argument(
        "--max-json-retries",
        type=int,
        default=None,
        help="Stage 2 retries on malformed JSON (default: 3).",
    )
    parser.add_argument(
        "--max-scan-retries",
        type=int,
        default=None,
        help="Stage 1 retries on malformed JSON (default: 3).",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=None,
        help="Directory for the LLM response cache (default: none).",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Directory for debug logs (creates one request <timestamp>.log per LLMContext).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging; defaults --log-dir to ./temp/logs/ if not set.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help=(
            "Minimum level for the stream logger (default: WARNING). "
            "Affects only --stream-logs / non-TTY output; rich progress "
            "is unaffected."
        ),
    )
    parser.add_argument(
        "--log-format",
        type=str,
        default="text",
        choices=["text", "json"],
        help=(
            "Stream logger record format (default: text). 'json' emits one "
            "JSON object per line for log aggregators (Loki, Datadog, ...)."
        ),
    )
    parser.add_argument(
        "--log-stream",
        type=str,
        default="stderr",
        choices=["stdout", "stderr"],
        help="Stream the stream logger writes to (default: stderr).",
    )
    parser.add_argument(
        "--stream-logs",
        action="store_true",
        help=(
            "Disable rich progress and emit each ProgressEvent as a stream "
            "log record. Auto-enabled when stderr is not a TTY. Combine "
            "with --log-level=INFO --log-format=json for batch / cloud use."
        ),
    )
    parser.add_argument(
        "--cache-user-id",
        type=str,
        default=None,
        help="Namespace for cache seeds (default: 'default'). Use a unique value per user / book.",
    )
    parser.add_argument(
        "--target-language",
        type=str,
        default=None,
        help="Language the LLM should author commentary in (default: Chinese).",
    )
    parser.add_argument(
        "--css-path",
        type=Path,
        default=None,
        help="Path inside the EPUB where commentary.css is written (default: Styles/commentary.css).",
    )
    parser.add_argument(
        "--no-css",
        action="store_true",
        help="Skip injecting commentary.css (useful when injecting only <aside> markup).",
    )
    parser.add_argument(
        "--fail-on-empty-chapter",
        action="store_true",
        help="Raise an error instead of skipping chapters with zero <p> elements.",
    )
    parser.add_argument(
        "--fail-on-block-error",
        action="store_true",
        help=(
            "Raise on Stage 1 scan / Stage 2 annotate retry exhaustion. "
            "Default is to log a warning and skip the failed block / chapter."
        ),
    )
    parser.add_argument(
        "--skip-chapter-on-empty-annotation",
        action="store_true",
        help=(
            "Mark a whole chapter as skipped (counted in chapters_skipped) "
            "if any Stage 2 block fails or returns zero comments. Use this "
            "together with --interactive on a follow-up run to retry only "
            "the tainted chapters."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help=(
            "Suppress ALL output (progress, summary, warnings). "
            "For batch logging use --stream-logs --log-level=INFO instead."
        ),
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "After extracting chapters, prompt interactively to choose which "
            "chapters to annotate. Uses a rich-selector multi-select "
            "(↑/↓ move, Space/Enter toggle, A all, I invert, C clear, "
            "Esc/Q cancel). Requires a TTY."
        ),
    )
    select_group = parser.add_mutually_exclusive_group()
    select_group.add_argument(
        "--ai-select",
        action="store_true",
        help=(
            "Use the LLM to decide which chapters deserve AI-generated "
            "commentary before Stage 1 runs. Equivalent to "
            "-i/--interactive for batch / CI scenarios where a TTY is "
            "not available. Single LLM call; decisions are cached by "
            "book. Mutually exclusive with -i/--interactive."
        ),
    )
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument(
        "--review",
        action="store_true",
        help=(
            "After Stage 2 (annotation generation), always open an "
            "interactive selector to choose which generated chapter "
            "annotations to inject. By default the selector opens only "
            "when at least one Stage 2 block was skipped or returned "
            "empty, so clean runs stay zero-friction. Requires a TTY. "
            "Unlike -i/--interactive (which selects chapters *before* "
            "LLM processing), this selects annotations *after* "
            "generation based on per-chapter stats (comments generated, "
            "blocks skipped, empty blocks)."
        ),
    )
    review_group.add_argument(
        "--no-review",
        action="store_true",
        help=("Skip the annotation review selector entirely and inject every generated annotation unconditionally."),
    )
    review_group.add_argument(
        "--ai-review",
        action="store_true",
        help=(
            "Use the LLM to decide which generated annotations are "
            "worth injecting after Stage 2. Equivalent to --review for "
            "batch / CI scenarios where a TTY is not available. Single "
            "LLM call; decisions are cached by book. Mutually exclusive "
            "with --review / --no-review."
        ),
    )
    return parser


def _resolve_format_json_path(source: Path, explicit: Path | None) -> Path:
    """Locate the format.json file.

    Lookup order:

    1. The path supplied via ``--format-json`` (if any).
    2. ``<source parent>/format.json`` — convenient when the user keeps
       credentials next to their books.
    3. ``./format.json`` relative to the current working directory.
    """
    if explicit is not None:
        return explicit.resolve()
    candidate = source.parent / "format.json"
    if candidate.exists():
        return candidate.resolve()
    return Path("format.json").resolve()


def _chapter_preview(chapter: Chapter, max_chars: int = 60) -> str:
    """Render a short plain-text preview of the chapter body.

    Walks the body element with :func:`plain_text` so that content
    inside ``<div>`` / ``<section>`` / headings is captured too — not
    only ``<p>``. Whitespace is collapsed and the result is truncated
    to ``max_chars`` characters with an ellipsis when longer.
    """
    preview = normalize_whitespace(plain_text(chapter.body)).strip()

    if len(preview) > max_chars:
        preview = preview[: max_chars - 1].rstrip() + "…"
    return preview


def _read_format_json(format_path: Path) -> dict:
    """Read and parse ``format.json``, exiting with a clear message on failure."""
    if not format_path.exists():
        print(f"format.json not found at: {format_path}", file=sys.stderr)
        print(
            f"Copy format.template.json to format.json (or set ${EPUB_COMMENTOR_API_KEY_ENV_VAR} "
            f"to provide the API key without a config file).",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        return json.loads(format_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"format.json is not valid JSON ({format_path}): {exc}", file=sys.stderr)
        sys.exit(2)


def _llm_param_names() -> set[str]:
    """Names of keyword arguments :class:`LLM` accepts (minus ``self``)."""
    return {name for name in inspect.signature(LLM.__init__).parameters if name != "self"}


def _config_field_names() -> set[str]:
    """Names of the :class:`CommentConfig` dataclass fields."""
    return {f.name for f in dataclass_fields(CommentConfig)}


def _coerce_config_value(name: str, value: object) -> object:
    """Convert a JSON scalar into the type :class:`CommentConfig` expects.

    JSON only carries strings / numbers / bools / lists, so a couple of
    fields need lifting into their richer runtime types:

    * ``css_path_in_epub`` — ``str`` → :class:`~pathlib.Path`
    * ``position`` — ``str`` → :class:`CommentPosition`
    * ``kinds`` — ``list[str]`` → ``tuple[CommentKind, ...]``

    Every other field (ints, bools, plain strings) passes through as-is.
    """
    if value is None:
        return value
    if name == "css_path_in_epub" and isinstance(value, str):
        return Path(value)
    if name == "position" and isinstance(value, str):
        return CommentPosition(value)
    if name == "kinds" and isinstance(value, (list, tuple)):
        return tuple(CommentKind(k) if isinstance(k, str) else k for k in value)
    return value


def _split_format_config(raw: dict) -> tuple[dict, dict, list[str]]:
    """Route each ``format.json`` key to the object that owns it.

    ``format.json`` may now hold three kinds of keys in one flat file:

    1. **LLM credentials / runtime** — anything :class:`LLM` accepts
       (``key``, ``url``, ``model``, ``token_encoding``, ``timeout``,
       ``cache_path``, ``log_dir_path``, ...). These go to ``LLM(**cfg)``.
    2. **Pipeline options** — anything :class:`CommentConfig` exposes
       (``concurrency``, ``block_size``, ``target_language``,
       ``position``, ``kinds``, ...). These become config defaults that
       CLI flags can still override.
    3. **Unknown** — anything else. Returned so the caller can warn
       instead of crashing (a stray/misspelled key used to raise a
       cryptic ``LLM.__init__() got an unexpected keyword argument``).

    Returns
    -------
    tuple[dict, dict, list[str]]
        ``(llm_kwargs, config_kwargs, unknown_keys)``.
    """
    llm_names = _llm_param_names()
    config_names = _config_field_names()

    llm_kwargs: dict = {}
    config_kwargs: dict = {}
    unknown: list[str] = []
    for key, value in raw.items():
        if key in llm_names:
            llm_kwargs[key] = value
        elif key in config_names:
            config_kwargs[key] = _coerce_config_value(key, value)
        else:
            unknown.append(key)
    return llm_kwargs, config_kwargs, unknown


def _construct_llm(llm_kwargs: dict, format_path: Path) -> LLM:
    """Build the production :class:`LLM` from the routed LLM kwargs."""
    # API key resolution: ``$EPUB_COMMENTOR_API_KEY`` env var takes
    # precedence over the ``key`` field in ``format.json``. The earlier
    # ``_split_format_config()`` routed whatever it found under "key"
    # into ``llm_kwargs``; we override it here before touching ``LLM(...)``,
    # which treats ``key`` as a required positional arg.
    llm_kwargs["key"] = resolve_api_key(llm_kwargs.get("key"))
    if not llm_kwargs["key"]:
        print(
            f"failed to construct LLM from {format_path}: missing API key.\n"
            f"Set the ${EPUB_COMMENTOR_API_KEY_ENV_VAR} environment variable "
            f'(recommended for safety) or fill the "key" field in {format_path}.\n'
            f"See format.template.json for the field list.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        return LLM(**llm_kwargs)
    except TypeError as exc:
        # A required field (url / model / token_encoding) is missing.
        print(f"failed to construct LLM from {format_path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _build_config(args: argparse.Namespace, base: dict | None = None) -> CommentConfig:
    """Translate parsed args into a :class:`CommentConfig`.

    ``base`` seeds the config with any pipeline options found in
    ``format.json`` (see :func:`_split_format_config`). Command-line flags
    are layered on top: each flag maps 1:1 onto a config field, and
    ``None`` means "leave whatever ``base`` / the dataclass default
    provides", so a flag only overrides a field the user actually set.
    """
    overrides: dict = dict(base or {})
    if args.synopsis is not None:
        overrides["book_synopsis"] = args.synopsis
    if args.block_size is not None:
        overrides["block_size"] = args.block_size
    if args.concurrency is not None:
        overrides["concurrency"] = args.concurrency
    if args.max_json_retries is not None:
        overrides["max_json_retries"] = args.max_json_retries
    if args.max_scan_retries is not None:
        overrides["max_scan_retries"] = args.max_scan_retries
    if args.cache_user_id is not None:
        overrides["cache_seed_user_id"] = args.cache_user_id
    if args.target_language is not None:
        overrides["target_language"] = args.target_language
    if args.css_path is not None:
        overrides["css_path_in_epub"] = args.css_path
    if args.no_css:
        overrides["inject_css"] = False
    if args.fail_on_empty_chapter:
        overrides["fail_on_empty_chapter"] = True
    if args.fail_on_block_error:
        overrides["fail_on_block_error"] = True
    if args.skip_chapter_on_empty_annotation:
        overrides["skip_chapter_on_empty_annotation"] = True

    cfg = CommentConfig(**overrides)

    # cache_path is special: LLM() takes it directly, but the config
    # dataclass doesn't model it. We don't need to store it in the
    # config; the script / caller forwards it to LLM() at construction.
    return cfg


def _build_chapter_filter(args: argparse.Namespace) -> ChapterFilter | None:
    """Build the chapter-filter callback for ``-i`` / ``--interactive``.

    Returns ``None`` when the flag is absent — ``comment_epub`` treats that
    as identity (no chapters filtered). Otherwise returns a callable that
    presents a rich-selector multi-select on stdin and yields a parallel bool
    mask aligned with the input chapters.

    Raises
    ------
    SystemExit(2)
        When the flag is set but stdin is not a TTY (CI, piped input,
        redirected stdin). Silently falling back to "all chapters" would
        surprise users who explicitly opted into the interactive flow.
    """
    if not getattr(args, "interactive", False):
        return None

    if not sys.stdin.isatty():
        print(
            "error: --interactive requires an interactive terminal (stdin is not a TTY).",
            file=sys.stderr,
        )
        sys.exit(2)

    from rich_selector import (  # local import; library stays import-clean if -i is unused
        Choice,
        Selection,
        SelectionCancelled,
    )

    def _filter(chapters: list[Chapter]) -> list[bool]:
        # Pre-deselect empty chapters so a user can move to `[ Confirm ]` and
        # press Enter to skip them all at once. The library's own _process_chapter
        # still guards against them defensively in case a callback ever drops
        # the pre-deselect.
        n_paragraphs: dict[str, int] = {ch.path.as_posix(): sum(1 for _ in ch.body.iter("p")) for ch in chapters}
        choices = [
            Choice(
                title=(
                    f"{i + 1:2d}. {ch.title[:60]}"
                    + ("" if n_paragraphs[ch.path.as_posix()] > 0 else "  (empty — no <p>)")
                ),
                description=_chapter_preview(ch),
                selected=(n_paragraphs[ch.path.as_posix()] > 0),
            )
            for i, ch in enumerate(chapters)
        ]
        try:
            # Selection.run() returns a `list[bool]` mask aligned with `choices`.
            mask = Selection("Select chapters to annotate", choices).run()
        except SelectionCancelled:  # user pressed Esc or Q
            print("aborted by user.", file=sys.stderr)
            # ``os._exit`` bypasses Python cleanup (atexit, finally, __del__)
            # so the user sees the abort instantly instead of waiting for the
            # Rich Live region + bg render thread (500ms join timeout) to
            # unwind. Source EPUB is untouched; target ZIP may be partial —
            # acceptable for a user-initiated abort.
            os._exit(130)
        except KeyboardInterrupt:  # user pressed Ctrl-C
            print("aborted by user.", file=sys.stderr)
            os._exit(130)
        # Wipe the selector off the screen before the progress bar takes over.
        _clear_console()
        return mask

    return _filter


def _build_ai_chapter_filter(args: argparse.Namespace, llm: LLM, config: CommentConfig) -> ChapterFilter | None:
    """Build the AI-driven chapter filter for ``--ai-select``.

    Returns ``None`` when the flag is absent — :func:`comment_epub`
    treats that as "process every chapter". Otherwise returns a callable
    that invokes :func:`epub_commentor.llm.select.select_chapters` once
    per book (a single LLM call covers all chapters) and yields a
    parallel ``list[bool]`` mask.

    The closure captures the decisions into
    :data:`epub_commentor.commentor._AI_DECISION_SINKS["select"]` so
    :func:`comment_epub` can snapshot them into :class:`CommentorResult`
    and surface them in the summary panel after the run. TTY is
    irrelevant — this flag is the batch / CI counterpart of
    ``-i/--interactive``.
    """
    if not getattr(args, "ai_select", False):
        return None

    from .commentor import _AI_DECISION_SINKS

    def _filter(chapters: list[Chapter], prompt_metadata: dict[str, str]) -> list[bool]:
        mask, reasons = select_chapters(chapters, prompt_metadata, llm, config)
        _AI_DECISION_SINKS["select"] = {i: (chapters[i].title, mask[i], reasons[i]) for i in range(len(chapters))}
        return mask

    return _filter


def _annotation_preview(ann: ChapterAnnotation, max_chars: int = 60) -> str:
    """Render a short preview for the review picker's description column.

    Prefers the first comment's text (since that's what the user just paid
    LLM tokens for); falls back to the first ``<p>`` in the chapter body
    for chapters with zero comments (so the picker still has something
    readable under the row title).

    Mirrors :func:`_chapter_preview`'s shape — plain-text snippet with
    collapsed whitespace and ellipsis truncation — so the picker feels
    visually consistent across the two selection gates.
    """
    if ann.comments:
        snippet = normalize_whitespace(ann.comments[0].content).strip()
        if snippet:
            return snippet[: max_chars - 1].rstrip() + "…" if len(snippet) > max_chars else snippet
    body = ann.chapter.body
    first_p = next(iter(body.iter("p")), None)
    if first_p is None:
        return ""
    snippet = normalize_whitespace(plain_text(first_p)).strip()
    if not snippet:
        return ""
    return snippet[: max_chars - 1].rstrip() + "…" if len(snippet) > max_chars else snippet


def _make_review_choice(index: int, ann: ChapterAnnotation):
    """Build one :class:`rich_selector.Choice` row for the review picker.

    Lock-off semantics
    ------------------
    Three rows states map onto the three lifecycle outcomes:

    - **Locked off** (``disabled=True``): the chapter's
      ``ChapterAnnotation.comments`` is empty AND the memo is *not* a
      placeholder. There's literally nothing to inject, so the row
      renders with a ``-`` indicator and the user cannot toggle it.
      Mirrors how :func:`_build_chapter_filter` pre-deselects empty
      chapters (cli.py:388-399) but goes one step further — the
      annotation gate makes the empty-comments chapters structurally
      untoggleable.
    - **Selectable, pre-deselected** (``selected=False``): the memo
      is the placeholder. The chapter failed Stage 1 (scan error) or
      had zero ``<p>`` elements. There's nothing meaningful to inject,
      but the user MAY want to keep the chapter in the mask anyway
      (a no-op inject is harmless — CSS link still gets wired, IDs
      still get deduplicated). The row is marked with a ``⚠`` prefix
      so the user knows why it was pre-deselected.
    - **Selectable, default selected** (``selected=True``): normal
      case — Stage 2 generated at least one comment. The title
      surfaces per-chapter stats so the user can scan-coverage at a
      glance.

    Title format: ``"<index>. <chapter title>  ·  💬 N comments · ⚠ K
    block(s) skipped · ⚠ L empty block(s)"``. Stats are appended only
    when non-zero so a fully-clean chapter shows just the title.
    """
    from rich_selector import Choice  # local import; see lazy-import note in _build_annotation_filter

    title_prefix = f"{index + 1:2d}. "
    placeholder = ann.memo.core_thesis.startswith(_SKIPPED_PREFIX)

    if len(ann.comments) == 0 and not placeholder:
        # Genuinely empty chapter — locked off.
        return Choice(
            title=f"{title_prefix}{ann.chapter.title[:50]}  🔒 no comments",
            description=_annotation_preview(ann),
            selected=False,
            disabled=True,
        )
    if placeholder:
        # Pipeline skip — pre-deselected but user can opt in for a no-op inject.
        return Choice(
            title=f"{title_prefix}{ann.chapter.title[:50]}  ⚠ scan failed / empty chapter",
            description=_annotation_preview(ann),
            selected=False,
            disabled=False,
        )
    stats_bits = [f"💬 {len(ann.comments)} comments"]
    if ann.skipped_blocks > 0:
        stats_bits.append(f"⚠ {ann.skipped_blocks} block(s) skipped")
    if ann.has_empty_blocks > 0:
        stats_bits.append(f"⚠ {ann.has_empty_blocks} empty block(s)")
    return Choice(
        title=f"{title_prefix}{ann.chapter.title[:50]}  · " + " · ".join(stats_bits),
        description=_annotation_preview(ann),
        selected=True,
        disabled=False,
    )


def _build_annotation_filter(args: argparse.Namespace) -> AnnotationFilter | None:
    """Build the annotation-filter callback for ``--review`` / ``--no-review``.

    Returns ``None`` when ``--no-review`` is set — :func:`comment_epub`
    treats that as "inject every annotation unconditionally". Otherwise
    returns a callable that opens a rich-selector multi-select on the
    post-Stage-2 annotations and yields a parallel ``list[bool]`` mask.

    Smart-trigger default
    ---------------------
    When ``--review`` is NOT set, the returned closure inspects the
    annotations and short-circuits with an all-``True`` mask when
    *nothing went wrong* — i.e. ``all(skipped_blocks == 0 and
    has_empty_blocks == 0)``. That makes the picker a no-op on clean
    runs while still surfacing whenever Stage 2 lost a block or got
    back empty responses.

    ``--review`` disables the smart trigger (always opens the picker)
    so users can manually curate even when the pipeline ran perfectly.

    TTY handling mirrors ``-i``:

    - ``--review`` + non-TTY → :func:`sys.exit` with code 2 (the user
      explicitly asked for the picker, so silently falling back would
      surprise them).
    - ``--no-review`` + non-TTY → returns ``None`` (graceful; the flag
      already says "skip the picker").
    - Default (no flags) + non-TTY → returns the closure; its smart
      trigger returns ``[True]*N`` on clean runs and would otherwise
      hit the TTY error inside ``Selection.run`` if anything went
      wrong. Callers running in a non-TTY context are expected to
      provide clean runs (or use ``--no-review``).
    """
    if getattr(args, "no_review", False):
        return None

    if getattr(args, "review", False) and not sys.stdin.isatty():
        print(
            "error: --review requires an interactive terminal (stdin is not a TTY).",
            file=sys.stderr,
        )
        sys.exit(2)

    smart_trigger = not getattr(args, "review", False)

    def _filter(annotations: list[ChapterAnnotation]) -> list[bool]:
        # Lazy import keeps the public ``epub_commentor`` cli import-clean
        # for users that never pass --review or --no-review. ``Choice`` is
        # only used by ``_make_review_choice`` (which imports it locally);
        # this closure only needs ``Selection`` and ``SelectionCancelled``.
        from rich_selector import (  # type: ignore[import-not-found]
            Selection,
            SelectionCancelled,
        )

        if smart_trigger and not any(a.skipped_blocks > 0 or a.has_empty_blocks > 0 for a in annotations):
            return [True] * len(annotations)

        choices = [_make_review_choice(i, ann) for i, ann in enumerate(annotations)]
        try:
            mask = Selection("Select chapters to inject", choices).run()
        except SelectionCancelled:
            print("aborted by user.", file=sys.stderr)
            # See _build_chapter_filter for why os._exit instead of sys.exit:
            # Rich Live's bg render thread join alone costs ~500ms.
            os._exit(130)
        except KeyboardInterrupt:
            print("aborted by user.", file=sys.stderr)
            os._exit(130)
        _clear_console()
        return mask

    return _filter


def _build_ai_annotation_filter(args: argparse.Namespace, llm: LLM, config: CommentConfig) -> AnnotationFilter | None:
    """Build the AI-driven annotation filter for ``--ai-review``.

    Returns ``None`` when the flag is absent — :func:`comment_epub`
    treats that as "inject every annotation unconditionally". Otherwise
    returns a callable that invokes
    :func:`epub_commentor.llm.review.review_annotations` once per book
    (a single LLM call covers all chapters) and yields a parallel
    ``list[bool]`` mask.

    The closure captures the decisions into
    :data:`epub_commentor.commentor._AI_DECISION_SINKS["review"]` so
    :func:`comment_epub` can snapshot them into :class:`CommentorResult`
    and surface them in the summary panel after the run. TTY is
    irrelevant — this flag is the batch / CI counterpart of ``--review``.
    """
    if not getattr(args, "ai_review", False):
        return None

    from .commentor import _AI_DECISION_SINKS

    def _filter(annotations: list[ChapterAnnotation], prompt_metadata: dict[str, str]) -> list[bool]:
        mask, reasons = review_annotations(annotations, prompt_metadata, llm, config)
        _AI_DECISION_SINKS["review"] = {
            i: (annotations[i].chapter.title, mask[i], reasons[i]) for i in range(len(annotations))
        }
        return mask

    return _filter


def _clear_console() -> None:
    """Clear the terminal so the next phase renders on a tidy screen.

    No-op when stderr is not a TTY (piped / redirected), so escape codes
    never leak into captured output.
    """
    if sys.stderr.isatty():
        Console(file=sys.stderr).clear()


def _ai_decision_panel(
    decisions: dict[int, tuple[str, bool, str]] | None,
    *,
    title: str,
) -> Panel | None:
    """Build a ``Panel`` listing each AI decision (kept / dropped + reason).

    Returns ``None`` when the filter never ran (no AI flag enabled) or when
    every verdict was a keep — there's nothing to surface in either case,
    and a successful AI run with zero drops reads as "the AI agreed with
    us", which a chapter list under the summary already conveys.

    Kept entries appear first (in spine order), then drops. Each row is
    formatted as ``"<idx>. <title>  ·  <reason>"``; ``✓ kept`` / ``✗ dropped``
    prefix mirrors the colour cue the rich-selector row uses so terminal
    and AI panels feel visually consistent.
    """
    if not decisions:
        return None
    rows_kept: list[tuple[int, str, bool, str]] = []
    rows_dropped: list[tuple[int, str, bool, str]] = []
    for idx in sorted(decisions.keys()):
        ch_title, include, reason = decisions[idx]
        bucket = rows_kept if include else rows_dropped
        bucket.append((idx, ch_title, include, reason))
    if not rows_dropped and not rows_kept:
        return None
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", style="dim")
    grid.add_column(overflow="fold")
    body_rows: list[tuple[int, str, bool, str]] = rows_kept + rows_dropped
    if not body_rows:
        return None
    for idx, ch_title, include, reason in body_rows:
        prefix = "✓ kept" if include else "✗ dropped"
        grid.add_row(f"{idx + 1}.", f"{ch_title}  ·  {prefix}  ·  {reason}")
    return Panel(grid, title=title, expand=False)


def _print_summary(result: CommentorResult, source: Path) -> None:
    """Render a rich one-screen summary at the end of a successful run."""
    console = Console()

    stats = Table.grid(padding=(0, 2))
    stats.add_column(justify="right", style="bold")
    stats.add_column(overflow="fold")
    stats.add_row("source", str(source))
    stats.add_row("output", str(result.output_path))
    stats.add_row("chapters processed", str(result.chapters_processed))
    stats.add_row("chapters skipped", str(result.chapters_skipped))
    if result.chapters_filtered > 0:
        stats.add_row("chapters filtered", str(result.chapters_filtered))
    if result.blocks_skipped > 0:
        stats.add_row("blocks skipped", str(result.blocks_skipped))
    stats.add_row("comments generated", str(result.total_comments))
    stats.add_row("input tokens", str(result.input_tokens))
    stats.add_row("input cache tokens", str(result.input_cache_tokens))
    stats.add_row("output tokens", str(result.output_tokens))
    stats.add_row("total tokens", str(result.total_tokens))

    titles = result.processed_titles
    if titles:
        chapters = Table.grid(padding=(0, 1))
        chapters.add_column(justify="right", style="dim")
        chapters.add_column(overflow="fold")
        for i, title in enumerate(titles, start=1):
            chapters.add_row(f"{i}.", title)
        chapters_body: object = chapters
    else:
        chapters_body = "[dim]none[/dim]"

    console.print()
    console.print(Panel(stats, title="EPUB Commentor — summary", expand=False))
    console.print(Panel(chapters_body, title="Chapters processed", expand=False))

    select_panel = _ai_decision_panel(result.ai_select_decisions, title="AI chapter selection — kept / dropped")
    if select_panel is not None:
        console.print(select_panel)
    review_panel = _ai_decision_panel(result.ai_review_decisions, title="AI annotation review — kept / dropped")
    if review_panel is not None:
        console.print(review_panel)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"source EPUB not found: {args.source}", file=sys.stderr)
        return 2

    format_path = _resolve_format_json_path(args.source, args.format_json)
    raw_cfg = _read_format_json(format_path)

    # A single flat format.json can now hold LLM credentials AND pipeline
    # options. Route each key to its owner; warn (don't crash) on strays.
    llm_kwargs, config_from_json, unknown_keys = _split_format_config(raw_cfg)
    if unknown_keys:
        print(
            f"warning: ignoring unrecognised key(s) in {format_path.name}: {', '.join(sorted(unknown_keys))}",
            file=sys.stderr,
        )

    # cache_path and log_dir_path steer where the LLM writes cache / debug
    # log files. CLI flags win over whatever format.json declared.
    if args.cache_path is not None:
        llm_kwargs["cache_path"] = str(args.cache_path.resolve())
    log_dir: Path | None = args.log_dir
    if args.debug and log_dir is None:
        log_dir = Path("temp/logs")
    if log_dir is not None:
        llm_kwargs["log_dir_path"] = str(log_dir.resolve())
    # Rate-limit knobs: CLI flags override format.json entries one-to-one.
    if args.rpm_limit is not None:
        llm_kwargs["rpm_limit"] = args.rpm_limit
    if args.tpm_limit is not None:
        llm_kwargs["tpm_limit"] = args.tpm_limit
    if args.request_concurrency is not None:
        llm_kwargs["request_concurrency"] = args.request_concurrency

    llm = _construct_llm(llm_kwargs, format_path)

    # Pipeline options: format.json values seed the config, CLI flags override.
    config = _build_config(args, base=config_from_json)

    # Install the stream logger handler on the project namespace before
    # any code path can emit log records. Idempotent — safe even if
    # earlier invocations already attached one (e.g. in a long-lived
    # test process).
    setup_root_logger(
        level=args.log_level,
        fmt=args.log_format,
        stream=args.log_stream,
    )

    progress_quiet = args.quiet
    progress_callback = make_default_progress_callback(
        quiet=progress_quiet,
        stream_logs=args.stream_logs,
    )

    chapter_filter = _build_chapter_filter(args) or _build_ai_chapter_filter(args, llm, config)
    annotation_filter = _build_annotation_filter(args) or _build_ai_annotation_filter(args, llm, config)

    try:
        try:
            result = comment_epub(
                source=args.source,
                output=args.output,
                llm=llm,
                config=config,
                progress_callback=progress_callback,
                chapter_filter=chapter_filter,
                annotation_filter=annotation_filter,
            )
        except CommentorError as exc:
            print(f"commentor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        except CommentAbortError:
            # The two-stage SIGINT handler (epub_commentor.llm._abort)
            # already printed "aborting..." to stderr. A second Ctrl-C
            # would hard-exit the process via os._exit(130) inside the
            # handler, so reaching this branch always means a clean
            # cooperative abort.
            print("aborted by user.", file=sys.stderr)
            return 130
    finally:
        # rich Progress context must be exited explicitly; _NoOpDisplay.close() is a no-op.
        # __self__ access works at runtime (callbacks are bound methods); pyright sees
        # the type as FunctionType, hence the ignore.
        progress_callback.__self__.close()  # type: ignore[attr-defined]

    if not args.quiet:
        _clear_console()
        _print_summary(result, args.source)

    return 0


if __name__ == "__main__":
    sys.exit(main())
