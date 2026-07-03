#!/usr/bin/env bash
# Demo: spin up the daemon in a tmp workspace, submit a tiny EPUB, watch it run.
#
# Usage:
#   bash scripts/demo_daemon.sh
#
# Requires:
#   - poetry environment with the project dependencies installed
#   - EPUB_COMMENTOR_API_KEY set to a *real* key for whichever model
#     `format.json` points at. The daemon does no network mocking; an
#     invalid key will land the demo job in FAILED with a 401 from
#     the upstream provider. To prove plumbing without spending credits
#     patch `epub_commentor.daemon.worker._build_llm` to return a
#     `tests._mock_llm.MockLLM` and re-run.
#
# On exit (Ctrl-C, failure, or success) the workspace is removed and
# the daemon is sent SIGTERM.

set -euo pipefail

# Use a temp workspace so the demo is self-contained and cleans up.
WORKSPACE=$(mktemp -d)
DB_PATH="$WORKSPACE/daemon.sqlite"
LOG_DIR="$WORKSPACE/logs"
mkdir -p "$LOG_DIR"

cleanup() {
    if [[ -n "${DAEMON_PID:-}" ]]; then
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    rm -rf "$WORKSPACE"
    echo
    echo "demo workspace cleaned up"
}
trap cleanup EXIT INT TERM

echo "workspace = $WORKSPACE"
echo

# --- Launch daemon ---
poetry run python -m epub_commentor.daemon \
    --workspace "$WORKSPACE" \
    < /dev/null > "$LOG_DIR/daemon.out" 2> "$LOG_DIR/daemon.err" &
DAEMON_PID=$!
echo "daemon pid = $DAEMON_PID (logs: $LOG_DIR/daemon.{out,err})"
sleep 2

# Build a minimal EPUB on the fly (the test one might not be present).
EPUB="$WORKSPACE/tiny.epub"
python - "$EPUB" <<'PY'
import sys, zipfile
epub = sys.argv[1]
with zipfile.ZipFile(epub, "w") as zf:
    zf.writestr("mimetype", "application/epub+zip")
    zf.writestr(
        "META-INF/container.xml",
        '<?xml version="1.0"?>'
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        '<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles>'
        '</container>',
    )
    zf.writestr(
        "ch1.xhtml",
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head><title>Tiny</title></head>'
        '<body><h1>Tiny</h1><p>Hello</p><p>World</p></body></html>',
    )
    zf.writestr(
        "book.opf",
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">'
        '<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="ch1"/></spine></package>',
    )
print(f"wrote {epub}")
PY

# --- Submit ---
echo
echo "--- submit ---"
poetry run python -m epub_commentor.ctl --db "$DB_PATH" submit "$EPUB" --priority 1

echo
echo "--- status ---"
poetry run python -m epub_commentor.ctl --db "$DB_PATH" status

echo
echo "--- watch for up to 30s ---"
END=$(( $(date +%s) + 30 ))
while [[ $(date +%s) -lt $END ]]; do
    LINE=$(poetry run python -m epub_commentor.ctl --db "$DB_PATH" status 2>/dev/null | grep -E "^[0-9]+" || true)
    [[ -z "$LINE" ]] && break
    STATUS=$(echo "$LINE" | awk '{print $2}')
    if [[ "$STATUS" == "SUCCESS" || "$STATUS" == "FAILED" || "$STATUS" == "CANCELLED" ]]; then
        break
    fi
    sleep 2
done

echo
echo "--- show 1 ---"
poetry run python -m epub_commentor.ctl --db "$DB_PATH" show 1 || true

echo
echo "--- log 1 (tail 20) ---"
poetry run python -m epub_commentor.ctl --db "$DB_PATH" log 1 --tail 20 || true

echo
echo "--- health ---"
poetry run python -m epub_commentor.ctl --db "$DB_PATH" health

echo
echo "--- daemon logs ---"
echo "[daemon.out]:"; cat "$LOG_DIR/daemon.out" || true
echo
echo "[daemon.err]:"
cat "$LOG_DIR/daemon.err" 2>/dev/null | tail -30 || true
