<div align=center>
  <h1>EPUB Commentor</h1>
  <p>
    <a href="https://github.com/noau/epub-commentor/actions/workflows/merge-build.yml" target="_blank"><img src="https://img.shields.io/github/actions/workflow/status/noau/epub-commentor/merge-build.yml" alt="ci" /></a>
    <a href="https://github.com/noau/epub-commentor/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/github/license/noau/epub-commentor" alt="license" /></a>
  </p>
  <p>English | <a href="./README_zh-CN.md">中文</a></p>
</div>

> This project contains LLM generated content such as code or documentation.

**EPUB Commentor** reads an EPUB and hands you back the *same book* — every word of the original untouched — with AI-written reading companions added alongside the text: a short **introduction** before a passage, a **summary** after it, and occasional **margin notes** on tricky terms. The commentary is rendered as quiet, styled side-blocks that Kindle, Kobo, and other e-ink readers display natively. Import the result and read.

**What it looks like** — your original text is untouched; the model only adds the bordered blocks around it:

<p align="center">
  <img src="./docs/imgs/example.png" alt="Example"
       style="max-width: 560px; width: 100%; height: auto;" />
</p>

<p align="center"><sub>Example generated from 老舍《茶馆》.</sub></p>

## What you get

- **Your original book, intact.** No paragraph is rewritten, translated, or reordered. Commentor only *adds* content next to the text.
- **Three kinds of companion notes**, all authored by the model:
  - **Intro** — a 1–3 sentence lead-in placed *before* a passage, so you know what's coming.
  - **Summary** — a 1–3 sentence wrap-up placed *after* a passage, tying it together.
  - **Note** — a brief gloss on a specific term or idea.
- **Optional paragraph translation** — with `--enable-translation`, each original paragraph is followed by a same-language rendering so the commentary and the text share a single reading language. Off by default; the original text is never modified.
- **Commentary in any language you choose** — read an English book with Chinese notes, or vice-versa.
- **E-ink friendly styling** — greyscale, no color or shadow, and notes never split across a page break.
- **A ready-to-read `.epub`** written next to your source file. Nothing to convert afterward — drag it onto your reader.

---

## Table of contents

