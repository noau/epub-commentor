# EPUB Commentor — Cloud Daemon

A long-running queue + worker for `epub-commentor`, designed for cloud servers
and batch pipelines where SSH drops, opaque progress, and disk pressure are
real problems.

The daemon keeps the existing single-file CLI intact and adds an **optional**
mode on top of it: enqueue many EPUBs once, walk away, and inspect everything
from a local CLI client. No HTTP, no auth, no extra processes — just an SQLite
file, a workspace directory, and one Python thread.

> Looking for the single-file CLI instead? See the [top-level README](../README.md).

---

## Table of contents

- [What problem this solves](#what-problem-this-solves)
- [Architecture at a glance](#architecture-at-a-glance)
- [Per-job workspace](#per-job-workspace)
- [Quick start](#quick-start)
- [Configuration files](#configuration-files)
  - [`format.daemon.json` (daemon settings)](#formatdaemonjson-daemon-settings)
  - [`format.json` (LLM credentials)](#formatjson-llm-credentials)
- [Running the daemon](#running-the-daemon)
- [`epubctl` — the local CLI client](#epubctl--the-local-cli-client)
  - [Submitting jobs](#submitting-jobs)
  - [Inspecting the queue](#inspecting-the-queue)
  - [Live watch](#live-watch)
  - [Reading logs](#reading-logs)
  - [Lifecycle events](#lifecycle-events)
  - [Priority, pause, cancel](#priority-pause-cancel)
  - [Health, recover, prune](#health-recover-prune)
- [Per-job flags (`--flags-json`)](#per-job-flags---flags-json)
- [Job lifecycle & state machine](#job-lifecycle--state-machine)
- [Crash recovery](#crash-recovery)
- [Disk circuit breaker](#disk-circuit-breaker)
- [Robustness — what happens when...](#robustness--what-happens-when)
- [Deploying the daemon](#deploying-the-daemon)
  - [systemd unit (Linux)](#systemd-unit-linux)
  - [Docker / containers](#docker--containers)
  - [Single-instance guarantee](#single-instance-guarantee)
  - [Graceful shutdown](#graceful-shutdown)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

---

## What problem this solves

Annotating a long EPUB against an LLM takes **hours**, and on a cloud server
the plain `epub-commentor` CLI runs into four real pain points:

1. **SSH disconnects kill the run.** Close your terminal mid-job and the
   whole thing is wasted.
2. **Status is invisible.** You can't tell from another terminal which chapter
   is running, how many tokens have been spent, or which LLM call is in flight.
3. **No multi-book queue.** Want to annotate five books back-to-back? You have
   to babysit the queue manually.
4. **Disk pressure kills the server.** Per-book LLM caches plus debug logs
   reach hundreds of MB; cloud disks of 20–50 GB blow up after three or four
   books with `OSError: [Errno 28]`.

The daemon addresses all four:

- **Survives disconnects** — runs as a background process, not a child of your
  shell. Wrap it in `systemd` or `docker` and let it restart on its own.
- **Visible status from any terminal** — `epubctl status` / `watch` / `show`.
- **Built-in queue with priority + retry** — submit as many books as you want;
  the single worker pulls them in `priority DESC, created_at` order.
- **Defensive disk monitoring** — when free space crosses a threshold, every
  non-terminal job auto-pauses and resumes when you free space.

It does **not** add an HTTP API, auth, notifications, or per-job parallel
workers — those are intentionally out of scope (see [FAQ](#faq) for why).

---

## Architecture at a glance

```
┌────────────────────┐
│  epubctl submit    │ ── INSERT ──┐
│  epubctl status    │ ◀─ SELECT ──┤
│  epubctl cancel    │ ── signal ──┤
│  epubctl log       │ ── read FS ─┤
└────────────────────┘             ▼
                        ┌──────────────────────┐
                        │  daemon.sqlite (WAL) │
                        │   - jobs             │
                        │   - events           │
                        │   - control_signals  │
                        │   - server_stats     │
                        └──────────────────────┘
                                   ▲
                                   │ SELECT/UPDATE
                                   │
       ┌───────────────────────────┴─────────────────────────────┐
       │  python -m epub_commentor.daemon                         │
       │   worker_loop (1 thread, blocking):                      │
       │     while not shutdown:                                  │
       │       if disk_low(): pause + sleep                       │
       │       if cancel-signal for current job: request_abort()  │
       │       job = fetch_next_pending()                         │
       │       if job: run_job(job)   # ← reuses comment_epub()   │
       │       else: sleep 5                                       │
       └──────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                            comment_epub(
                                source     = <ws>/input.epub,
                                output     = <ws>/output.commented.epub,
                                llm        = LLM(cache_path=<ws>/cache,
                                                  log_dir_path=<ws>/logs),
                                progress_cb= quiet,
                            )
```

The worker calls `comment_epub()` **in-process** (no `subprocess`) for two
reasons:

1. The CLI internally calls `sys.exit(2)` on errors; shelling out would kill
   the worker mid-job.
2. The rate limiter, cache, and abort flags are all in-process globals — a
   separate process would need IPC to share them.

Per-job isolation is therefore enforced through the existing CLI args:
`cache_path`, `log_dir_path`, and `output` all point into the job's own
subdirectory.

---

## Per-job workspace

Each job gets its own self-contained directory:

```
<workspace_dir>/
├── daemon.sqlite              # the queue database (WAL mode)
├── daemon.lock                # single-instance guard (fcntl/PID)
├── format.daemon.json         # daemon config (optional)
├── format.json                # LLM credentials (shared with the CLI)
└── jobs/
    └── job_<id>/
        ├── input.epub              # copy of what you submitted
        ├── output.commented.epub   # SUCCESS — what you ship to your reader
        ├── cache/                  # LLM cache (deleted on SUCCESS)
        ├── logs/                   # LLM debug logs (tar.gz'd on SUCCESS)
        ├── commentor.log           # daemon's own stderr mirror
        └── meta.json               # CommentorResult snapshot (SUCCESS only)
```

Key invariants:

- **`input.epub` is copied, not symlinked** — your original file is never
  touched. You can delete or replace the source the moment the job is queued.
- **`cache/` and `logs/` are scoped to the job** — two books running
  back-to-back cannot pollute each other's caches.
- On `SUCCESS` the cache is deleted and the logs are archived into
  `logs/archive.tar.gz` to keep the workspace bounded across many jobs.
- On `FAILED` the cache is also deleted — a poisoned cache entry from a
  validation failure shouldn't haunt the next retry.
- `output.commented.epub` and `meta.json` are kept indefinitely so you can
  ship the EPUB and audit what the model produced.

---

## Quick start

You need `epub-commentor` already installed (`poetry install`); the daemon
ships in the same package.

```bash
# 1. Pick a workspace directory. Everything lives under here.
mkdir -p ~/epub-daemon

# 2. (Optional) Drop a config. The defaults work for most setups.
cat > ~/epub-daemon/format.daemon.json <<'EOF'
{
  "workspace_dir": "/home/you/epub-daemon",
  "disk": { "min_free_gb": 2.0, "min_free_percent": 10.0 },
  "max_retries": 3,
  "log_level": "INFO"
}
EOF

# 3. Provide the API key (the daemon reuses the same resolve_api_key).
export EPUB_COMMENTOR_API_KEY=sk-...

# 4. Launch the daemon. Blocks the foreground; run it under systemd or
#    `nohup`, or open a dedicated terminal for it.
poetry run python -m epub_commentor.daemon --workspace ~/epub-daemon

# 5. From another terminal, submit books and watch.
poetry run epubctl submit ~/books/little-prince.epub \
    --flags '{"ai_select": true, "no_review": true}' \
    --synopsis "A poetic fairy tale about a pilot and a small prince." \
    --priority 5

poetry run epubctl status --watch    # live refresh, Ctrl-C to exit
poetry run epubctl log 1 --follow    # tail logs for job 1
```

When `status` shows `SUCCESS`, the EPUB is at
`~/epub-daemon/jobs/job_1/output.commented.epub` — drag it onto your reader.

---

## Configuration files

The daemon reads **two** flat JSON files, both already familiar from the CLI:

| File | Purpose | Daemon-specific? |
|---|---|---|
| `format.json` | LLM credentials + per-run defaults (`url`, `model`, `key`, `concurrency`, …) | No — same as the CLI |
| `format.daemon.json` | Workspace path, disk thresholds, log level, poll cadence | Yes |

### `format.daemon.json` (daemon settings)

Drop the template into your workspace and edit it:

```bash
cp format.daemon.template.json ~/epub-daemon/format.daemon.json
```

```json
{
  "workspace_dir": "./daemon_workspace",
  "sqlite_path": null,
  "log_level": "INFO",
  "log_format": "text",
  "max_retries": 3,
  "disk": {
    "min_free_gb": 2.0,
    "min_free_percent": 10.0
  },
  "shutdown_grace_seconds": 30,
  "poll_interval_idle_seconds": 5.0,
  "poll_interval_paused_seconds": 60.0,
  "notification_command": null
}
```

| Field | Default | What it does |
|---|---|---|
| `workspace_dir` | `./daemon_workspace` | Root holding `daemon.sqlite` and `jobs/`. **CLI `--workspace` overrides this.** |
| `sqlite_path` | `<workspace_dir>/daemon.sqlite` | Override only if you want the DB on a different disk. |
| `log_level` | `INFO` | Python root logger level. Use `DEBUG` when chasing a hairy retry. |
| `log_format` | `text` | `text` or `json`. JSON pairs well with `journalctl -o json`. |
| `max_retries` | `3` | How many times to re-queue a `FAILED` job automatically before leaving it alone. |
| `disk.min_free_gb` | `2.0` | Pause the queue when free space drops below this many GB. |
| `disk.min_free_percent` | `10.0` | Pause the queue when used percent climbs above `100 - this`. |
| `shutdown_grace_seconds` | `30` | Reserved for future use; current loop drains the in-flight job. |
| `poll_interval_idle_seconds` | `5.0` | How often the worker checks the queue when nothing's pending. |
| `poll_interval_paused_seconds` | `60.0` | How often the worker rechecks disk while paused. |
| `notification_command` | `null` | Optional shell hook (see below). |

**Lookup order** for the config file (highest priority first):

1. `--config <path>` CLI flag
2. `$EPUBCTL_DAEMON_CONFIG` env var
3. `<cwd>/format.daemon.json`

**Unknown keys are logged at WARNING** but never crash startup — a typo is
loud but non-fatal.

#### Optional `notification_command`

`notification_command` is an escape hatch: when set, the daemon runs it via
`subprocess` after these events, passing the event kind and a one-line summary
on argv. Default is `null` (no notifications). Example:

```json
{
  "notification_command": "/home/you/bin/notify.sh {kind} {summary}"
}
```

The hook is a single shell invocation. You write the wrapper; the daemon only
fills in `{kind}` (`started` / `finished` / `failed` / `cancelled` /
`disk_low` / `disk_recovered`) and `{summary}`.

### `format.json` (LLM credentials)

Identical to the single-file CLI's `format.json`. The daemon loads it once on
startup and reuses the LLM kwargs for every job; per-job overrides come from
`--flags-json` (see below). All CLI fields are honoured: `url`, `model`,
`token_encoding`, `timeout`, `temperature`, `top_p`, `cache_path`,
`log_dir_path`, `rpm_limit`, `tpm_limit`, `request_concurrency`, and so on.

The API key resolution order is the same: `$EPUB_COMMENTOR_API_KEY` env var
wins over `format.json`'s `key` field. The daemon does **not** require a key
to start — only when it picks up its first job does it look for one; if
neither source is set, the job lands in `FAILED` with `error_stage=api_key`.

---

## Running the daemon

```bash
poetry run python -m epub_commentor.daemon --workspace ~/epub-daemon
```

| Flag | Default | What it does |
|---|---|---|
| `--workspace PATH` | *required* | Root directory. Holds `daemon.sqlite` and `jobs/`. Created if missing. |
| `--config PATH` | auto-discovered | Override `format.daemon.json` location. |
| `--once` | off | Run a single poll cycle and exit. Useful for smoke tests. |
| `--max-seconds N` | `0` (forever) | Exit after N seconds. Smoke-test cap. |

On startup the daemon:

1. Resolves the config and the SQLite path.
2. Opens the database (WAL mode), creates the schema if absent.
3. Runs `recover_crashed_jobs` — see [Crash recovery](#crash-recovery).
4. Acquires the single-instance lock (`<workspace>/daemon.lock`).
5. Wires SIGINT / SIGTERM to a graceful shutdown.
6. Loads `format.json` once for the base LLM kwargs.
7. Enters the worker loop.

To stop it: send SIGTERM (`kill <pid>`) or Ctrl-C in its terminal. The
in-flight job cooperatively aborts, the row lands in `CANCELLED`, and the
daemon exits.

---

## `epubctl` — the local CLI client

`epubctl` is the local tool you use to manage a running daemon. It talks to
the SQLite database directly — no network involved.

```
poetry run epubctl --db ~/epub-daemon/daemon.sqlite <subcommand> [args]
```

If you omit `--db`, the path is resolved in this order:

1. `--db <path>` argument
2. `$EPUBCTL_DAEMON_DB` environment variable
3. `./daemon.sqlite` (cwd)

### Submitting jobs

```bash
poetry run epubctl submit ~/books/little-prince.epub
```

`submit` copies the file into `jobs/job_<N>/input.epub` and inserts a
`PENDING` row. Arguments:

| Flag | What it does |
|---|---|
| `file` (positional) | Path to the source `.epub`. Must exist. |
| `--priority N` | Integer; higher runs first. Default `0`. |
| `--synopsis "..."` | One-line book description (forwarded to Stage 1). |
| `--max-retries N` | Per-job retry budget. Default `3` (matches `format.daemon.json`). |
| `--flags-json '{...}'` | Per-job `CommentConfig` + `LLM` overrides — see [Per-job flags](#per-job-flags---flags-json). |
| `--ai-select` | Convenience: adds `"ai_select": true` to flags. |
| `--no-review` | Convenience: adds `"no_review": true` to flags. |
| `--ai-review` | Convenience: adds `"ai_review": true` to flags. |

Examples:

```bash
# Priority book, AI pre-selects chapters, no human review
poetry run epubctl submit ~/books/a.epub \
    --priority 10 --ai-select --no-review

# Same book, full control via JSON
poetry run epubctl submit ~/books/a.epub --flags '{
  "ai_select": true,
  "no_review": true,
  "concurrency": 2,
  "block_size": 4,
  "target_language": "English"
}'

# Multiple books queued back-to-back
for f in ~/books/*.epub; do
    poetry run epubctl submit "$f" --priority 1
done
```

### Inspecting the queue

```bash
poetry run epubctl status          # all jobs, newest first
poetry run epubctl status --status PROCESSING
poetry run epubctl show 3          # full JSON for job 3
poetry run epubctl show 3 --meta   # also dump meta.json
```

`status` prints a fixed-width table:

```
id    status      pri  file                     tokens       comments  age
3     SUCCESS     0    catcher-in-the-rye.epub  12345/8765   42        2h
2     PROCESSING  5    little-prince.epub       4321/2100    12        18m
1     PAUSED      0    pride-and-prejudice.epub 0/0          0         4m
--------------------------------------------------------------------
depths: PENDING=0, PROCESSING=1, SUCCESS=1, PAUSED=1, FAILED=0, CANCELLED=0
```

`show <id>` prints the full row as JSON, including flags, tokens, error
information, and timestamps. Add `--meta` to also dump `meta.json` (the
`CommentorResult` snapshot) so you can audit what the model produced.

### Live watch

```bash
poetry run epubctl watch --interval 2
```

Clears the screen and re-renders the `status` table every N seconds. Ctrl-C
exits.

### Reading logs

```bash
poetry run epubctl log 3 --tail 200       # last 200 lines
poetry run epubctl log 3 --follow         # like `tail -f`
poetry run epubctl log 3 --follow --interval 1
```

Logs come from `<workspace>/jobs/job_<id>/logs/*.log` — the same files the
`comment_epub()` pipeline wrote. The first time a job runs, you'll see one
file per LLM request, each containing the prompt, the raw response, and any
`[[StageError]]` / `[[FinalError]]` markers.

### Lifecycle events

```bash
poetry run epubctl events 3 --limit 50
```

Prints the audit trail for one job:

```
2026-07-03 10:00:01  enqueued
2026-07-03 10:00:03  started
2026-07-03 10:42:11  finished — /home/you/daemon/jobs/job_3/output.commented.epub
```

`failed`, `cancelled`, `paused`, `resumed`, `restarted`, `disk_low`,
`disk_recovered` show up here too. Use this when post-morteming a job that
behaved unexpectedly.

### Priority, pause, cancel

```bash
poetry run epubctl priority 3 10         # bump job 3 to priority 10
poetry run epubctl cancel  3             # cooperative cancel (next chapter)
poetry run epubctl retry   3             # FAILED → PENDING (retry_count++)
poetry run epubctl resume  3             # PAUSED → PENDING
poetry run epubctl pause-all --reason "operator away"
poetry run epubctl resume-all
```

`cancel` doesn't kill the worker — it sets a control flag in SQLite that the
worker reads between chapter iterations. The current chapter finishes, the
job transitions to `CANCELLED`, and the next job starts. This avoids
leaving the cache / logs in an inconsistent state.

`retry` on a `FAILED` job bumps `retry_count`; if the budget is exhausted the
command fails loudly.

### Health, recover, prune

```bash
poetry run epubctl health     # queue depths + last server_stats row
poetry run epubctl recover    # manually re-run crash recovery
poetry run epubctl prune      # delete old SUCCESS/FAILED/CANCELLED jobs
poetry run epubctl prune --success --force   # no confirmation prompt
poetry run epubctl prune --failed --cancelled
```

`prune` deletes both the database row (and its events, via FK cascade) and
the `jobs/job_<id>/` workspace. Confirm each deletion unless `--force` is
passed. By default it leaves SUCCESS jobs alone (you probably still want
those EPUBs); pass `--keep-success=false` (the default behaviour) or use the
status flags to control exactly what gets pruned.

---

## Per-job flags (`--flags-json`)

Every key in `format.json` plus every `CommentConfig` field can be overridden
per-job. Pass a JSON object:

```bash
poetry run epubctl submit ~/books/long.epub --flags '{
  "ai_select": true,
  "no_review": true,
  "concurrency": 2,
  "block_size": 8,
  "target_language": "English",
  "book_synopsis": "A philosophical fairy tale.",
  "cache_seed_user_id": "long-book-v1",
  "fail_on_empty_chapter": false
}'
```

**Unsupported flags fail loudly**, not silently:

- `--interactive` (`-i`) — needs a TTY, the daemon has none.
- `--review` (interactive) — same reason.

These are rejected at submit time with `error_stage=flag` so you find out
before wasting an LLM call.

Unknown keys are ignored with a warning, matching the CLI's behaviour for
`format.json` typos.

---

## Job lifecycle & state machine

```
              ┌────── daemon_restart ────┐
              │                         │
              ▼                         │
         PENDING ──worker picks──► PROCESSING ──success──► SUCCESS ──► (cleanup cache/)
            ▲                          │
            │                          ├── error ─► FAILED ──retryable──► PENDING (retry_count++)
            │                          │            └─non-retry──► FAILED
            │                          ├── abort ──────► CANCELLED
            │                          └── disk_low ───► PAUSED
            │                                              ▲
            └── resume (manual or auto) ──────────────────┘
```

Six states:

| State | Meaning |
|---|---|
| `PENDING` | Queued, waiting for the worker to pick it up. |
| `PROCESSING` | The worker is running `comment_epub()`. |
| `SUCCESS` | Output EPUB written. Cache deleted, logs archived. |
| `FAILED` | Hit an exception. May be retried up to `max_retries`. |
| `PAUSED` | Held by the disk circuit breaker or an operator. Resumed manually or on disk recovery. |
| `CANCELLED` | Operator-initiated cancel. Terminal — no auto-retry. |

Three are **terminal** (`SUCCESS`, `FAILED`, `CANCELLED`); the others cycle.

---

## Crash recovery

If the daemon process dies mid-job (OOM kill, machine reboot, systemd
restart), the row is left in `PROCESSING`. On the next startup the daemon
runs `recover_crashed_jobs`, which does two passes:

1. **Stale rows** — `PROCESSING` jobs whose `started_at` is more than 1 hour
   old are escalated to `FAILED` with `error_stage=timeout`. Truly orphaned
   jobs don't loop forever.
2. **Fresh rows** — `PROCESSING` jobs started less than 1 hour ago are reset
   to `PENDING` with `error_stage=daemon_restart` and a `restarted` event
   for audit. The next worker picks them up.

The per-job workspace already contains `cache/` and `logs/`, so the retry
starts from a clean state for the worker's own files (validation failures
have already evicted any poisoned cache entries via `LLMContext.discard_last`).

If the daemon itself is unhealthy, you can also trigger recovery manually
without restarting:

```bash
poetry run epubctl recover
```

---

## Disk circuit breaker

The worker checks `shutil.disk_usage(<workspace_dir>)` on every iteration
(once every few seconds when busy, every `poll_interval_paused_seconds`
when idle). If either condition is true:

- `avail_gb < min_free_gb` (default 2.0 GB)
- `used_percent > (100 - min_free_percent)` (default 90%)

…the daemon:

1. Bulk-pauses every non-terminal job (`PENDING` and `PROCESSING`) with a
   `paused` event tagged `bulk: disk_low`.
2. Stops pulling new jobs.
3. Sleeps and rechecks every `poll_interval_paused_seconds`.

When disk recovers, the breaker detects the edge and bulk-resumes every
paused job, logging `disk_recovered — resumed N paused job(s)`.

This is the daemon's main defense against the cloud-disk `Errno 28` failure
mode. Tune the thresholds to your disk size — on a 30 GB VM, 2 GB free is
comfortable; on a 200 GB box you might prefer 5 GB so you have headroom for
logs and other tenants.

---

## Robustness — what happens when...

| Scenario | What the daemon does |
|---|---|
| Daemon process is killed | `systemd`/`docker` restarts it → `recover_crashed_jobs` resets `PROCESSING` rows. |
| Two daemons launched against the same workspace | The second fails to acquire `<workspace>/daemon.lock` and exits. SQLite is never double-opened. |
| `$EPUB_COMMENTOR_API_KEY` missing | First job lands in `FAILED` with `error_stage=api_key`. Daemon stays up — set the env var and `epubctl retry`. |
| Disk fills mid-run | `OSError: [Errno 28]` propagates as `FAILED` with `error_stage=disk_full`. The breaker then pauses every other job. |
| OOM (kernel kills the worker) | Same as a daemon crash — `recover_crashed_jobs` resets on restart. |
| Ctrl-C in the daemon's terminal | SIGINT → `request_abort()` → in-flight job raises `CommentAbortError` → row becomes `CANCELLED`. Daemon exits cleanly. |
| `epubctl cancel <id>` mid-job | Control signal row → worker picks it up on next iteration → same path as Ctrl-C. |
| Cache has a poisoned entry | The pipeline's `LLMContext.discard_last` evicts it on validation failure; the daemon also wipes the per-job `cache/` on `FAILED` so the next retry starts clean. |
| Logs filling the disk | `SUCCESS` tar-gz's every `logs/*.log` into `logs/archive.tar.gz` and removes the originals. |
| Stale `PROCESSING` after a crash | The 1-hour rule in `recover_crashed_jobs` escalates it to `FAILED` (`error_stage=timeout`) so it doesn't loop. |

---

## Deploying the daemon

### systemd unit (Linux)

A minimal unit you can drop into `/etc/systemd/system/`:

```ini
[Unit]
Description=EPUB Commentor Daemon
After=network-online.target

[Service]
Type=simple
User=you
WorkingDirectory=/home/you
Environment="EPUB_COMMENTOR_API_KEY=sk-..."
ExecStart=/home/you/.local/bin/python -m epub_commentor.daemon --workspace /home/you/daemon
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now epub-commentor
journalctl -u epub-commentor -f      # live tail
```

`Restart=on-failure` plus `RestartSec=30` covers crashes; the daemon's own
`recover_crashed_jobs` makes sure no job is left in `PROCESSING`.

### Docker / containers

The daemon is a single blocking Python process — drop it in a container the
same way you would the CLI:

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

ENV EPUB_COMMENTOR_API_KEY=""
VOLUME ["/daemon"]
CMD ["python", "-m", "epub_commentor.daemon", "--workspace", "/daemon"]
```

```bash
docker build -t epub-daemon .
docker run -d \
    --name epub-daemon \
    -e EPUB_COMMENTOR_API_KEY=sk-... \
    -v /srv/daemon:/daemon \
    --restart unless-stopped \
    epub-daemon
```

Mount `/daemon` on a host volume so SQLite and `jobs/` survive container
restarts. `epubctl` can run on the host against the same volume:

```bash
epubctl --db /srv/daemon/daemon.sqlite status
```

### Single-instance guarantee

The daemon opens `<workspace>/daemon.lock` and tries `fcntl.flock(LOCK_EX |
LOCK_NB)`. If the lock is already held (another daemon is running), the new
process exits immediately. On Windows where `fcntl` isn't available, the
guard degrades to a PID check — the second daemon still exits but the race
window is wider.

This means:

- You can run `epubctl` freely from any terminal — it never opens the lock.
- You must run exactly **one** daemon per workspace.
- To migrate to a new machine, stop the old daemon first.

### Graceful shutdown

The daemon installs signal handlers for `SIGINT` (Ctrl-C) and `SIGTERM`:

1. Set a shutdown event.
2. Call `request_abort()` so the in-flight LLM call raises
   `CommentAbortError`.
3. The current job finishes with `CANCELLED`.
4. The worker exits the loop.
5. The lock and the SQLite connection are released.
6. Exit code `0`.

On the next startup `recover_crashed_jobs` runs — but if shutdown was
graceful, there's nothing to recover (the cancelled job is already in
`CANCELLED`).

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `format.daemon.json is not valid JSON` | Typo (often a trailing comma). Validate with any JSON linter. |
| Daemon exits with "could not open … daemon.lock" | Another daemon is already running against the same workspace. `epubctl status` will tell you. |
| Job stuck in `PENDING` | Worker is paused (`pause-all` or disk breaker) or the daemon isn't running. Check `epubctl status` and `epubctl health`. |
| Job in `PAUSED` with no operator pause | Disk circuit breaker tripped — free space on the workspace volume, the daemon auto-resumes once it's safe. |
| `error_stage=api_key` on first run | `$EPUB_COMMENTOR_API_KEY` not set, and `format.json` has no `key`. Set the env var and `epubctl retry <id>`. |
| `error_stage=flag: --review is not supported in the daemon` | You submitted a job with interactive flags. The daemon has no TTY — use `--ai-review` or `--no-review` instead. |
| `error_stage=disk_full` mid-run | The disk filled despite the breaker. Free space, raise `min_free_gb`, and `epubctl retry`. |
| `epubctl` prints "database not found at …" | Pass `--db`, set `$EPUBCTL_DAEMON_DB`, or run from the workspace directory. |
| Daemon restarted itself (and a job moved `PROCESSING→PENDING`) | This is normal — `recover_crashed_jobs` did its job. Check `epubctl events <id>` for the `restarted` event. |
| `error_stage=timeout` on a fresh job | `PROCESSING > 1h` on restart — the previous daemon was killed mid-job. Inspect logs and `epubctl retry` if you want to re-queue. |
| Worker prints "ignoring unknown keys" | A typo in `format.json` or `--flags-json`. Fix and reload the daemon. |

---

## FAQ

**Why no HTTP API?**
The single user / single host case doesn't need one. `epubctl` talks to the
SQLite database directly — faster, no auth surface, no port to firewall.
If you need to drive the daemon from a different host, run `epubctl` over
SSH; that's still simpler than deploying FastAPI + JWT.

**Why not use `subprocess` per job (like Celery)?**
Three reasons. (1) The CLI calls `sys.exit(2)` on bad input; that kills the
worker. (2) The LLM rate limiter is an in-process global — multiple
processes would each have their own counter and collectively blow past the
provider's cap. (3) Crashes are easier to recover from in-process via
`recover_crashed_jobs`.

**Why no email / webhook notifications?**
Out of scope for the first cut. The `notification_command` config hook
exists for a single shell invocation per event — write your own wrapper to
fan out to email / Slack / webhook / etc.

**Why single-threaded?**
Long EPUB annotation is rate-limited by upstream tokens/second, not by CPU.
Adding more workers would just race for the same provider quota. Keep the
daemon simple; if you need parallelism, run two daemons against different
workspaces (each with its own API key).

**Why no watchdog / file-drop submission?**
Container deployments don't need a directory watcher — the host runs
`epubctl submit`. If you really want auto-submit, wrap `epubctl submit` in
an `inotifywait` one-liner.

**Where does the daemon store its logs?**
On stderr (via the project root logger) and per-job in
`<workspace>/jobs/job_<id>/logs/`. The daemon-level log level is
`log_level` in `format.daemon.json`; the per-job log directory is set by
the worker's `log_dir_path` (default under the job's `logs/`).

**Can I `epubctl submit` while the daemon is down?**
Yes — `epubctl` just writes to SQLite. The daemon picks up the new row on
its next poll cycle.

**What's the cost saving from running multiple jobs in sequence vs. parallel?**
None, but you also don't *lose* anything — the upstream is the bottleneck,
not the daemon. Sequential gives you predictable token spend per book and
simpler failure recovery. Parallel daemons (separate workspaces, separate
API keys) make sense if you've outgrown one provider key.

**Can I move a job's workspace to a different machine?**
Yes — `epubctl prune` deletes the row and the directory, and you can `cp
-r jobs/job_<id>/input.epub` elsewhere. There's no built-in "export to
remote" because the daemon has no remote concept.

**Does the daemon cache responses across jobs?**
No — each job has its own `cache/` directory under its workspace. That's
intentional: a stale cache entry from a previous run with different
`format.json` settings could otherwise pollute the new run. If you want
shared caches, set the same `cache_path` in `format.json` and stop using
per-job workspaces — but you lose isolation.