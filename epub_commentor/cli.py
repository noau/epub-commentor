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
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .commentor import ChapterFilter, CommentorResult, comment_epub
from .config import CommentConfig
from .errors import CommentorError
from .llm import LLM
from .pipeline.extract import Chapter
from .progress import make_default_progress_callback
from .utils import normalize_whitespace
from .xml import plain_text


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
        "--cache-user-id",
        type=str,
        default=None,
        help="Namespace for cache seeds (default: 'default'). Use a unique value per user / book.",
    )
    parser.add_argument(
        "--target-language",
        type=str,
        default=None,
        help="Language the LLM should author commentary in (default: English).",
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
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress the per-stage progress summary printed at the end.",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "After extracting chapters, prompt interactively to choose which "
            "chapters to annotate. Uses a terminal checkbox "
            "(space=toggle, a=all, i=invert, enter=confirm). Requires a TTY."
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


def _load_llm(format_path: Path) -> LLM:
    """Read ``format.json`` and construct the production :class:`LLM`."""
    if not format_path.exists():
        print(f"format.json not found at: {format_path}", file=sys.stderr)
        print("Copy format.template.json to format.json and fill in the API key.", file=sys.stderr)
        sys.exit(2)

    try:
        cfg = json.loads(format_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"format.json is not valid JSON ({format_path}): {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        return LLM(**cfg)
    except TypeError as exc:
        # Unknown kwarg / missing required field — surface a clear error.
        print(f"failed to construct LLM from {format_path}: {exc}", file=sys.stderr)
        sys.exit(2)


def _build_config(args: argparse.Namespace) -> CommentConfig:
    """Translate parsed args into a :class:`CommentConfig`.

    Each flag maps 1:1 onto a config field; ``None`` means "use the
    config default", so we only override fields the user actually set.
    """
    overrides: dict = {}
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

    cfg = CommentConfig(**overrides)

    # cache_path is special: LLM() takes it directly, but the config
    # dataclass doesn't model it. We don't need to store it in the
    # config; the script / caller forwards it to LLM() at construction.
    return cfg


def _build_chapter_filter(args: argparse.Namespace) -> ChapterFilter | None:
    """Build the chapter-filter callback for ``-i`` / ``--interactive``.

    Returns ``None`` when the flag is absent — ``comment_epub`` treats that
    as identity (no chapters filtered). Otherwise returns a callable that
    presents a questionary checkbox on stdin and yields a parallel bool mask.

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

    import questionary  # local import; library stays import-clean if -i is unused

    def _filter(chapters: list[Chapter]) -> list[bool]:
        # Pre-deselect empty chapters so a user can press `enter` to skip
        # them all at once. The library's own _process_chapter still guards
        # against them defensively in case a callback ever drops the pre-deselect.
        n_paragraphs: dict[str, int] = {ch.path.as_posix(): sum(1 for _ in ch.body.iter("p")) for ch in chapters}
        choices = [
            questionary.Choice(
                title=(
                    f"{i + 1:2d}. {ch.title[:60]}"
                    + ("" if n_paragraphs[ch.path.as_posix()] > 0 else "  (empty — no <p>)")
                ),
                value=i,
                description=_chapter_preview(ch),
                checked=(n_paragraphs[ch.path.as_posix()] > 0),
            )
            for i, ch in enumerate(chapters)
        ]
        answer = questionary.checkbox(
            "Select chapters to annotate",
            choices=choices,
            show_description=True,
            instruction="(space=toggle, a=all, i=invert, enter=confirm)",
        ).ask()
        if answer is None:  # user pressed Ctrl-C
            print("aborted by user.", file=sys.stderr)
            sys.exit(130)

        mask = [False] * len(chapters)
        for value in answer:
            mask[value] = True
        return mask

    return _filter


def _print_summary(result: CommentorResult, source: Path) -> None:
    """Render a one-screen summary at the end of a successful run."""
    print("")
    print("=" * 60)
    print(f"source:  {source}")
    print(f"output:  {result.output_path}")
    print(f"chapters processed: {result.chapters_processed}")
    print(f"chapters skipped:   {result.chapters_skipped}")
    print(f"comments generated: {result.total_comments}")
    print("-" * 60)
    print(f"input tokens:        {result.input_tokens}")
    print(f"input cache tokens:  {result.input_cache_tokens}")
    print(f"output tokens:       {result.output_tokens}")
    print(f"total tokens:        {result.total_tokens}")
    print("=" * 60)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"source EPUB not found: {args.source}", file=sys.stderr)
        return 2

    format_path = _resolve_format_json_path(args.source, args.format_json)
    llm = _load_llm(format_path)

    # cache_path and log_dir_path are forwarded to LLM directly (they
    # control where cache / debug log files land). They can't be set on
    # CommentConfig because the config is supposed to be LLM-agnostic.
    cfg_overrides: dict[str, object] = {}
    if args.cache_path is not None:
        cfg_overrides["cache_path"] = str(args.cache_path.resolve())
    log_dir: Path | None = args.log_dir
    if args.debug and log_dir is None:
        log_dir = Path("temp/logs")
    if log_dir is not None:
        # Note: `--debug` is purely a CLI shorthand for `log_dir_path`;
        # the actual per-request debug logging is gated by `log_dir_path`
        # inside `LLM`. We deliberately do NOT inject a `debug` key into
        # `cfg_overrides` — `LLM.__init__` accepts the field from
        # `format.json` as a no-op for backward compatibility, but here we
        # trust the on-disk value rather than overwrite it from the CLI.
        cfg_overrides["log_dir_path"] = str(log_dir.resolve())
    if cfg_overrides:
        # LLM doesn't accept post-construction rewrites of these paths;
        # we'd need to rebuild it. For simplicity we re-instantiate.
        cfg_dict = json.loads(format_path.read_text(encoding="utf-8"))
        cfg_dict.update(cfg_overrides)
        llm = LLM(**cfg_dict)

    config = _build_config(args)

    progress_quiet = args.quiet
    progress_callback = make_default_progress_callback(quiet=progress_quiet)

    chapter_filter = _build_chapter_filter(args)

    try:
        try:
            result = comment_epub(
                source=args.source,
                output=args.output,
                llm=llm,
                config=config,
                progress_callback=progress_callback,
                chapter_filter=chapter_filter,
            )
        except CommentorError as exc:
            print(f"commentor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    finally:
        # rich Progress context must be exited explicitly; _NoOpDisplay.close() is a no-op.
        # __self__ access works at runtime (callbacks are bound methods); pyright sees
        # the type as FunctionType, hence the ignore.
        progress_callback.__self__.close()  # type: ignore[attr-defined]

    if not args.quiet:
        _print_summary(result, args.source)

    return 0


if __name__ == "__main__":
    sys.exit(main())
