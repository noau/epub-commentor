<div align=center>
  <h1>EPUB Commentor</h1>
  <p>
    <a href="https://github.com/your-org/epub-commentor/actions/workflows/merge-build.yml" target="_blank"><img src="https://img.shields.io/github/actions/workflow/status/your-org/epub-commentor/merge-build.yml" alt="ci" /></a>
    <a href="https://pypi.org/project/epub-commentor/" target="_blank"><img src="https://img.shields.io/badge/pip_install-epub--commentor-blue" alt="pip install epub-commentor" /></a>
    <a href="https://pypi.org/project/epub-commentor/" target="_blank"><img src="https://img.shields.io/pypi/v/epub-commentor.svg" alt="pypi epub-commentor" /></a>
    <a href="https://pypi.org/project/epub-commentor/" target="_blank"><img src="https://img.shields.io/pypi/pyversions/epub-commentor.svg" alt="python versions" /></a>
    <a href="https://github.com/your-org/epub-commentor/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/github/license/your-org/epub-commentor" alt="license" /></a>
  </p>
  <p>English | <a href="./README_zh-CN.md">中文</a></p>
</div>


Want AI-generated reading guidance without losing the original text? **EPUB Commentor** adds LLM-authored introductions, summaries, and marginalia directly into your EPUBs — the original prose stays intact, and the commentary lands as styled `<aside>` blocks an e-ink reader can render natively.

A fork of [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator) — same XML/EPUB machinery, but the LLM is retargeted from "translate paragraphs" to "annotate them." The result is an EPUB that ships as both **book and study companion**, importable straight into Kindle / Kobo / Calibre without any post-processing.

![Commentary Effect](./docs/images/commentary.png)

## Why commentary (not translation)?

Most LLM-driven epub tooling rewrites the prose. That works for bilingual reading, but it strips the original voice and makes the book opaque to anyone studying the language. Commentor instead *keeps the original* and injects three kinds of new content beside it:

- **`intro`** — A 1–3 sentence scene-setter placed *before* the first target paragraph. Anchors the reader to what's coming.
- **`summary`** — A 1–3 sentence synthesis placed *after* the last target paragraph. Closes the loop on the block.
- **`note`** — A short gloss on a specific term or concept. Optional, can sit before or after the target.

The output preserves every `<p>`, every `<em>`, every heading hierarchy of the source — the only new DOM is `<aside class="commentary commentary-{kind}">` siblings, plus a single CSS file referenced from every chapter's `<head>`.

## Installation

```bash
pip install epub-commentor
```

(or `poetry add epub-commentor` if you prefer project-scoped installs)

**Requirements**: Python 3.11, 3.12, or 3.13.

## Quick Start

### 1. Configure credentials

Copy the template and fill in your OpenAI-compatible endpoint:

```bash
cp format.template.json format.json
# edit format.json with your key, url, model, token_encoding, ...
```

