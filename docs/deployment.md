# EPUB Commentor — Deployment Guide

A complete, copy-pasteable walkthrough that takes a fresh Ubuntu server from
"empty box" to "daemon running under systemd, first book submitted and finished".
After the steps you'll find a snapshot of the final filesystem layout so you
can see at a glance where everything lives.

The example targets **Ubuntu 22.04 LTS or 24.04 LTS** and uses **systemd
(user-level)** to run the daemon — no root required, no `sudo` for normal
operation, secrets stay in a 0600 file under your home directory. The same
shape works on any Debian-family / systemd distro; see the [Alternatives
appendix](#alternatives) at the bottom.

> Looking for the daemon's behaviour reference (every config key, every
> `epubctl` subcommand, the state machine, etc.)? See
> [`daemon.md`](./daemon.md).

---

## Table of contents

- [Prerequisites](#prerequisites)
- [0. Pick a layout (read this first)](#0-pick-a-layout-read-this-first)
- [1. Install Python 3.13](#1-install-python-313)
- [2. Install Poetry](#2-install-poetry)
- [3. Clone the repo and install the package](#3-clone-the-repo-and-install-the-package)
- [4. Create the workspace directory](#4-create-the-workspace-directory)
- [5. Drop in the two config files](#5-drop-in-the-two-config-files)
- [6. Store the API key in a private env file](#6-store-the-api-key-in-a-private-env-file)
- [7. Write the systemd (user-level) unit](#7-write-the-systemd-user-level-unit)
- [8. Start the daemon](#8-start-the-daemon)
- [9. Submit your first book](#9-submit-your-first-book)
- [10. Read the output on your reader](#10-read-the-output-on-your-reader)
- [Day-2: update, back up, clean up](#day-2-update-back-up-clean-up)
- [Troubleshooting](#troubleshooting)
- [Appendix: final filesystem snapshot](#appendix-final-filesystem-snapshot)
- [Alternatives](#alternatives)

---

## Prerequisites

- An Ubuntu 22.04+ server (or VM / cloud instance) you can SSH into.
- A non-root user with `sudo` access for the Python install.
- An OpenAI-compatible API key (OpenAI, DeepSeek, Azure, etc.).
- One `.epub` file ready on the box (you'll use it as a smoke test in step 9).

You do **not** need: a domain name, a reverse proxy, an HTTP port, TLS, a
database server, Docker (though see the [alternatives](#alternatives)).

---

## 0. Pick a layout (read this first)

This guide sticks to one consistent path layout so commands are
copy-pasteable. Adjust the prefix if you prefer, but don't move pieces
around mid-walkthrough.

| What | Path | Owned by |
|---|---|---|
| Repo + Python venv | `~/epub-commentor/` | you |
| Daemon workspace (SQLite + per-job dirs) | `~/epub-daemon/` | you |
| Daemon config | `~/epub-daemon/format.daemon.json` | you (0600) |
| LLM credentials | `~/epub-daemon/format.json` | you (0600, no key) |
| API key env file | `~/epub-daemon/.env` | you (0600) |
| systemd unit | `~/.config/systemd/user/epub-commentor.service` | you |
| Books to annotate | `~/books/` | you |

The daemon runs under **your user account** (not root), so every file the
daemon creates is owned by you and no special permissions are needed.

---

## 1. Install Python 3.13

Ubuntu 22.04 ships Python 3.10; 24.04 ships 3.12. Project requires 3.13.

**Option A — deadsnakes PPA (simplest):**

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.13 python3.13-venv python3.13-dev
python3.13 --version   # should print Python 3.13.x
```

**Option B — uv (modern, fast, all-in-one):**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# log out and back in (or `source ~/.bashrc`) so uv is on PATH
uv python install 3.13
```

Either path is fine — pick A if you want plain `apt`, B if you want one tool
that also handles the venv later.

---

## 2. Install Poetry

Poetry manages the project venv and the console scripts (`epubctl`,
`epub-commentor`).

```bash
curl -sSL https://install.python-poetry.org | python3.13 -
# log out and back in (or `source ~/.bashrc`)
poetry --version
```

Verify `~/.local/bin` is on your `PATH`:

```bash
echo "$PATH" | tr ':' '\n' | grep -E '\.local/bin' || echo 'add export PATH=$HOME/.local/bin:$PATH to ~/.bashrc'
```

---

## 3. Clone the repo and install the package

```bash
cd ~
git clone https://github.com/noau/epub-commentor.git
cd epub-commentor
poetry install
```

`poetry install` builds the venv under `~/epub-commentor/.venv/` and installs
two console scripts **into that venv**:

- `epub-commentor` (the one-shot CLI)
- `epubctl` (the daemon client)

You can confirm:

```bash
poetry run which epubctl
# → /home/you/epub-commentor/.venv/bin/epubctl
```

Two ways to invoke `epubctl` later in this guide:

```bash
# (1) Always prefix with `poetry run` — works from anywhere
poetry --directory ~/epub-commentor run epubctl status

# (2) Activate the venv once per shell
source ~/epub-commentor/.venv/bin/activate
epubctl status   # no prefix needed
```

Pick whichever you prefer. The examples below use `(1)` because it's
copy-pasteable across shells.

---

## 4. Create the workspace directory

```bash
mkdir -p ~/epub-daemon
chmod 700 ~/epub-daemon
```

This will hold the SQLite queue (`daemon.sqlite`), the per-job workspaces
(`jobs/job_<id>/`), and the two config files.

---

## 5. Drop in the two config files

The daemon reads **two** flat JSON files:

| File | Purpose |
|---|---|
| `format.daemon.json` | Workspace path, disk thresholds, log level, poll cadence |
| `format.json` | LLM credentials + per-run defaults (`url`, `model`, …) |

Both are independent of each other — the daemon reads `format.daemon.json`
for its own settings and `format.json` for LLM credentials.

### `format.daemon.json`

```bash
cp ~/epub-commentor/format.daemon.template.json ~/epub-daemon/format.daemon.json
chmod 600 ~/epub-daemon/format.daemon.json
$EDITOR ~/epub-daemon/format.daemon.json
```

At minimum, change `workspace_dir` to the **absolute** path:

```json
{
    "workspace_dir": "/home/you/epub-daemon",
    "sqlite_path": null,
    "log_level": "INFO",
    "log_format": "text",
    "max_retries": 3,
    "disk": {
        "min_free_gb": 2.0,
        "min_free_percent": 10.0
    },
    "shutdown_grace_seconds": 30,
    "poll_interval_idle_seconds": 5,
    "poll_interval_paused_seconds": 60,
    "notification_command": null
}
```

> **Why absolute?** The template's `./daemon_workspace` is relative to the
> daemon's `cwd`. Under systemd that's the unit's `WorkingDirectory`, which
> might not be what you think. Absolute paths remove the guesswork.

For a small server (20–50 GB disk) the defaults are fine. If you have more
room, raise `disk.min_free_gb` to `5.0` so the breaker has headroom for log
spikes.

### `format.json`

```bash
cp ~/epub-commentor/format.template.json ~/epub-daemon/format.json
chmod 600 ~/epub-daemon/format.json
$EDITOR ~/epub-daemon/format.json
```

Edit `url`, `model`, and `token_encoding` for your provider:

```json
{
    "url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "token_encoding": "o200k_base",
    "timeout": 360.0,
    "retry_times": 5,
    "retry_interval_seconds": 6.0,
    "temperature": 0.4,
    "top_p": 0.9,
    "json_mode": false,
    "rpm_limit": 60,
    "tpm_limit": 200000,
    "request_concurrency": 4
}
```

Notice **no `key` field** — we'll wire the API key through an environment
file in the next step. This keeps `format.json` safe to commit.

---

## 6. Store the API key in a private env file

Create `~/epub-daemon/.env`:

```bash
cat > ~/epub-daemon/.env <<'EOF'
EPUB_COMMENTOR_API_KEY=sk-your-secret-api-key
EOF
chmod 600 ~/epub-daemon/.env
$EDITOR ~/epub-daemon/.env   # paste your real key on the right-hand side
```

This file is the **only** place the secret lives on disk. systemd's
`EnvironmentFile=` will load it when starting the daemon.

---

## 7. Write the systemd (user-level) unit

User-level systemd runs services under your account — no root required, no
port collisions with the system instance, no surprise ownership changes.
The unit file lives in your home directory.

```bash
mkdir -p ~/.config/systemd/user
$EDITOR ~/.config/systemd/user/epub-commentor.service
```

```ini
[Unit]
Description=EPUB Commentor Daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/you/epub-commentor
EnvironmentFile=/home/you/epub-daemon/.env
ExecStart=/home/you/epub-commentor/.venv/bin/python -m epub_commentor.daemon --workspace /home/you/epub-daemon
Restart=on-failure
RestartSec=30
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

A few notes:

- **`WorkingDirectory`** must point at the repo, not the workspace — that's
  where `epub_commentor.daemon` is importable from.
- **`ExecStart`** calls the venv's Python directly (no `poetry run`),
  because systemd does not source your shell profile.
- **`EnvironmentFile`** loads the API key from the 0600 file.
- **`Restart=on-failure`** plus `RestartSec=30` covers crashes; the daemon's
  own `recover_crashed_jobs` makes sure no job is left in `PROCESSING`.

Let the systemd user instance know it should run your services even when
you're not logged in:

```bash
loginctl enable-linger "$USER"
```

Reload the unit, enable it at boot, start it now:

```bash
systemctl --user daemon-reload
systemctl --user enable --now epub-commentor.service
```

---

## 8. Start the daemon

```bash
systemctl --user status epub-commentor.service
```

You should see `Active: active (running)`. If not, jump to
[Troubleshooting](#troubleshooting).

Tail the daemon's own log (the systemd journal mirrors the daemon's
stderr):

```bash
journalctl --user -u epub-commentor.service -f
```

You should see lines like:

```
[INFO] epub_commentor.daemon.server: daemon started; workspace=/home/you/epub-daemon
[INFO] epub_commentor.daemon.server: recovered 0 crashed jobs
```

Press `Ctrl-C` to leave the tail running in the background (it does not
stop the daemon).

The daemon has now created its SQLite queue and is waiting for jobs:

```bash
ls -la ~/epub-daemon/
# expect: daemon.sqlite  daemon.sqlite-wal  daemon.sqlite-shm  format.daemon.json
#         format.json  .env  jobs/  ...
```

---

## 9. Submit your first book

In a second terminal (or after `Ctrl-C`-ing the journal tail):

```bash
# Drop a test EPUB on the box if you don't have one yet
mkdir -p ~/books
# (scp / rsync / wget your book into ~/books/ — left as an exercise)

EPUBCTL="poetry --directory ~/epub-commentor run epubctl --db ~/epub-daemon/daemon.sqlite"

$EPUBCTL submit ~/books/your-book.epub \
    --synopsis "One-line description of your book." \
    --priority 1
```

Expected output:

```
job id 1 enqueued
```

Watch the queue:

```bash
$EPUBCTL status --watch
```

The table will refresh every couple of seconds. Press `Ctrl-C` to exit.
You can also filter:

```bash
$EPUBCTL status                 # all jobs, newest first
$EPUBCTL status --status PROCESSING
```

Tail the per-job logs (after the job moves to `PROCESSING`):

```bash
$EPUBCTL log 1 --follow
```

While it's running, the daemon is hitting the LLM. A typical 200-page book
takes 30–90 minutes; a 30-page short story takes ~5 minutes. The
`status` table shows live token counts.

When `status` reports `SUCCESS`, the EPUB is at:

```
~/epub-daemon/jobs/job_1/output.commented.epub
```

You can confirm:

```bash
ls -la ~/epub-daemon/jobs/job_1/
```

---

## 10. Read the output on your reader

Pull the file off the server:

```bash
# From your laptop
scp you@your-server:~/epub-daemon/jobs/job_1/output.commented.epub ~/Downloads/
```

Drop `output.commented.epub` into Calibre / Send-to-Kindle / Kobo /
Apple Books — see the [main README](../README.md#reading-the-result-on-your-device)
for per-device instructions. The original text is untouched; only
bordered `<aside>` blocks have been added.

---

## Day-2: update, back up, clean up

### Update the package

```bash
cd ~/epub-commentor
git pull
poetry install
systemctl --user restart epub-commentor.service
```

`recover_crashed_jobs` will not fire (no crash), and any `PROCESSING` job
will keep running across the restart — the unit's `TimeoutStopSec=60`
gives the worker a chance to land the current chapter cleanly.

### Back up the queue + jobs

The two things worth backing up are:

- `~/epub-daemon/daemon.sqlite` (the queue)
- `~/epub-daemon/jobs/` (per-job input + output + meta)

A small daily cron, owned by you, is enough:

```bash
cat > ~/bin/backup-epub-daemon.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
TS=$(date +%Y%m%d-%H%M)
tar -C ~ -czf ~/backups/epub-daemon-${TS}.tar.gz epub-daemon/daemon.sqlite epub-daemon/jobs
EOF
chmod +x ~/bin/backup-epub-daemon.sh
mkdir -p ~/backups
```

`jobs/` can grow large (every retained EPUB + meta). On a tight disk,
prune old terminals first:

```bash
$EPUBCTL prune --cancelled --force
$EPUBCTL prune --failed --force
```

Keep SUCCESS jobs unless you've already shipped their EPUBs elsewhere.

### Delete the daemon

```bash
systemctl --user disable --now epub-commentor.service
rm ~/.config/systemd/user/epub-commentor.service
systemctl --user daemon-reload
rm -rf ~/epub-daemon
```

The repo and Poetry venv stay around; the `epub-commentor` one-shot CLI
still works as before.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `systemctl --user status` shows `inactive (dead)` | The unit file failed to load. `journalctl --user -u epub-commentor.service -xe` shows the exact error. |
| `ExecStart` says "No such file or directory" | Wrong path to the venv Python. `ls ~/epub-commentor/.venv/bin/python` and fix `ExecStart`. |
| `ModuleNotFoundError: No module named 'epub_commentor.daemon'` | `WorkingDirectory` doesn't point at the repo, or you didn't run `poetry install` after cloning. |
| `format.daemon.json is not valid JSON` | Trailing comma or missing quote. Validate with `python3 -m json.tool ~/epub-daemon/format.daemon.json`. |
| First job lands in `FAILED` with `error_stage=api_key` | The `.env` file isn't loaded or the variable name is wrong. Check `journalctl --user -u epub-commentor.service` for the error and run `systemctl --user show epub-commentor.service -p Environment` to confirm systemd sees the key. |
| `Permission denied` on `~/epub-daemon/daemon.lock` | The daemon was previously run by another user. `rm ~/epub-daemon/daemon.lock` and restart. |
| Job stuck in `PENDING` | Worker is paused (operator `pause-all` or the disk breaker tripped). `epubctl health` shows the reason; `epubctl resume-all` unsticks it. |
| `journalctl --user` returns "Failed to connect" | Your user instance isn't running. `systemctl --user status` — if it says "Failed to fully start", run `loginctl enable-linger $USER`. |
| `epubctl: command not found` even after `poetry install` | You forgot the `poetry run` / `--directory` prefix, or your shell doesn't have `~/.local/bin` on PATH (only matters for the bare `poetry` invocation, not `epubctl`). |

---

## Appendix: final filesystem snapshot

After step 10 your home directory looks roughly like this (omitting the
usual `~/Documents`, `~/.config/...`, etc.):

```
/home/you/
├── epub-commentor/                  # the cloned repo (code + venv)
│   ├── .venv/                       # Poetry-managed venv
│   │   └── bin/
│   │       ├── python -> /usr/bin/python3.13
│   │       ├── epubctl              # console script (daemon client)
│   │       └── epub-commentor       # console script (one-shot CLI)
│   ├── pyproject.toml
│   ├── format.template.json         # untouched templates
│   ├── format.daemon.template.json
│   ├── README.md
│   └── docs/
│       ├── daemon.md
│       └── deployment.md            # this file
│
├── epub-daemon/                     # the daemon workspace (chmod 700)
│   ├── .env                         # API key (chmod 600)
│   ├── format.json                  # LLM credentials, no `key` field (chmod 600)
│   ├── format.daemon.json           # daemon settings (chmod 600)
│   ├── daemon.sqlite                # queue DB (WAL mode)
│   ├── daemon.sqlite-wal
│   ├── daemon.sqlite-shm
│   ├── daemon.lock                  # single-instance guard (created on start)
│   └── jobs/
│       └── job_1/
│           ├── input.epub           # the copy of your submitted book
│           ├── output.commented.epub
│           ├── meta.json            # CommentorResult snapshot
│           ├── commentor.log        # daemon's stderr mirror for this job
│           ├── cache/               # LLM cache (deleted on SUCCESS)
│           └── logs/
│               └── archive.tar.gz   # per-request LLM logs, archived
│
├── books/
│   └── your-book.epub               # the file you submitted
│
├── bin/
│   └── backup-epub-daemon.sh        # optional backup script
│
├── backups/
│   └── epub-daemon-20260703-0200.tar.gz
│
└── .config/
    └── systemd/
        └── user/
            └── epub-commentor.service
```

**Ownership:** everything under `~` is owned by `you:you`. The daemon runs
as you, so it can read/write all of the above without `sudo`.

**Disk budget (rough, per 200-page book):**

| Item | Approx size |
|---|---|
| `input.epub` | 1–3 MB |
| `output.commented.epub` | 1–3 MB |
| `meta.json` | < 100 KB |
| `logs/archive.tar.gz` | 5–20 MB |
| `cache/` (deleted on SUCCESS) | 5–50 MB |

The daemon's `disk.min_free_gb` (default 2 GB) is a safety margin against
the cache growing before the breaker pauses the queue — far above any
single-job cost.

---

## Alternatives

### Distro

The walkthrough works on any Debian-family / systemd host. On RHEL/Fedora
replace `apt` with `dnf`/`yum`; on Arch use `pacman`. The deadsnakes
equivalent on Fedora is `dnf install python3.13` (the official repo
ships 3.13 on recent releases); on Arch it's `pacman -S python`.

### System-wide systemd instead of user-level

If you'd rather run the daemon as a system service (one daemon per host,
regardless of who's logged in):

- Put the unit under `/etc/systemd/system/epub-commentor.service` (root).
- Drop the `WantedBy=` line to `WantedBy=multi-user.target`.
- Replace `EnvironmentFile=/home/you/...` with the same line — the unit
  itself is root-owned, but `EnvironmentFile` is read with the privileges
  systemd was launched with.
- `sudo systemctl daemon-reload && sudo systemctl enable --now
  epub-commentor.service`.

The rest of the layout is identical.

### No systemd at all

You can run the daemon under `tmux`/`screen`/`nohup` for a quick smoke
test, but it loses automatic restart on crash and start-at-boot. Reserve
that mode for laptops or dev boxes.

### Docker

If your team already standardises on containers, see the
[`daemon.md` § Docker / containers](./daemon.md#docker--containers)
section for a `Dockerfile`. The setup steps above are simpler — Docker
adds a layer (image build, volume mount, `docker compose`) that you only
need when you're orchestrating many services at once.