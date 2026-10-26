#!/usr/bin/env bash
# Start the scholar search API (src/server/api.py) under gunicorn.
#
# Runs from the repo root whatever directory it is called from. Server
# settings live in src/server/gunicorn.conf.py.
#
# Environment variables:
#   INDEX_DIR         Lucene index (contents + title fields)
#                                         (default: data/s2orc_index; FULL_TEXT_INDEX_DIR also accepted)
#   ORIGINAL_DATA_PATH
#                     Decompressed S2ORC .jsonl files (default: data/s2orc_papers)
#   DOI_MAP_PATH      SQLite DOI map from build_doi_map.py (default: data/doi_map.sqlite)
#   PYTHON            Interpreter to use (default: <repo>/.venv/bin/python, else python3 on PATH)
#   HOST              Bind address        (default: 0.0.0.0)
#   PORT              Port                (default: 8000)
#   WORKERS           Worker processes    (default: 16, see gunicorn.conf.py)
#   SCHOLAR_API_KEYS  Comma-separated bearer tokens (optional)
#   SCHOLAR_API_KEYS_FILE
#                     File with one token per line, "#" comments allowed
#                     (default: ~/.scholar/api_keys when that file exists)
#                     With no keys from either source the API is OPEN and a
#                     warning is printed.
#
# Usage:
#   src/server/start.sh
#   PORT=8001 WORKERS=2 src/server/start.sh
#   SCHOLAR_API_KEYS=team-key-1,team-key-2 src/server/start.sh
#   INDEX_DIR=/mnt/idx ORIGINAL_DATA_PATH=/mnt/s2orc src/server/start.sh
#   src/server/start.sh --reload      # dev: restart workers when a loaded .py changes
#                                     # (extra arguments are passed to gunicorn)
#
# Workers: each one starts its own JVM and opens its own Lucene searcher
# (~1.3 GB RSS and ~15 s startup per worker), so the service is ready about
# 15 s after launch regardless of WORKERS. gunicorn restarts a worker that
# dies.
#
# Reload: gunicorn's --reload only watches the Python modules the app has
# imported, so it is cheap here (unlike uvicorn's, which polls the whole repo
# including the data and index directories).
#
# Managing the running server (all from the repo root):
#   Start detached, logging to server.log:
#       nohup src/server/start.sh > server.log 2>&1 &
#       tail -f server.log                   # watch startup; Ctrl-C stops only the tail
#   Is it up?
#       curl -s localhost:8000/health
#       cat src/server/gunicorn-8000.pid     # master pid (gunicorn-<PORT>.pid), written by gunicorn.conf.py
#   Reload after a code change (zero downtime: new workers start, old ones
#   finish their requests and exit; ~15 s per worker for JVM start):
#       kill -HUP "$(cat src/server/gunicorn-8000.pid)"
#   Stop (graceful; waits up to graceful_timeout for in-flight requests):
#       kill -TERM "$(cat src/server/gunicorn-8000.pid)"
#   Stop immediately:
#       kill -QUIT "$(cat src/server/gunicorn-8000.pid)"
#   If the pid file is stale (e.g. after a crash) find the master with:
#       pgrep -f "gunicorn -c .*gunicorn.conf.py" | head -1
#   Keys: edit ~/.scholar/api_keys, then HUP to reload them.
#   Warm the page cache (after a start, a reboot, or a big disk job), while
#   serving is fine:
#       nohup src/server/cache_warm.sh > cache_warm.log 2>&1 &

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$APP_DIR/../.." && pwd)"
cd "$REPO_ROOT"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

fail() { echo "ERROR: $*" >&2; exit 1; }

if [ -z "${PYTHON:-}" ]; then
    if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
        PYTHON="$REPO_ROOT/.venv/bin/python"
    else
        PYTHON="$(command -v python3 || true)"
    fi
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || fail "No Python interpreter found. Create a venv (see README.md) or set PYTHON=/path/to/python"
command -v java >/dev/null 2>&1 || fail "Java not found. Pyserini needs Java 21 (see README.md)."
"$PYTHON" -c "import fastapi, gunicorn, uvicorn_worker, ujson, orjson, pyserini, Stemmer" 2>/dev/null \
    || fail "Missing Python packages. Run: $PYTHON -m pip install -r src/requirements.txt"

# Resolve the paths api.py will load (same defaults as api.py), so a missing
# index fails here rather than as a stack trace from the server's startup hook.
INDEX_DIR="${INDEX_DIR:-${FULL_TEXT_INDEX_DIR:-$REPO_ROOT/data/s2orc_index}}"
ORIGINAL_DATA_PATH="${ORIGINAL_DATA_PATH:-$REPO_ROOT/data/s2orc_papers}"
DOI_MAP_PATH="${DOI_MAP_PATH:-$REPO_ROOT/data/doi_map.sqlite}"
[ -d "$INDEX_DIR" ] || fail "INDEX_DIR not found: $INDEX_DIR (build the index or set INDEX_DIR)"
[ -d "$ORIGINAL_DATA_PATH" ] || fail "ORIGINAL_DATA_PATH not found: $ORIGINAL_DATA_PATH (set ORIGINAL_DATA_PATH)"
printf '%-20s %s\n' "INDEX_DIR" "$INDEX_DIR"
printf '%-20s %s\n' "ORIGINAL_DATA_PATH" "$ORIGINAL_DATA_PATH"
printf '%-20s %s\n' "DOI_MAP_PATH" "$DOI_MAP_PATH"

if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    fail "Port $PORT is already in use. Pick another with PORT=<port> src/server/start.sh"
fi

WORKERS="${WORKERS:-16}"
DEFAULT_KEYS_FILE="$HOME/.scholar/api_keys"
if [ -z "${SCHOLAR_API_KEYS:-}" ] && [ -z "${SCHOLAR_API_KEYS_FILE:-}" ] && [ -f "$DEFAULT_KEYS_FILE" ]; then
    export SCHOLAR_API_KEYS_FILE="$DEFAULT_KEYS_FILE"
fi
if [ -n "${SCHOLAR_API_KEYS_FILE:-}" ]; then
    [ -r "$SCHOLAR_API_KEYS_FILE" ] || fail "SCHOLAR_API_KEYS_FILE not readable: $SCHOLAR_API_KEYS_FILE"
    echo "Auth:                keys from $SCHOLAR_API_KEYS_FILE"
elif [ -n "${SCHOLAR_API_KEYS:-}" ]; then
    echo "Auth:                keys from SCHOLAR_API_KEYS"
else
    echo "WARNING: no API keys configured (SCHOLAR_API_KEYS / SCHOLAR_API_KEYS_FILE / $DEFAULT_KEYS_FILE); the API is open to anyone who can reach it." >&2
fi
if [ ! -f "$DOI_MAP_PATH" ]; then
    echo "NOTE: $DOI_MAP_PATH missing; /doi returns 503 until src/server/build_doi_map.py has run." >&2
fi

echo "Serving on:          http://$HOST:$PORT  (docs at /docs)"
echo "Workers:             $WORKERS  (each ~1.3 GB; ready in ~15 s)"

export HOST PORT WORKERS INDEX_DIR ORIGINAL_DATA_PATH DOI_MAP_PATH
export PYTHONUNBUFFERED=1   # worker log lines appear immediately
exec "$PYTHON" -m gunicorn -c "$APP_DIR/gunicorn.conf.py" api:app "$@"