`format.json` is a flat object — see [Configuration](#configuration) below.

### 2. Run the CLI

```bash
poetry run epub-commentor path/to/source.epub --synopsis "A philosophical fairy tale."
# Output: <source-stem>.commented.epub written next to the source
```

Or invoke it directly without installing the entrypoint:

```bash
poetry run python scripts/comment_epub.py path/to/source.epub --synopsis "..."
```

### 3. Or call the Python API

```python
from epub_commentor import LLM, comment_epub, CommentConfig, CommentKind, CommentPosition

llm = LLM(
    key="your-api-key",
    url="https://api.openai.com/v1",
    model="gpt-4",
    token_encoding="o200k_base",
)

config = CommentConfig(
    book_synopsis="A philosophical fairy tale about a pilot stranded in the Sahara.",
    target_language="English",
    block_size=6,                # paragraphs per Stage 2 batch
    concurrency=4,               # intra-chapter worker threads
    kinds=(CommentKind.INTRO, CommentKind.SUMMARY, CommentKind.NOTE),
    position=CommentPosition.BEFORE,
)

result = comment_epub(
    source="path/to/source.epub",
    output="path/to/annotated.epub",   # default: <stem>.commented.epub next to source
    llm=llm,
    config=config,
)

print(f"chapters processed: {result.chapters_processed}")
print(f"chapters skipped:   {result.chapters_skipped}")
print(f"comments generated: {result.total_comments}")
print(f"total tokens:       {result.total_tokens}")
```

### With progress tracking

The CLI installs a `rich` `Progress` by default — two stacked task rows share the same frame: the top row tracks chapter progress (`Ch. 3/28: Title` + `3/28` + ETA), the bottom row tracks block progress within the current chapter (`(block 12/24)` + `12/24` + ETA). Each row has its own spinner, bar, and count. `extract` and `inject` print single status lines to stderr.

For programmatic use, install the same renderer explicitly or roll your own:

```python
from epub_commentor import (
    LLM,
    comment_epub,
    CommentConfig,
    ProgressEvent,
    make_default_progress_callback,
)

llm = LLM(...)
config = CommentConfig(...)

# Default renderer: a rich Progress on stderr with two stacked task rows (use quiet=True to suppress).
progress = make_default_progress_callback(quiet=False)
result = comment_epub(source="book.epub", llm=llm, config=config, progress_callback=progress)
```

`ProgressEvent` carries everything the renderer needs:

| Field | Type | Meaning |
|---|---|---|
| `stage` | `str` | `"extract"` / `"process"` / `"inject"`. |
| `substage` | `str \| None` | Only set for `process`: `"scan"` (chapter) or `"annotate"` (block). |
| `current` / `total` | `int` | Progress within the current stage. |
| `message` | `str \| None` | Free-form description (e.g. chapter title). |

```python
# Custom renderer example: log every event instead of using the default bar.
def log_progress(event: ProgressEvent) -> None:
    label = event.substage or event.stage
    print(f"[{label}] {event.current}/{event.total}  {event.message or ''}")

comment_epub(source="book.epub", llm=llm, config=config, progress_callback=log_progress)
```

## API Reference

### `comment_epub(source, output=None, *, llm, config=None, progress_callback=None, chapter_filter=None) -> CommentorResult`

The single entry point. Runs extract → process → inject on `source` and writes a new EPUB to `output` (default: `<stem>.commented.epub` next to the source).

| Parameter | Type | Description |
|---|---|---|
| `source` | `Path \| str` | Path to the source EPUB. Read but never modified. |
| `output` | `Path \| str \| None` | Target path. `None` → `<stem>.commented.epub` next to the source. |
| `llm` | `LLMProtocol` | Any LLM satisfying the protocol (`LLM` in production, `MockLLM` in tests). |
| `config` | `CommentConfig \| None` | Pipeline knobs. `None` → defaults. |
| `progress_callback` | `Callable[[ProgressEvent], None] \| None` | Optional hook fired at stage boundaries and per chapter / per block. See [With progress tracking](#with-progress-tracking). |
| `chapter_filter` | `ChapterFilter \| None` | Optional `Callable[[list[Chapter]], list[bool]]`. Invoked between extract and process; returns a parallel bool mask (`True` = keep, `False` = drop). See [Filtering chapters](#filtering-chapters). |

Returns a `CommentorResult` with `output_path`, `annotations`, per-chapter counts, and the LLM's token totals (`total_tokens`, `input_tokens`, `input_cache_tokens`, `output_tokens`).

#### Filtering chapters

The library exposes a generic `ChapterFilter` callback so any caller (notebook, web UI, future GUI) can decide which chapters go through the LLM:

```python
from epub_commentor import Chapter, comment_epub

def only_real_chapters(chapters: list[Chapter]) -> list[bool]:
    """Skip the first spine entry (often a title page) and any empty chapter."""
    return [
        i > 0 and any(True for _ in ch.body.iter("p"))
        for i, ch in enumerate(chapters)
    ]

result = comment_epub(
    source="book.epub",
    llm=llm,
    config=config,
    chapter_filter=only_real_chapters,
)
```

Dropped chapters never reach the LLM stage — their bytes flow through the target ZIP unchanged (`Zip.__exit__` migrates them as-is from the source), so no restore step is needed. The callback receives a defensive copy of the spine-ordered chapter list.

If the returned mask is not a `list[bool]` of matching length, `comment_epub` raises `ValueError` (a programmer error, not a recoverable `CommentorError`).

The CLI ships a ready-made implementation: `-i` / `--interactive` opens a `questionary` checkbox so you can pick chapters at the terminal.

### `CommentConfig`

All runtime knobs in one dataclass:

| Field | Default | Description |
|---|---|---|
| `position` | `CommentPosition.BEFORE` | Default position when the LLM doesn't choose (`before` / `after`). |
| `kinds` | `(INTRO, SUMMARY, NOTE)` | Allowed annotation kinds the Stage 2 prompt enumerates. |
| `block_size` | `6` | Paragraphs per Stage 2 batch (the batch the LLM annotates in one call). |
| `max_scan_retries` | `3` | Stage 1 retries on malformed `ChapterMemo` JSON. |
| `max_json_retries` | `3` | Stage 2 retries on malformed `BlockAnnotation` JSON. |
| `concurrency` | `4` | Intra-chapter worker threads for Stage 2 blocks. |
| `cache_seed_user_id` | `"default"` | Cache namespace component. Change to invalidate per user / book. |
| `book_synopsis` | `None` | Free-form context forwarded to both stages. |
| `inject_css` | `True` | If `False`, skip writing `commentary.css` / patching the OPF / adding head links. |
| `css_path_in_epub` | `Path("Styles/commentary.css")` | Where the CSS lands inside the EPUB. |
| `target_language` | `"English"` | The language the LLM should author commentary in. |
| `fail_on_empty_chapter` | `False` | If `True`, raise `CommentNoParagraphsError` instead of skipping. |

### `CommentKind` / `CommentPosition`

```python
from epub_commentor import CommentKind, CommentPosition

CommentKind.INTRO       # "intro"
CommentKind.SUMMARY     # "summary"
CommentKind.NOTE        # "note"

CommentPosition.BEFORE  # "before"
CommentPosition.AFTER   # "after"
```

### CLI

The `epub-commentor` console script (registered in `pyproject.toml`) accepts:

```text
poetry run epub-commentor SOURCE [-o OUTPUT] [--format-json PATH] [--synopsis TEXT]
                              [--block-size N] [--concurrency N]
                              [--max-json-retries N] [--max-scan-retries N]
                              [--cache-path DIR] [--log-dir DIR] [--debug]
                              [--cache-user-id ID]
                              [--target-language LANG]
                              [--css-path PATH] [--no-css]
                              [--fail-on-empty-chapter] [-q] [-i]
```

All flags map 1:1 onto `CommentConfig` fields (plus `--cache-path` / `--log-dir` / `--debug` for the LLM). Run `epub-commentor --help` for the full list.

#### Interactive chapter selection (`-i` / `--interactive`)

By default every chapter in the spine goes through the LLM pipeline. To choose interactively instead, pass `-i`:

```bash
poetry run epub-commentor path/to/source.epub --synopsis "..." -i
```

After the EPUB is parsed, a checkbox list of all chapters appears. Use `space` to toggle, `a` to select all, `i` to invert, `enter` to confirm. Chapters with zero `<p>` elements (cover pages, nav documents, image-only sections) are pre-deselected so a user can press `enter` to skip them all at once.

When `-i` is set, the progress bar is automatically suppressed — questionary owns the terminal. The flag requires a TTY: piping the source through stdin exits with code `2`.

## Configuration

`format.json` is a flat object the CLI / API passes straight into `LLM(**cfg)`:

```json
{
  "key": "sk-...",
  "url": "https://api.openai.com/v1",
  "model": "gpt-4",
  "token_encoding": "o200k_base",
  "timeout": null,
  "retry_times": 5,
  "retry_interval_seconds": 6.0,
  "temperature": 0.4,
  "top_p": null,
  "cache_path": "./commentary_cache",
  "log_dir_path": null
}
```

| Provider | Example `url` | Notes |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | Use `o200k_base` for `gpt-4o`, `cl100k_base` for older models. |
| Azure OpenAI | `https://<res>.openai.azure.com/openai/deployments/<dep>` | Match the deployment name in `model`. |
| Any OpenAI-compatible API | `https://your-service.com/v1` | Match `token_encoding` to the tokenizer your model uses. |

### Single-LLM philosophy

The original `epub-translator` used two LLM instances (one creative, one structural). Commentor collapses this to a **single** `LLM`:

- Stage 1 (`scan_chapter` — full-chapter memo) and Stage 2 (`annotate_block` — per-block annotations) share one client.
- One temperature (e.g. `0.4`) balances "expressive prose" and "structured JSON."
- Token statistics remain single-instance; no aggregation needed.

## How it works

Commentor runs a **two-stage LLM pipeline** per chapter:

### Stage 1 — Scan

The full chapter body is sent as plain text along with book metadata and your synopsis. The LLM returns a `ChapterMemo`:

```jsonc
{
  "core_thesis": "One sentence capturing this chapter's main idea.",
  "outline": ["3..7 sub-topics covered, in order"],
  "key_terms": [{"term": "...", "gloss": "..."}],
  "tone": "didactic | contemplative | ...",
  "target_audience": "general readers | ...",
  "reading_anchors": ["0..3 specific passages to flag"]
}
```

### Stage 2 — Annotate

The chapter's paragraphs are sliced into `block_size`-shaped chunks. Each chunk is tagged with `data-p-id="0..N"` so the LLM can refer to them by index. Together with the memo + synopsis, the LLM produces per-block annotations:

```jsonc
{
  "comments": [
    {
      "target_p_ids": [0, 1, 2],        // contiguous subset of the block
      "position": "before" | "after",   // relative to the FIRST / LAST p_id
      "kind": "intro" | "summary" | "note",
      "content": "1-4 sentences in <target_language>"
    }
  ]
}
```

Validation enforces:

1. Every `target_p_ids` value sits in `[0, block_size)`.
2. The range is contiguous (e.g. `[3,4,5]` OK; `[2,4]` rejected).
3. Within a single block, no two comments share a paragraph.

A block that fails all `max_json_retries` attempts raises `CommentInvalidJSONError`.

### Inject

Each `CommentItem` becomes an `<aside class="commentary commentary-{kind}" id="cmt-...">` placed adjacent to its anchor paragraph. CSS is wired in three idempotent steps:

1. `commentary.css` is added to the target ZIP (via `Zip.add()`).
2. The OPF `<manifest>` gains `<item id="commentary-css" ...>` (skipped if already present).
3. Every chapter's `<head>` gains a `<link rel="stylesheet" type="text/css" href="..."/>` (skipped if already present).

The CSS targets e-ink: greyscale borders, no box-shadow, no color, `break-inside: avoid` so every `<aside>` prints as one block.

## Errors

Every error the pipeline raises derives from `CommentorError` (which inherits from `ValueError`):

| Exception | When |
|---|---|
| `CommentScanFailedError` | Stage 1 could not produce a valid `ChapterMemo` after all retries. |
| `CommentInvalidJSONError` | Stage 2 could not produce a valid `BlockAnnotation` after all retries. |
| `CommentOrphanPIdError` | A comment references p_ids outside the block, or a non-contiguous range. |
| `CommentOverlapError` | Two comments inside the same block share one or more p_ids. |
| `CommentNoParagraphsError` | A chapter has zero `<p>` elements and `fail_on_empty_chapter=True`. |

```python
from epub_commentor import comment_epub, CommentorError

try:
    result = comment_epub("book.epub", llm=llm)
except CommentorError as exc:
    # Every recoverable / structural failure lands here.
    print(f"failed: {type(exc).__name__}: {exc}")
```

## Concurrency model

- **Inter-chapter**: sequential. Stage 1 dominates per-chapter LLM traffic; no benefit to fanning out across chapters.
- **Intra-chapter**: parallel via `ThreadPoolExecutor(max_workers=concurrency)`. Stage 2 blocks within one chapter are independent.
- **Cache**: `LLMContext` commits cache writes under a global lock so multi-threaded runs don't race.

## Debug logging

Long books benefit from a paper trail when something goes wrong on chapter 17 of 28. Pass `--log-dir PATH` to the CLI (or set `log_dir_path` in `format.json`) and Commentor drops one `request YYYY-MM-DD HH-MM-SS.log` file per LLM context. Each file mixes structured sections so you can grep for what you need:

```bash
# Turn on debug logging and write to ./temp/logs.
poetry run epub-commentor tests/assets/The\ little\ prince.epub \
    --synopsis "..." --log-dir ./temp/logs --debug
```

```
08:29:12    [[Parameters]]:
              temperature=0.4
              top_p=0.9
              max_tokens=None
              cache_key=901e9296231d
08:29:12    [[Request]]:
              System: ...
              User: ...
08:29:12    [[CacheCheck]] cache_key=901e9296231d; hit=false
08:29:14    [[Response]]:
              {"comments": [...]}
08:29:15    [[StageError]] stage=annotate; attempt=1/3; error=ValidationError: ...
              Raw excerpt: {"comments": [{"target_p_ids": ...
08:29:18    [[FinalError]] stage=annotate; attempts_exhausted=true; exception=...
```

| Section | Written by | What it tells you |
|---|---|---|
| `[[Parameters]]` | `LLMExecutor` | temperature / top_p / max_tokens / cache_key for the request. |
| `[[Request]]` | `LLMExecutor` | Full system + user messages (and any assistant retries). |
| `[[Response]]` | `LLMExecutor` | The raw model output for that attempt. |
| `[[CacheCheck]] cache_key=<prefix>; hit=<bool>` | `LLMContext` | Cache short-circuit outcome. Pair with the matching `cache_key` in `[[Parameters]]`. |
| `[[StageError]] stage=<scan\|annotate>; attempt=N/M; error=...; Raw excerpt: <truncated>` | `memo.py` / `block.py` | JSON validation failed; the raw body is captured so you can see exactly what the model returned. |
| `[[FinalError]] stage=...; attempts_exhausted=true; exception=...` | `block.py` | All retries exhausted — last error before `CommentInvalidJSONError` is raised. |

> `scripts/check_duplicate_ids.py` still works against this directory — it scans for `<aside id=` patterns, which the new sections never contain.

## Testing

```bash
# Unit + integration + e2e (~197 tests against real asset files; 3 pre-existing failures unrelated to this feature)
poetry run pytest tests/ -v

# Just the commentary-specific tests
poetry run pytest tests/test_commentor_*.py -v

# The 10 hand-curated challenge cases (driven by MockLLM, no network)
poetry run python scripts/comment_challenge.py
```

`tests/_mock_llm.py` provides a `MockLLM` that routes canned JSON responses by cache-seed prefix (`:scan:` vs `:annotate:`). When constructed with `MockLLM(log_dir_path=...)`, the mock writes the same `[[Section]]` files as production `LLM`, so `tests/test_commentor_log.py` can assert on log content without an OpenAI key.

## Related Projects

- [PDF Craft](https://github.com/oomol-lab/pdf-craft) — Convert scanned / image-based PDF to EPUB first, then run it through Commentor.
- [SpineDigest](https://github.com/oomol-lab/spinedigest) — Want more than marginalia? SpineDigest builds chapter-level summaries, book topology, and a knowledge graph.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. See `plans/this-is-a-forked-encapsulated-seal.md` for the architectural intent.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details. Forked from [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator) under the same terms.

## Support

- **Issues**: [GitHub Issues](https://github.com/your-org/epub-commentor/issues)
