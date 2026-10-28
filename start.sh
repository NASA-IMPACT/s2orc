#!/usr/bin/env bash
# Start the Scholar Search API (src/server/api.py) under gunicorn.
#
# Runs from the repo root whatever directory it is called from. Python and all
# packages come from uv (pyproject.toml + uv.lock): the script syncs the
# environment and runs gunicorn through `uv run`, so a fresh checkout only
# needs uv and Java 21. Server settings live in src/server/gunicorn.conf.py.
#
# Configuration: environment variables, or a .env file at the repo root
# (copy .env.example). Variables already set in the shell win over .env.
#
#   INDEX_DIR         Lucene index (contents + title fields)
#                                         (default: data/s2orc_index; FULL_TEXT_INDEX_DIR also accepted)
#   RAW_SOURCE        Where result records are read: s3 (default) or disk
#   RAW_S3_MAP_PATH   s3: record -> S3 byte-range map from src/server/download_from_s3.sh
#                     or repack_raw_to_s3.py (default: data/raw_s3_map.sqlite;
#                     AWS credentials from the usual boto3 sources)
#   ORIGINAL_DATA_PATH
#                     disk: decompressed S2ORC .jsonl files (default: data/s2orc_papers)
#   DOI_MAP_PATH      SQLite DOI map from build_doi_map.py (default: data/doi_map.sqlite)
#   HOST              Bind address        (default: 0.0.0.0)
#   PORT              Port                (default: 8000)
#   WORKERS           Worker processes    (default: 16, see gunicorn.conf.py)
#   SCHOLAR_API_KEYS  Comma-separated bearer tokens (optional)
#   SCHOLAR_API_KEYS_FILE
#                     File with one token per line, "#" comments allowed
#                     (default: ~/.scholar/api_keys when that file exists)
#                     With no keys from either source the API is OPEN and a
#                     warning is printed.
#   PYTHON            Interpreter to use when uv is not installed
#                     (default: <repo>/.venv/bin/python)
#
# Usage:
#   ./start.sh
#   PORT=8001 WORKERS=2 ./start.sh
#   SCHOLAR_API_KEYS=team-key-1,team-key-2 ./start.sh
#   RAW_SOURCE=disk ORIGINAL_DATA_PATH=/mnt/s2orc ./start.sh   # records from local .jsonl files
#   ./start.sh --reload      # dev: restart workers when a loaded .py changes
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
#       nohup ./start.sh > server.log 2>&1 &
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
#   In Docker: docker compose up -d api / logs -f api / restart api (see docker-compose.yml).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$REPO_ROOT/src/server"
cd "$REPO_ROOT"

fail() { echo "ERROR: $*" >&2; exit 1; }

# .env: KEY=VALUE lines, without overriding variables already set in the shell.
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        key="${line%%=*}"; value="${line#*=}"
        [ -z "${!key:-}" ] && export "$key=$value"
    done < .env
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# Python: uv manages the environment from uv.lock. Without uv, fall back to an
# existing interpreter ($PYTHON or the repo's .venv).
if command -v uv >/dev/null 2>&1; then
    uv sync --frozen --no-dev --quiet || fail "uv sync failed (is uv.lock present and the network reachable?)"
    run_py() { uv run --frozen --no-dev python "$@"; }
    PY_DESC="uv ($(uv run --frozen --no-dev python -c 'import sys; print(sys.executable)'))"
else
    PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
    [ -x "$PYTHON" ] || fail "uv not found and no interpreter at $PYTHON. Install uv (https://docs.astral.sh/uv/) or set PYTHON."
    run_py() { "$PYTHON" "$@"; }
    PY_DESC="$PYTHON (uv not installed)"
fi
command -v java >/dev/null 2>&1 || fail "Java not found. Pyserini needs Java 21 (see README.md)."
run_py -c "import fastapi, gunicorn, uvicorn_worker, ujson, orjson, pyserini, Stemmer" 2>/dev/null \
    || fail "Missing Python packages. Run: uv sync"

# Resolve the paths api.py will load (same defaults as api.py), so a missing
# index fails here rather than as a stack trace from the server's startup hook.
INDEX_DIR="${INDEX_DIR:-${FULL_TEXT_INDEX_DIR:-$REPO_ROOT/data/s2orc_index}}"
RAW_SOURCE="${RAW_SOURCE:-s3}"
RAW_S3_MAP_PATH="${RAW_S3_MAP_PATH:-$REPO_ROOT/data/raw_s3_map.sqlite}"
ORIGINAL_DATA_PATH="${ORIGINAL_DATA_PATH:-$REPO_ROOT/data/s2orc_papers}"
DOI_MAP_PATH="${DOI_MAP_PATH:-$REPO_ROOT/data/doi_map.sqlite}"
[ -d "$INDEX_DIR" ] || fail "INDEX_DIR not found: $INDEX_DIR (build the index or set INDEX_DIR)"
case "$RAW_SOURCE" in
    s3)   [ -f "$RAW_S3_MAP_PATH" ] || fail "RAW_S3_MAP_PATH not found: $RAW_S3_MAP_PATH (run src/server/download_from_s3.sh, or set RAW_SOURCE=disk)" ;;
    disk) [ -d "$ORIGINAL_DATA_PATH" ] || fail "ORIGINAL_DATA_PATH not found: $ORIGINAL_DATA_PATH (set ORIGINAL_DATA_PATH, or RAW_SOURCE=s3)" ;;
    *)    fail "RAW_SOURCE must be s3 or disk, not '$RAW_SOURCE'" ;;
esac
printf '%-20s %s\n' "Python" "$PY_DESC"
printf '%-20s %s\n' "INDEX_DIR" "$INDEX_DIR"
if [ "$RAW_SOURCE" = s3 ]; then
    printf '%-20s %s\n' "RAW_SOURCE" "s3 (map $RAW_S3_MAP_PATH)"
else
    printf '%-20s %s\n' "RAW_SOURCE" "disk ($ORIGINAL_DATA_PATH)"
fi
printf '%-20s %s\n' "DOI_MAP_PATH" "$DOI_MAP_PATH"

if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    fail "Port $PORT is already in use. Pick another with PORT=<port> ./start.sh"
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

export HOST PORT WORKERS INDEX_DIR RAW_SOURCE RAW_S3_MAP_PATH ORIGINAL_DATA_PATH DOI_MAP_PATH
export PYTHONUNBUFFERED=1   # worker log lines appear immediately
exec run_py -m gunicorn -c "$APP_DIR/gunicorn.conf.py" api:app "$@"
