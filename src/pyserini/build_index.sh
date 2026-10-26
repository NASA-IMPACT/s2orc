#!/usr/bin/env bash
# Build everything the API serves from decompressed S2ORC .jsonl files:
#
#   convert   full text and titles -> Pyserini documents      (convert_data_text.py, convert_data_title.py)
#   merge     one two-field collection {id, title, contents}  (convert_data_merge.py)
#   index     Lucene index, title as its own field, no positions (build_pyserini_index.py)
#   doi       DOI -> record map for /doi/{doi}                (src/server/build_doi_map.py)
#
# Downloading and decompressing come first and are separate (they need an API
# key and a lot of time); see src/s2orc/README.md:
#   python src/s2orc/download_data.py --api-key "$S2_API_KEY" --local-path "$DATA_DIR/s2orc_papers"
#   bash src/s2orc/decompress_sequential.sh "$DATA_DIR/s2orc_papers"
#
# Usage (from the repo root, or via `docker compose run --rm index ...`):
#   src/pyserini/build_index.sh                 # all four steps
#   src/pyserini/build_index.sh index doi       # only these steps
#   DATA_DIR=/mnt/s2orc src/pyserini/build_index.sh
#
# Environment (also read from .env at the repo root):
#   DATA_DIR            root for inputs and outputs            (default: ./data)
#   ORIGINAL_DATA_PATH  decompressed .jsonl files              (default: $DATA_DIR/s2orc_papers)
#   INDEX_DIR           where the index is written             (default: $DATA_DIR/s2orc_index)
#   DOI_MAP_PATH        where the DOI map is written           (default: $DATA_DIR/doi_map.sqlite)
#   CONVERT_WORKERS     processes for conversion and merge     (default: 8)
#   INDEX_THREADS       Lucene indexing threads                (default: 20)
#
# Sizes and durations for the full corpus (513 files, 1.8 TB): convert ~1 TB
# text + 5 GB titles, merge ~1 TB (4.5 h), index 60 GB (~3 h), DOI map 1.7 GB
# (~4 h). The converted collections can be deleted once the index exists.
# Each step can be re-run on its own; merge skips files already written.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"

# .env: KEY=VALUE lines, without overriding variables already set in the shell.
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        key="${line%%=*}"; value="${line#*=}"
        [ -z "${!key:-}" ] && export "$key=$value"
    done < .env
fi

DATA_DIR="${DATA_DIR:-$REPO_ROOT/data}"
ORIGINAL_DATA_PATH="${ORIGINAL_DATA_PATH:-$DATA_DIR/s2orc_papers}"
INDEX_DIR="${INDEX_DIR:-$DATA_DIR/s2orc_index}"
DOI_MAP_PATH="${DOI_MAP_PATH:-$DATA_DIR/doi_map.sqlite}"
TEXT_DIR="$DATA_DIR/s2orc_pyserini_text"
TITLE_DIR="$DATA_DIR/s2orc_pyserini_title"
MERGED_DIR="$DATA_DIR/s2orc_pyserini_text_title"
CONVERT_WORKERS="${CONVERT_WORKERS:-8}"
INDEX_THREADS="${INDEX_THREADS:-20}"

fail() { echo "ERROR: $*" >&2; exit 1; }

# Python via uv when available (uses pyproject.toml/uv.lock), else $PYTHON or a venv.
if command -v uv >/dev/null 2>&1; then
    uv sync --frozen --no-dev --quiet
    py() { uv run --frozen --no-dev python "$@"; }
elif [ -n "${PYTHON:-}" ]; then
    py() { "$PYTHON" "$@"; }
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    py() { "$REPO_ROOT/.venv/bin/python" "$@"; }
else
    fail "uv not found and no Python environment. Install uv (https://docs.astral.sh/uv/) or set PYTHON."
fi
command -v java >/dev/null 2>&1 || fail "Java not found. Pyserini needs Java 21 (see README.md)."

STEPS=("$@")
[ ${#STEPS[@]} -eq 0 ] && STEPS=(convert merge index doi)
for s in "${STEPS[@]}"; do
    case "$s" in convert|merge|index|doi) ;; *) fail "unknown step '$s' (convert, merge, index, doi)";; esac
done

echo "DATA_DIR            $DATA_DIR"
echo "ORIGINAL_DATA_PATH  $ORIGINAL_DATA_PATH"
echo "INDEX_DIR           $INDEX_DIR"
echo "DOI_MAP_PATH        $DOI_MAP_PATH"
echo "steps               ${STEPS[*]}"
echo

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

for step in "${STEPS[@]}"; do
    echo "== $(stamp) $step"
    case "$step" in
        convert)
            [ -d "$ORIGINAL_DATA_PATH" ] || fail "ORIGINAL_DATA_PATH not found: $ORIGINAL_DATA_PATH (download and decompress first)"
            ls "$ORIGINAL_DATA_PATH"/* >/dev/null 2>&1 || fail "no files in $ORIGINAL_DATA_PATH"
            py src/pyserini/convert_data_text.py  --input-path "$ORIGINAL_DATA_PATH" --output-path "$TEXT_DIR"  --num-workers "$CONVERT_WORKERS"
            py src/pyserini/convert_data_title.py --input-path "$ORIGINAL_DATA_PATH" --output-path "$TITLE_DIR" --num-workers "$CONVERT_WORKERS"
            ;;
        merge)
            [ -d "$TEXT_DIR" ] || fail "$TEXT_DIR not found (run the convert step)"
            py src/pyserini/convert_data_merge.py --text-path "$TEXT_DIR" --title-path "$TITLE_DIR" --output-path "$MERGED_DIR" --num-workers "$CONVERT_WORKERS"
            ;;
        index)
            [ -d "$MERGED_DIR" ] || fail "$MERGED_DIR not found (run the merge step)"
            py src/pyserini/build_pyserini_index.py --input-path "$MERGED_DIR" --index-path "$INDEX_DIR" --fields title --no-positions --threads "$INDEX_THREADS"
            ;;
        doi)
            [ -d "$ORIGINAL_DATA_PATH" ] || fail "ORIGINAL_DATA_PATH not found: $ORIGINAL_DATA_PATH"
            py src/server/build_doi_map.py --data-path "$ORIGINAL_DATA_PATH" --out "$DOI_MAP_PATH"
            ;;
    esac
done

echo
echo "== $(stamp) done"
echo "Serve with:  INDEX_DIR=$INDEX_DIR ORIGINAL_DATA_PATH=$ORIGINAL_DATA_PATH DOI_MAP_PATH=$DOI_MAP_PATH src/server/start.sh"
echo "(or put those three in .env and run src/server/start.sh / docker compose up -d api)"
