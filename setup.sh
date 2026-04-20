#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# setup.sh — one-shot, idempotent setup for PED Tools.
#
# Safe to run on:
#   - a fresh checkout (creates venv, installs deps, initialises DB)
#   - an existing install (upgrades deps, backs up DB, adds any missing tables)
#
# Does not touch existing rows. Never drops tables. Always backs up the DB
# before running bootstrap so a failed run can be rolled back.
#
# Usage:
#   ./setup.sh              # full setup (default)
#   ./setup.sh --no-venv    # use current python (skip venv create/activate)
#   ./setup.sh --skip-deps  # skip pip install
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
USE_VENV=1
SKIP_DEPS=0

for arg in "$@"; do
    case "$arg" in
        --no-venv)   USE_VENV=0 ;;
        --skip-deps) SKIP_DEPS=1 ;;
        -h|--help)
            sed -n '2,18p' "$0"; exit 0 ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Run with --help to see usage." >&2
            exit 2 ;;
    esac
done

log() { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# 1. Resolve Python interpreter (venv or system)
# -----------------------------------------------------------------------------
if [ "$USE_VENV" -eq 1 ]; then
    if [ ! -d "$VENV_DIR" ]; then
        log "Creating virtual environment at $VENV_DIR"
        python3 -m venv "$VENV_DIR"
    else
        log "Reusing existing virtual environment at $VENV_DIR"
    fi
    PY="$SCRIPT_DIR/$VENV_DIR/bin/python"
    PIP="$SCRIPT_DIR/$VENV_DIR/bin/pip"
else
    PY="$(command -v python3 || true)"
    PIP="$(command -v pip3 || true)"
    [ -x "$PY" ] || die "python3 not found on PATH"
    [ -x "$PIP" ] || die "pip3 not found on PATH"
    log "Using system python: $PY"
fi

# -----------------------------------------------------------------------------
# 2. Install / upgrade dependencies
# -----------------------------------------------------------------------------
if [ "$SKIP_DEPS" -eq 0 ]; then
    log "Installing requirements.txt"
    "$PIP" install --quiet --upgrade pip
    "$PIP" install --quiet -r requirements.txt
else
    log "Skipping dependency install (--skip-deps)"
fi

# -----------------------------------------------------------------------------
# 3. Resolve DB path (honours .env via PED_DB_PATH, same as app.py)
# -----------------------------------------------------------------------------
DB_PATH="$(
    "$PY" - <<'PYEOF'
import os
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env" if "__file__" in dir() else ".env")
except Exception:
    pass
base = os.path.dirname(os.path.abspath("."))
print(os.environ.get("PED_DB_PATH", os.path.join(".", "pedapp.db")))
PYEOF
)"

# -----------------------------------------------------------------------------
# 4. Back up existing DB before any mutation
# -----------------------------------------------------------------------------
if [ -f "$DB_PATH" ]; then
    TS="$(date +%Y%m%d_%H%M%S)"
    BACKUP="${DB_PATH}.bak.${TS}"
    cp -p "$DB_PATH" "$BACKUP"
    log "Backed up existing DB: $BACKUP"
else
    log "No existing DB at $DB_PATH — will be created"
fi

# -----------------------------------------------------------------------------
# 5. Run bootstrap (idempotent: CREATE TABLE IF NOT EXISTS + one-shot JSON import)
# -----------------------------------------------------------------------------
log "Running bootstrap.py (init schema)"
"$PY" bootstrap.py

# -----------------------------------------------------------------------------
# 6. Verify schema — read-only sanity check before reporting success
# -----------------------------------------------------------------------------
log "Verifying schema"
"$PY" - <<'PYEOF'
import os, sqlite3, sys
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path.cwd() / ".env")
except Exception:
    pass
db = os.environ.get("PED_DB_PATH", "pedapp.db")
required = {"proxies", "mocks", "request_history", "mock_sequences", "mock_state"}
conn = sqlite3.connect(db)
try:
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
finally:
    conn.close()
missing = required - present
if missing:
    print(f"[setup] MISSING TABLES: {sorted(missing)}", file=sys.stderr)
    sys.exit(1)
print(f"[setup] schema ok ({len(required)} required tables present) at {db}")
PYEOF

log "Done. Start the server with: ./run.sh  (or: $PY app.py)"
