#!/usr/bin/env bash
# Start the Scholar Search API (src/server/api.py) under gunicorn.
#
# Runs from the repo root whatever directory it is called from. Python and all
# packages come from uv (pyproject.toml + uv.lock): the script syncs the
# environment and runs gunicorn through `uv run`, so a fresh checkout only
# needs uv and Java 21. Server settings live in src/server/gunicorn.conf.py.
#
# Configuration: command-line flags (./start.sh --help), environment
# variables, or a .env file at the repo root (copy .env.example), in that
# order of precedence. Each flag sets the variable named next to it.
#
#   INDEX_DIR         Lucene index (contents + title fields)
#                                         (default: data/s2orc_index; FULL_TEXT_INDEX_DIR also accepted)
#   ORIGINAL_DATA_PATH
#                     Decompressed S2ORC .jsonl files (default: data/s2orc_papers)
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
#   SCHOLAR_THREADS   Search threads per worker (default: 40)
#   SCHOLAR_TITLE_WEIGHT
#                     Title-field boost in BM25 scoring (default: 2.0)
#   LOG_LEVEL         gunicorn log level (default: info)
#   PYTHON            Interpreter to use when uv is not installed
#                     (default: <repo>/.venv/bin/python)
#
# Usage:
#   ./start.sh
#   ./start.sh --port 8001 --workers 2        # same as PORT=8001 WORKERS=2 ./start.sh
#   ./start.sh --index-dir /mnt/idx --data-dir /mnt/s2orc
#   SCHOLAR_API_KEYS=team-key-1,team-key-2 ./start.sh
#   ./start.sh --cache-warm  # also run src/server/cache_warm.sh in the background
#                                     # (off by default; logs to cache_warm.log)
#   ./start.sh --reload      # dev: restart workers when a loaded .py changes
#                                     # (unknown arguments, and all after --, go to gunicorn)
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
#   serving is fine (or start with --cache-warm to do it at launch):
#       nohup src/server/cache_warm.sh > cache_warm.log 2>&1 &
#   In Docker: docker compose up -d api / logs -f api / restart api (see docker-compose.yml).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$REPO_ROOT/src/server"
cd "$REPO_ROOT"

fail() { echo "ERROR: $*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: ./start.sh [options] [gunicorn arguments...]

Start the Scholar Search API under gunicorn. Each option sets the environment
variable in brackets and wins over the shell environment and .env.

  --host ADDR          bind address                    [HOST, default 0.0.0.0]
  --port N             port                            [PORT, default 8000]
  --workers N          worker processes, ~1.3 GB each  [WORKERS, default 16]
  --threads N          search threads per worker       [SCHOLAR_THREADS, default 40]
  --index-dir DIR      Lucene index                    [INDEX_DIR, default data/s2orc_index]
  --data-dir DIR       decompressed S2ORC .jsonl files [ORIGINAL_DATA_PATH, default data/s2orc_papers]
  --doi-map FILE       SQLite DOI map                  [DOI_MAP_PATH, default data/doi_map.sqlite]
  --keys-file FILE     API keys, one per line          [SCHOLAR_API_KEYS_FILE, default ~/.scholar/api_keys]
  --title-weight X     title-field boost               [SCHOLAR_TITLE_WEIGHT, default 2.0]
  --log-level LEVEL    gunicorn log level              [LOG_LEVEL, default info]
  --cache-warm         also read the index into the page cache in the
                       background (src/server/cache_warm.sh, log: cache_warm.log)
  -h, --help           show this help

Options take "--opt value" or "--opt=value". Unknown arguments, and everything
after "--", are passed to gunicorn (e.g. --reload). See the top of start.sh for
managing a running server.
USAGE
}

