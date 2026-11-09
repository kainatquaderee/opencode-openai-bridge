#!/usr/bin/env bash
# =============================================================================
# opencode-openai-bridge v0.6.0 — launcher
# =============================================================================
# Verifies deps, files, and imports, then starts uvicorn.
#
# Usage:
#   ./start.sh              # full auto: install deps if needed, verify, run
#   ./start.sh --check      # verify + test only, don't start the server
#   ./start.sh --reinstall  # wipe .venv and reinstall before verify
#   ./start.sh --help       # show this help block
#
# Environment (see .env.example for the full list):
#   BRIDGE_HOST     bind host   (default 0.0.0.0)
#   BRIDGE_PORT     bind port   (default 4097)
#   PYTHON          interpreter (default python3)
#
# Notes:
#   - This script never reads the generic $PORT env var, so a stray
#     PORT=20128 exported by OmniRoute cannot hijack the bind port.
#   - A .env file in this directory is loaded first; values already set
#     in the shell take precedence over .env.
#   - Deps are reinstalled only when requirements.txt changes (hash-tracked).
# =============================================================================

set -euo pipefail

# --- Anchor to script directory ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
SCRIPT_LINES=$(wc -l < "$0" | tr -d ' ')
echo "==> start.sh v0.6.0 (${SCRIPT_LINES} lines)"

# --- Arg parsing ---
MODE="run"
for arg in "$@"; do
    case "$arg" in
        --check)     MODE="check" ;;
        --reinstall) rm -rf .venv; echo "==> removed .venv (reinstall)" ;;
        -h|--help)
            sed -n '2,24p' "$0"
            exit 0
            ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done
echo "==> mode: $MODE"

# --- Optional .env loading (shell env wins over .env) ---
if [ -f .env ]; then
    echo "==> loading .env"
    while IFS='=' read -r key val || [ -n "$key" ]; do
        # strip whitespace from key
        key=$(printf '%s' "$key" | tr -d ' \t\r')
        [ -z "$key" ] && continue
        case "$key" in \#*) continue ;; esac
        # strip surrounding quotes and trailing whitespace from value
        val=$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                                        -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
        if [ -z "${!key+x}" ]; then
            export "$key=$val"
        fi
    done < .env
else
    echo "==> no .env file (using environment defaults)"
fi

# --- Python interpreter ---
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "ERROR: $PY not found. Install Python 3.9+ or set PYTHON=<path>." >&2
    exit 1
fi
echo "==> python: $($PY --version 2>&1)"

# --- Virtualenv ---
if [ ! -d .venv ]; then
    echo "==> creating .venv"
    "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
. .venv/bin/activate

# --- Dependencies: reinstall only when requirements.txt changes ---
if command -v sha256sum >/dev/null 2>&1; then
    REQ_HASH=$(sha256sum requirements.txt | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    REQ_HASH=$(shasum -a 256 requirements.txt | awk '{print $1}')
else
    REQ_HASH="unhashed-$(wc -c < requirements.txt)"
fi

PREV_HASH=""
[ -f .venv/.deps-hash ] && PREV_HASH=$(cat .venv/.deps-hash 2>/dev/null || true)

if [ "$PREV_HASH" != "$REQ_HASH" ]; then
    echo "==> installing deps (hash changed)"
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    printf '%s' "$REQ_HASH" > .venv/.deps-hash
else
    echo "==> deps up to date"
fi

# --- Verify files present ---
echo "==> verifying files"
python verify_files.py

# --- Run smoke test ---
echo "==> running smoke test"
python test_imports.py

if [ "$MODE" = "check" ]; then
    echo
    echo "==> check only; not starting server"
    exit 0
fi

# --- Bind host/port (BRIDGE_* namespaced; never inherit generic PORT) ---
BIND_HOST="${BRIDGE_HOST:-0.0.0.0}"
BIND_PORT="${BRIDGE_PORT:-4097}"

if [ -n "${PORT:-}" ] && [ "${PORT}" != "$BIND_PORT" ]; then
    echo "NOTE: shell has PORT=$PORT (ignored). Using BRIDGE_PORT=$BIND_PORT."
fi

# --- Optional: verify opencode binary ---
if command -v "${BRIDGE_OPENCODE_BIN:-opencode}" >/dev/null 2>&1; then
    echo "==> opencode: $(command -v "${BRIDGE_OPENCODE_BIN:-opencode}")"
else
    echo "WARN: opencode not on PATH. Bridge will start but /v1/chat/completions returns 503." >&2
fi

echo
echo "==> starting server on ${BIND_HOST}:${BIND_PORT}"
echo "    OmniRoute base URL: http://localhost:${BIND_PORT}/v1"
echo "    Quick check:        curl -s http://localhost:${BIND_PORT}/v1/models | jq"
echo
exec uvicorn server:app --host "$BIND_HOST" --port "$BIND_PORT"