- [What you get](#what-you-get)
- [Table of contents](#table-of-contents)
- [Installation](#installation)
- [Get an API key ready](#get-an-api-key-ready)
- [Configure `format.json`](#configure-formatjson)
  - [Provider examples](#provider-examples)
  - [Also valid in `format.json`: pipeline options](#also-valid-in-formatjson-pipeline-options)
- [Run it](#run-it)
  - [A realistic first run](#a-realistic-first-run)
- [Choosing chapters interactively](#choosing-chapters-interactively)
- [Tuning the commentary](#tuning-the-commentary)
- [Rate limiting for free LLM tiers](#rate-limiting-for-free-llm-tiers)
- [Batch & cloud processing](#batch--cloud-processing)
  - [Recommended `format.json` for batch jobs](#recommended-formatjson-for-batch-jobs)
  - [CLI presets by scenario](#cli-presets-by-scenario)
  - [Output handling](#output-handling)
- [Long-running daemon (`epubctl`)](#long-running-daemon-epubctl)
- [Forcing JSON output](#forcing-json-output)
- [Command reference](#command-reference)
- [Reading the result on your device](#reading-the-result-on-your-device)
- [Saving money with the cache](#saving-money-with-the-cache)
- [When something goes wrong](#when-something-goes-wrong)
  - [Common issues](#common-issues)
- [Using it from Python](#using-it-from-python)
  - [Watching progress](#watching-progress)
  - [Picking chapters programmatically](#picking-chapters-programmatically)
  - [`CommentConfig` options](#commentconfig-options)
- [FAQ](#faq)
- [License](#license)
- [Support](#support)

---

## Installation

You need **Python 3.13+** and [Poetry](https://python-poetry.org/) (Python's dependency manager).

```bash
git clone https://github.com/noau/epub-commentor.git
cd epub-commentor
poetry install
```

That's it — `poetry install` pulls every dependency into an isolated environment. From here on, prefix commands with `poetry run` so they use that environment.

> **Don't have Poetry?** Install it once with `pipx install poetry` (or follow the [official guide](https://python-poetry.org/docs/#installation)).

---

## Get an API key ready

Commentor talks to any **OpenAI-compatible** chat API — that includes OpenAI itself, Azure OpenAI, and most self-hosted or third-party gateways (DeepSeek, Together, Groq, local Ollama with an OpenAI shim, etc.). You need three things from your provider:

1. An **API key** (a secret string, usually starting `sk-...`).
2. The **base URL** of the API (the part ending in `/v1`).
3. The **model name** you want to use.

Keep these handy for the next step.

---

## Configure `format.json`

Commentor reads your credentials from a file called `format.json`. Create it once by copying the template:

```bash
cp format.template.json format.json
```

Then open `format.json` and fill it in. Here's a complete example with **every field explained** — you only *need* to change `key`, `url`, `model`, and `token_encoding`:

```json
{
  "key": "sk-your-secret-api-key",
  "url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "token_encoding": "o200k_base",
  "timeout": 360.0,
  "retry_times": 5,
  "retry_interval_seconds": 6.0,
  "temperature": 0.4,
  "top_p": 0.9,
  "json_mode": false,
  "cache_path": "./commentary_cache",
  "log_dir_path": null
}
```

| Field | Required? | What to put | Notes |
|---|---|---|---|
| `key` | Optional* | Your API key. | *See [Where does the API key come from?](#where-does-the-api-key-come-from) below. Keep this secret — don't commit `format.json` to a public repo. |
| `url` | **Yes** | The API base URL, ending in `/v1`. | See the provider table below. |
| `model` | **Yes** | The model name. | e.g. `gpt-4o`, `deepseek-chat`, or your Azure deployment name. |
| `token_encoding` | **Yes** | The tokenizer name your model uses. | Used only to count tokens for the progress display. Use `o200k_base` for GPT-4o / GPT-4.1 / o-series, `cl100k_base` for older GPT-4 / GPT-3.5. When unsure, `o200k_base` is a safe default. |
| `timeout` | No | Seconds to wait for one response before giving up. | `360.0` is generous for long chapters. Set `null` for no limit. |
| `retry_times` | No | How many times to retry a failed network call. | Default `5`. |
| `retry_interval_seconds` | No | Seconds to wait between retries. | Default `6.0`. |
| `temperature` | No | Creativity of the writing, `0.0`–`1.0`. | `0.4` keeps notes expressive but on-topic. Higher = more varied, lower = more literal. |
| `top_p` | No | Alternative to temperature (nucleus sampling). | Leave as `0.9`, or set `null` to ignore. |
| `json_mode` | No | Force every chat-completion call to request `response_format={"type": "json_object"}`. | `false` (default) = unconstrained. `true` = force valid-JSON output. See [Forcing JSON output](#forcing-json-output). |
| `cache_path` | No | Folder to store responses so re-runs are free. | See [Saving money with the cache](#saving-money-with-the-cache). Omit or `null` to disable. |
| `log_dir_path` | No | Folder for detailed debug logs. | `null` = off. See [When something goes wrong](#when-something-goes-wrong). |
| `rpm_limit` | No | Max LLM requests per 60-second sliding window. | `null` = no limit. See [Rate limiting for free LLM tiers](#rate-limiting-for-free-llm-tiers). |
| `tpm_limit` | No | Max estimated LLM tokens per 60-second sliding window. | `null` = no limit. Token count is estimated via `token_encoding` with a safety buffer. |
| `request_concurrency` | No | Max simultaneous in-flight LLM HTTP requests. | `null` = no limit. Set this to the provider's server-side hard cap (e.g. `2` for GLM-4-flash-250414 free tier). |
| `token_count_buffer` | No | Safety multiplier on top of tiktoken's estimate (default `1.2`). | Raise this if you observe `429` even with `tpm_limit` set. |

### Provider examples

| Provider | `url` | `model` (example) | `token_encoding` |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` | `o200k_base` |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/deployments/<deployment>` | *your deployment name* | match your model |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | `cl100k_base` |
| Any OpenAI-compatible service | `https://your-service.com/v1` | *provider-specific* | match your model's tokenizer |

> **You can keep `format.json` next to your books.** Commentor looks for it in three places, in order: the path you pass with `--format-json`, then next to the source EPUB, then in the current folder.

### Where does the API key come from?

The loader checks two places, in this order:

1. **`$EPUB_COMMENTOR_API_KEY`** environment variable. If set, it **wins** and `format.json`'s `key` field is ignored entirely.
2. **`format.json`**'s `key` field. Empty / missing / `<PLACEHOLDER>` values are treated as missing.

Prefer the env var for safety. Twelve-factor style keeps secrets out of checked-in files — set the env var in your shell profile, in CI's secret store, or via `direnv`, and **omit the `"key"` line from `format.json` entirely**:

```bash
export EPUB_COMMENTOR_API_KEY="sk-your-secret-api-key"
```

A safe-to-commit `format.json` (no secret anywhere):

```json
{
  "url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "token_encoding": "o200k_base"
}
```

Both places may be set at the same time (env var wins); this lets you check `format.json` into a public repo with **no `key` field at all** while keeping your actual key in a shell env that's never committed.

If you forget to `export` the env var AND `format.json` has no `key` field, Commentor exits with a clear message:

```
failed to construct LLM from format.json: missing API key.
Set the $EPUB_COMMENTOR_API_KEY environment variable (recommended for safety)
or fill the "key" field in format.json.
```

### Also valid in `format.json`: pipeline options

`format.json` isn't only for credentials. You can drop any **pipeline option** into the same flat file to make it a persistent default — handy when you don't want to retype the same flag on every run:

```json
{
  "url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "token_encoding": "o200k_base",

  "concurrency": 8,
  "block_size": 8,
  "target_language": "English",
  "book_synopsis": "A philosophical fairy tale about a stranded pilot."
}
```

Any field from the [`CommentConfig` table](#commentconfig-options) works here — `concurrency`, `block_size`, `target_language`, `book_synopsis`, `position`, `kinds`, `max_json_retries`, and so on. Two rules:

- **Use the config field names, and command-line flags win.** A value in `format.json` is only a *default*; passing the matching flag (e.g. `--concurrency 4`) overrides it for that run. Note the file uses the config field name (`book_synopsis`, `cache_seed_user_id`), not the flag spelling (`--synopsis`, `--cache-user-id`).
- **Unknown keys are ignored** with a warning on stderr — a typo won't crash the run.

---

## Run it

The basic command takes one EPUB and (optionally) a one-line synopsis to set the tone:

```bash
poetry run epub-commentor "path/to/book.epub" --synopsis "A philosophical fairy tale about a stranded pilot."
```

When it finishes you'll see a summary panel, and a new file named **`book.commented.epub`** appears next to your original. That's the file to read.

Want it somewhere specific? Use `-o`:

```bash
poetry run epub-commentor "book.epub" -o "~/Kindle/book-annotated.epub" --synopsis "..."
```

The **`--synopsis`** flag is optional but recommended — a single sentence about the book helps the model pitch its notes at the right level. If you skip it, Commentor still works using the book's own metadata.

### A realistic first run

```bash
poetry run epub-commentor "The little prince.epub" \
    --synopsis "A poetic tale about a pilot who meets a small prince from another planet." \
    --target-language "English"
```

While it runs you'll see a live progress display: the top line tracks chapters (`Ch. 3/28: ...`), the bottom line tracks the smaller batches within the current chapter. Long books take a while and cost real API tokens — the final summary shows exactly how many tokens were used.

---

## Choosing chapters interactively

Most EPUBs contain more than chapters: cover pages, tables of contents, copyright notices. To pick exactly what gets annotated, add **`-i`** (interactive):

```bash
poetry run epub-commentor "book.epub" --synopsis "..." -i
```

A checklist of every chapter appears. Controls:

| Key | Action |
|---|---|
| `↑` / `↓` | Move up / down |
| `Space` or `Enter` | Toggle the highlighted chapter |
| `A` | Select all |
| `I` | Invert selection |
| `C` | Clear all |
| move to `[ Confirm ]` + `Enter` | Start annotating the selected chapters |
| `Esc` / `Q` | Cancel and exit |

Chapters with no real text (covers, navigation, image-only pages) are **pre-unchecked** for you — so you can often just press `Enter` on `[ Confirm ]` to skip all the junk at once. Everything you leave unchecked is copied into the output untouched.

> `-i` needs a real terminal. If you pipe input or run it in a script, it exits with an error instead of guessing.

---

## Tuning the commentary

You control the notes through a handful of flags. All are optional.

- **`--target-language "Chinese"`** — the language the notes are written in. The book itself is never translated; only the added commentary uses this language. Default: Chinese.
- **`--synopsis "..."`** — a one-line description of the book to steer the tone.
- **`--block-size 6`** — how many paragraphs the model looks at per batch. Smaller = more granular, finer notes but more API calls (higher cost); larger = broader notes, cheaper. Default: `6`.
- **`--concurrency 4`** — how many batches within a chapter are processed at once. Higher finishes faster but hits your API's rate limits harder. Default: `4`.
- **`--enable-translation`** — (optional) after Stage 2, translate every paragraph into the language specified by `--target-language`. The original is unchanged; each translated paragraph is inserted as `<p class="translation">` right after the source. Same concurrency / cache / rate-limit knobs apply.
- **`--no-css`** — inject only the note blocks, without the built-in styling (advanced; use if you'll supply your own stylesheet).

---

## Rate limiting for free LLM tiers

When you point Commentor at a free LLM tier (e.g. Zhipu / GLM, or any provider with a hard per-key ceiling), the model will throw `429 Too Many Requests` if you out-pace it. Three rate-limit knobs live **inside the `LLM` class**, so every layer — CLI, scripts, Python callers — gets throttled for free:

| Knob | Default | What it controls |
|---|---|---|
| `rpm_limit` | `null` (unlimited) | Max LLM requests per rolling 60-second window. |
| `tpm_limit` | `null` (unlimited) | Max estimated tokens per rolling 60-second window. Tokens are counted via `tiktoken` + a `1.2x` safety buffer, so non-OpenAI tokenizers (GLM etc.) stay safely under the cap. |
| `request_concurrency` | `null` (unlimited) | Max simultaneous in-flight HTTP calls. This is the *server-side* ceiling — e.g. `2` for `glm-4-flash-250414` on the free tier. |

Set them on the CLI:

```bash
poetry run epub-commentor path/to/source.epub \
  --synopsis "..." \
  --rpm-limit 60 --tpm-limit 200000 --request-concurrency 2
```

…or persistently in `format.json`:

```json
{
  "key": "...",
  "url": "https://open.bigmodel.cn/api/paas/v4/",
  "model": "glm-4-flash-250414",
  "token_encoding": "o200k_base",
  "rpm_limit": 60,
  "tpm_limit": 200000,
  "request_concurrency": 2,
  "token_count_buffer": 1.2
}
```

Notes:

- **`--concurrency` is different.** It controls the *worker thread pool* (how many Stage 2 batches process in parallel). `request_concurrency` controls the *number of HTTP requests that actually leave the process*. Set both: e.g. `--concurrency 8 --request-concurrency 2` means 8 workers contend for 2 outbound slots.
- **Retries count.** Every retry attempt passes through the rate limiter, so a flapping connection won't punch through your `rpm_limit`.
- **429 is still retried.** If a 429 ever leaks through (e.g. another process sharing the same API key), the executor backs off and retries — `is_retry_error` now recognises `openai.RateLimitError`.
- **`token_count_buffer` is your safety valve.** Raise it (e.g. `1.5`) if you still see `429` after setting `tpm_limit`; it over-estimates the per-request cost before charging the TPM window.

---

## Batch & cloud processing

When you run Commentor on a server, in a CI job, or as part of a batch pipeline, the **interactive** defaults break down: rich progress bars draw escape codes into log files, and `--quiet` silences *everything* including soft-skip warnings. This section covers the dedicated flags and configs designed for that environment.

The two display modes are now independent of the three logging knobs:

| Concern | Flags | When to change |
|---|---|---|
| **Display** (what shows on stderr) | `--quiet`, `--stream-logs`, TTY auto-detect | Pick rich bar / stream logs / silence |
| **Logging config** (level / format / stream) | `--log-level`, `--log-format`, `--log-stream` | Tame noise or feed a log aggregator |

A TTY user can pass `--log-level=INFO --log-format=json` and still get the rich bar. A cloud user piping `2> build.log` gets the stream logger automatically — no extra flag needed.

### Recommended `format.json` for batch jobs

This is a complete, ready-to-fill config for unattended runs on a server:

```json
{
  "url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "token_encoding": "o200k_base",

  "timeout": 600.0,
  "retry_times": 5,
  "retry_interval_seconds": 10.0,
  "temperature": 0.4,
  "top_p": 0.9,

  "cache_path": "./commentary_cache",
  "log_dir_path": "./run-logs",

  "concurrency": 4,
  "block_size": 6,
  "target_language": "English",
  "book_synopsis": "A philosophical fairy tale about a stranded pilot.",

  "rpm_limit": 60,
  "tpm_limit": 200000,
  "request_concurrency": 4,
  "token_count_buffer": 1.2
}
```

Notes on the choices:

- **`timeout: 600.0`** — long chapters can take minutes; a tight timeout aborts in the middle of Stage 2.
- **`cache_path`** — makes every retry / re-run free on cached chapters.
- **`log_dir_path`** — keeps per-request debug logs for post-mortem (`./run-logs/`). Pair with `[[StageError]]` / `[[FinalError]]` segments.
- **`concurrency` vs `request_concurrency`** — workers in flight inside the process vs HTTP requests that actually leave the box. Set both when the upstream has a hard cap.
- **No `key` field** — provide the API key via `$EPUB_COMMENTOR_API_KEY` (12-factor, safe to commit).

### CLI presets by scenario

The defaults below assume the `format.json` above is in place; the CLI flags override those defaults per-run.

**Local terminal (rich bar, default behavior):**
```bash
poetry run epub-commentor book.epub
# nothing extra; auto-detects TTY
```

**Cloud server / CI / piped output (structured logs, no escape codes):**
```bash
poetry run epub-commentor book.epub 2> build.log
# auto-detects non-TTY → uses _StreamLogDisplay
# default level is WARNING → soft-skip warnings still visible
```

**Cloud server with full event trace (per-chapter progress, machine-parseable):**
```bash
poetry run epub-commentor book.epub \
  --log-level=INFO \
  --log-format=json \
  --stream-logs \
  2> build.jsonl
# Each ProgressEvent becomes one JSON object per line:
#   {"ts":"2026-07-02T14:23:01.123Z","level":"INFO",
#    "logger":"epub_commentor.progress","message":"Chapter Three",
#    "stage":"process","substage":"scan","current":3,"total":12}
# Pipe into jq: cat build.jsonl | jq 'select(.stage=="warn")'
```

**Cron job / discarded output (zero noise):**
```bash
poetry run epub-commentor book.epub --quiet >/dev/null 2>&1
# -q means truly silent — no progress, no summary, no warnings.
# For batch logging use --stream-logs --log-level=INFO instead.
```

**Cloud retry (audit-friendly, debug logs on disk):**
```bash
poetry run epub-commentor book.epub \
  --log-dir ./run-logs \
  --log-level=INFO \
  --log-format=json \
  --stream-logs \
  --log-stream=stdout \
  > combined.jsonl 2> stderr.log
# stdout  → one JSON record per ProgressEvent
# stderr  → unrelated diagnostics (rare)
# ./run-logs/ → per-request LLM trace for post-mortem
```

### Output handling

- **Default level is `WARNING`** — soft-skip warnings ("chapter has zero `<p>`", "block retries exhausted") still surface. Pass `--log-level=INFO` for per-chapter visibility, or `--log-level=ERROR` to suppress even warns.
- **Default stream is `stderr`** — matches 12-factor convention; stdout stays free for future structured result output.
- **JSON extras are flat top-level fields** — `stage`, `substage`, `current`, `total` are siblings of `message` and `level`, so `jq` selectors work without unwrapping. See [the JsonFormatter section](#json-output-mode) for the schema.
- **Non-TTY auto-detect is opt-out-able** — pass `--stream-logs` to force the stream logger even on a TTY (useful when piping through `tee` and you want clean log lines on the file even with the bar on screen).
- **`--quiet` truly silences everything** — even ERROR-level records. For ERROR-only visibility on a server, set `--log-level=ERROR` (don't use `--quiet`).

---

## Long-running daemon (`epubctl`)

> **Setting up a fresh server?** See the [Deployment Guide](./docs/deployment.md)
> first — a copy-pasteable walkthrough from a clean Ubuntu box to a
> daemon running under systemd, ending with the first book finished and
> a filesystem snapshot.

When you're annotating a stack of books on a server, the one-shot CLI
forces you to babysit each run: SSH can drop, you can't peek at progress
from another terminal, and disk pressure from LLM caches + logs can take
the whole box down. `epub-commentor` ships with a small **local daemon**
(`epubctl` + `python -m epub_commentor.daemon`) that gives you a SQLite-
backed queue, a single in-process worker, and a CLI for inspecting
everything — no HTTP, no auth, no extra processes.

> **Full documentation lives in [`docs/daemon.md`](./docs/daemon.md)** —
> it covers the architecture, every `epubctl` subcommand, the job state
> machine, crash recovery, the disk circuit breaker, systemd + Docker
> deployment, and troubleshooting. This section is just the
> orientation.

A typical workflow looks like:

```bash
# Terminal 1: start the daemon (blocks the foreground)
mkdir -p ~/epub-daemon
export EPUB_COMMENTOR_API_KEY=sk-...
poetry run python -m epub_commentor.daemon --workspace ~/epub-daemon

# Terminal 2: submit books, watch progress, tail logs
poetry run epubctl submit ~/books/little-prince.epub \
    --flags '{"ai_select": true, "no_review": true}' --priority 5
poetry run epubctl status --watch
poetry run epubctl log 1 --follow
```

When a job lands in `SUCCESS`, the EPUB is at
`~/epub-daemon/jobs/job_<id>/output.commented.epub` — drag it onto your
reader.

Read [the full daemon guide](./docs/daemon.md) for setup, configuration
(`format.daemon.json`), every `epubctl` subcommand, the per-job workspace
layout, and how to deploy under `systemd` or in a container.

---

## Forcing JSON output

Every LLM call Commentor makes (the chapter overview and each block's annotation) expects a **valid JSON object** back from the model. The prompts ask for it in text, and pydantic + a multi-turn retry loop clean up whatever slips through — but you can short-circuit that whole detour by turning on the API-level JSON mode.

OpenAI, DeepSeek, and most other OpenAI-compatible providers honour a parameter that tells the model to only emit a JSON object: `response_format={"type": "json_object"}`. Commentor wraps that into a single boolean:

```json
{
  "key": "sk-your-secret-api-key",
  "url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "token_encoding": "cl100k_base",
  "json_mode": true
}
```

Notes:

- **Default is `false`** — when `json_mode` is `false` (or absent), Commentor never sends `response_format` to the SDK and your existing behaviour is preserved.
- **One knob, every call.** Both the chapter-overview call and the per-block annotation call pick it up; there's nothing to wire per stage.
- **Provider support is not universal.** A provider that rejects an unknown field will surface the error through the normal retry loop. Keep it `false` on providers that don't recognise it.
- **Streaming still works.** Commentor reads the response in chunks regardless of JSON mode, so nothing changes about the live progress display.
- **Debug-friendly.** When `log_dir_path` is set, the `[[Parameters]]` section of every request log now records the active `json_mode` value alongside `temperature`, `top_p`, etc.

---

## Command reference

Run `poetry run epub-commentor --help` any time for the authoritative list. Every flag below is optional except `source`.

| Flag | Meaning |
|---|---|
| `source` | Path to the EPUB to annotate. **Required.** Read-only — your original is never modified. |
| `-o`, `--output PATH` | Where to write the result. Default: `<name>.commented.epub` next to the source. |
| `--format-json PATH` | Where to read credentials from. Default: `format.json` next to the source, then the current folder. |
| `--synopsis TEXT` | One-line book description to steer tone. |
| `--target-language LANG` | Language for commentary (Stage 2) and, when `--enable-translation` is on, paragraph translation (Stage 3). Default: `Chinese`. |
| `--enable-translation` | Opt into Stage 3 paragraph translation. See [Tuning the commentary](#tuning-the-commentary). |
| `--block-size N` | Paragraphs per batch. Default: `6`. |
| `--concurrency N` | Batches processed at once within a chapter. Default: `4`. |
| `--max-json-retries N` | Retries when the model returns malformed notes for a batch. Default: `3`. |
| `--max-scan-retries N` | Retries when the model's chapter overview comes back malformed. Default: `3`. |
| `--cache-path DIR` | Folder to cache responses (makes re-runs free). |
| `--css-path PATH` | Where the stylesheet lives inside the EPUB. Default: `Styles/commentary.css`. |
| `--no-css` | Skip adding the stylesheet. |
| `--fail-on-empty-chapter` | Stop with an error on a chapter that has no paragraphs, instead of skipping it. |
| `--log-dir DIR` | Write detailed debug logs to this folder. |
| `--debug` | Turn on debug logging (defaults the log folder to `./temp/logs/`). |
| `--log-level LVL` | Minimum level for the stream logger. One of `DEBUG/INFO/WARNING/ERROR/CRITICAL`. Default `WARNING`. See [Batch & cloud processing](#batch--cloud-processing). |
| `--log-format FMT` | Stream logger format: `text` (default) or `json`. |
| `--log-stream STR` | Stream the logger writes to: `stdout` or `stderr` (default). |
| `--stream-logs` | Force the stream-logger display (auto-enabled when stderr is not a TTY). |
| `--cache-user-id ID` | Namespace for the cache. Change it to force fresh results for a new book/user. |
| `-i`, `--interactive` | Pick chapters from a checklist before running. |
| `-q`, `--quiet` | Suppress **all** output (truly silent, including warnings). See [Batch & cloud processing](#batch--cloud-processing). |
| `--rpm-limit N` | Max LLM requests per 60s window. See [Rate limiting](#rate-limiting-for-free-llm-tiers). |
| `--tpm-limit N` | Max estimated LLM tokens per 60s window. |
| `--request-concurrency N` | Max simultaneous in-flight LLM HTTP calls. |

---

## Reading the result on your device

The output is a standard `.epub`. To read it:

- **Kindle** — Email the file to your [Send to Kindle](https://www.amazon.com/sendtokindle) address, or drag it into the Send to Kindle desktop app. (Modern Kindles accept EPUB directly.)
- **Kobo / PocketBook / other e-ink** — Copy the `.epub` onto the device over USB, or add it through the device's library app.
- **Calibre** — Just add the file to your library; the commentary styling comes along.
- **Apple Books / Google Play Books** — Import the file directly.

The notes appear as bordered side-blocks in the flow of the text, styled to stay readable on greyscale screens.

---

## Saving money with the cache

LLM calls cost money, and a long book is many calls. If you set a **cache folder**, Commentor remembers every response — so if you re-run the same book (say, after tweaking one chapter, or after a crash), the parts it already did come back instantly and for free.

```bash
poetry run epub-commentor "book.epub" --synopsis "..." --cache-path ./commentary_cache
```

Or set `"cache_path": "./commentary_cache"` in `format.json` so it's always on.

The cache is keyed by the book content and your settings, so changing the synopsis, language, or model correctly produces fresh notes. To deliberately start over for the same book, delete the cache folder or pass a new `--cache-user-id`.

---

## When something goes wrong

If a run fails or the notes look off, turn on **debug logging** to see exactly what the model was asked and what it replied:

```bash
poetry run epub-commentor "book.epub" --synopsis "..." --debug
# logs land in ./temp/logs/ — one file per request
```

Each log file records the full request, the raw response, and — if a batch had to be retried — the error and the model's exact malformed output. This is the first thing to check when a specific chapter produces strange or missing notes.

### Common issues

| Symptom | Likely cause / fix |
|---|---|
| `format.json not found` | You didn't copy the template. Run `cp format.template.json format.json` and fill it in (or set `$EPUB_COMMENTOR_API_KEY` to provide the API key without a config file — recommended for safety). |
| `format.json is not valid JSON` | A typo — usually a trailing comma or missing quote. Validate it in any JSON linter. |
| Authentication / 401 errors | Wrong `key` (in `$EPUB_COMMENTOR_API_KEY` env var or in `format.json`'s `key` field) or wrong `url`. Double-check both with your provider. |
| Timeouts on long chapters | Raise `timeout` in `format.json` (e.g. `600.0`), or lower `--block-size`. |
| Rate-limit errors | Lower `--concurrency` (try `2` or `1`). |
| A chapter got no notes | It may have no real paragraphs (a cover or nav page) — that's normal and it's skipped. Use `-i` to see what's what. |
| `--interactive requires a TTY` | You ran `-i` in a pipe or script. Drop `-i`, or run it in a normal terminal. |
| `--quiet` swallowed my warnings | That's by design — `-q` is truly silent. For warnings on a server, use `--stream-logs --log-level=WARNING` (the default). |

---

## Using it from Python

Prefer to script it? The same functionality is a single function call.

```python
from epub_commentor import LLM, comment_epub, CommentConfig

llm = LLM(
    key="sk-your-api-key",
    url="https://api.openai.com/v1",
    model="gpt-4o",
    token_encoding="o200k_base",
)

config = CommentConfig(
    book_synopsis="A philosophical fairy tale about a stranded pilot.",
    target_language="English",   # commentary language; the book is never translated
    block_size=6,                # paragraphs per batch
    concurrency=4,               # batches processed at once within a chapter
)

result = comment_epub(
    source="book.epub",
    output="book-annotated.epub",  # optional; defaults to <name>.commented.epub
    llm=llm,
    config=config,
)

print(f"chapters annotated:     {result.chapters_processed}")
print(f"comments generated:     {result.total_comments}")
print(f"tokens used:            {result.total_tokens}")
if result.paragraphs_translated:
    print(f"paragraphs translated:  {result.paragraphs_translated}")
    print(f"chapters translated:    {result.chapters_translated}")
```

### Watching progress

Pass a `progress_callback` to get live updates. The easiest option is the same renderer the CLI uses:

```python
from epub_commentor import comment_epub, make_default_progress_callback

progress = make_default_progress_callback(quiet=False)  # quiet=True to silence it
comment_epub(source="book.epub", llm=llm, config=config, progress_callback=progress)
```

Or write your own — the callback receives a `ProgressEvent` with `stage`, `current`, `total`, and a `message`:

```python
def on_progress(event):
    print(f"[{event.stage}] {event.current}/{event.total}  {event.message or ''}")

comment_epub(source="book.epub", llm=llm, config=config, progress_callback=on_progress)
```

### Picking chapters programmatically

Provide a `chapter_filter` that returns one `True`/`False` per chapter (in reading order):

```python
from epub_commentor import comment_epub, Chapter

def only_real_chapters(chapters: list[Chapter]) -> list[bool]:
    # keep chapters that actually contain paragraphs
    return [any(True for _ in ch.body.iter("p")) for ch in chapters]

comment_epub(source="book.epub", llm=llm, config=config, chapter_filter=only_real_chapters)
```

### `CommentConfig` options

| Option | Default | What it does |
|---|---|---|
| `book_synopsis` | `None` | One-line description to steer tone. |
| `target_language` | `"Chinese"` | Language of the commentary. |
| `block_size` | `6` | Paragraphs per batch. |
| `concurrency` | `4` | Batches processed at once within a chapter. |
| `kinds` | all three | Which note types to allow (`INTRO`, `SUMMARY`, `NOTE`). |
| `position` | `BEFORE` | Default placement when the model doesn't specify. |
| `max_scan_retries` | `3` | Retries on a malformed chapter overview. |
| `max_json_retries` | `3` | Retries on malformed batch notes. |
| `inject_css` | `True` | Whether to add the built-in stylesheet. |
| `css_path_in_epub` | `Styles/commentary.css` | Where the stylesheet lands inside the EPUB. |
| `fail_on_empty_chapter` | `False` | Error (instead of skip) on a chapter with no paragraphs. |
| `enable_translation` | `False` | Opt into optional Stage 3 paragraph translation. |
| `max_translation_retries` | `3` | Retries on malformed translation JSON. |
| `fail_on_translation_error` | `False` | Error (instead of soft-skip) when a translation block fails. |
| `cache_seed_user_id` | `"default"` | Cache namespace; change to force fresh results. |

`comment_epub` raises a `CommentorError` if a chapter can't be annotated after all retries — catch it if you want to handle failures gracefully:

```python
from epub_commentor import comment_epub, CommentorError

try:
    comment_epub("book.epub", llm=llm)
except CommentorError as exc:
    print(f"failed: {exc}")
```

---

## FAQ

**Does it change or translate my book?**
No — not by default. With `--enable-translation`, each paragraph gets a translated copy placed *after* the original (both the original and the translation appear on the page). Without that flag (the default), the original text is preserved exactly and Commentor only *adds* note blocks beside it. `--target-language` controls the language of all added content — commentary and, when enabled, translation.

**Can I read the result on a Kindle?**
Yes — see [Reading the result on your device](#reading-the-result-on-your-device). The output is a plain EPUB that modern Kindles and every other reader accept.

**How much does a book cost to annotate?**
It depends on the book's length and your model's pricing. The summary at the end of each run reports the exact token count. Use `--cache-path` so re-runs don't pay twice, and `-i` to skip chapters you don't need.

**Do I need an OpenAI account specifically?**
No — any OpenAI-compatible API works (OpenAI, Azure, DeepSeek, local gateways, …). Just point `url` and `model` at your provider.

**Why did a chapter get skipped?**
It had no readable paragraphs — typically a cover, table of contents, or image-only page. That's intentional. Pass `--fail-on-empty-chapter` if you'd rather be told loudly.

## License

MIT — see [LICENSE](LICENSE). Forked from [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator) under the same terms.

## Support

Questions or bugs? Open a [GitHub Issue](https://github.com/noau/epub-commentor/issues).