# Flags set their environment variable before .env is read, so they win.
CACHE_WARM=0
GUNICORN_ARGS=()
while [ $# -gt 0 ]; do
    opt="$1"; val=""; has_val=0
    case "$opt" in --*=*) val="${opt#*=}"; opt="${opt%%=*}"; has_val=1 ;; esac
    var=""
    case "$opt" in
        -h|--help)      usage; exit 0 ;;
        --cache-warm)   CACHE_WARM=1; shift; continue ;;
        --)             shift; GUNICORN_ARGS+=("$@"); break ;;
        --host)         var=HOST ;;
        --port)         var=PORT ;;
        --workers)      var=WORKERS ;;
        --threads)      var=SCHOLAR_THREADS ;;
        --index-dir)    var=INDEX_DIR ;;
        --data-dir)     var=ORIGINAL_DATA_PATH ;;
        --doi-map)      var=DOI_MAP_PATH ;;
        --keys-file)    var=SCHOLAR_API_KEYS_FILE ;;
        --title-weight) var=SCHOLAR_TITLE_WEIGHT ;;
        --log-level)    var=LOG_LEVEL ;;
        *)              GUNICORN_ARGS+=("$1"); shift; continue ;;
    esac
    if [ "$has_val" = 0 ]; then
        [ $# -ge 2 ] || fail "$opt needs a value (see ./start.sh --help)"
        val="$2"; shift
    fi
    shift
    export "$var=$val"
done

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
    PY_CMD=(uv run --frozen --no-dev python)
    PY_DESC="uv ($("${PY_CMD[@]}" -c 'import sys; print(sys.executable)'))"
else
    PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
    [ -x "$PYTHON" ] || fail "uv not found and no interpreter at $PYTHON. Install uv (https://docs.astral.sh/uv/) or set PYTHON."
    PY_CMD=("$PYTHON")
    PY_DESC="$PYTHON (uv not installed)"
fi
command -v java >/dev/null 2>&1 || fail "Java not found. Pyserini needs Java 21 (see README.md)."
"${PY_CMD[@]}" -c "import fastapi, gunicorn, uvicorn_worker, ujson, orjson, pyserini, Stemmer" 2>/dev/null \
    || fail "Missing Python packages. Run: uv sync"

# Resolve the paths api.py will load (same defaults as api.py), so a missing
# index fails here rather than as a stack trace from the server's startup hook.
INDEX_DIR="${INDEX_DIR:-${FULL_TEXT_INDEX_DIR:-$REPO_ROOT/data/s2orc_index}}"
ORIGINAL_DATA_PATH="${ORIGINAL_DATA_PATH:-$REPO_ROOT/data/s2orc_papers}"
DOI_MAP_PATH="${DOI_MAP_PATH:-$REPO_ROOT/data/doi_map.sqlite}"
[ -d "$INDEX_DIR" ] || fail "INDEX_DIR not found: $INDEX_DIR (build the index or set INDEX_DIR)"
[ -d "$ORIGINAL_DATA_PATH" ] || fail "ORIGINAL_DATA_PATH not found: $ORIGINAL_DATA_PATH (set ORIGINAL_DATA_PATH)"
printf '%-20s %s\n' "Python" "$PY_DESC"
printf '%-20s %s\n' "INDEX_DIR" "$INDEX_DIR"
printf '%-20s %s\n' "ORIGINAL_DATA_PATH" "$ORIGINAL_DATA_PATH"
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

export HOST PORT WORKERS INDEX_DIR ORIGINAL_DATA_PATH DOI_MAP_PATH
export PYTHONUNBUFFERED=1   # worker log lines appear immediately

if [ "$CACHE_WARM" = 1 ]; then
    # Detached (subshell + nohup) so it is not a child of the gunicorn master.
    # It only reads, at idle I/O priority, so serving can start right away.
    ( nohup "$APP_DIR/cache_warm.sh" > "$REPO_ROOT/cache_warm.log" 2>&1 & )
    echo "Cache warm:          running in background, log: $REPO_ROOT/cache_warm.log"
fi
# A command array, not a function: exec only runs programs.
exec "${PY_CMD[@]}" -m gunicorn -c "$APP_DIR/gunicorn.conf.py" api:app ${GUNICORN_ARGS[@]+"${GUNICORN_ARGS[@]}"}
