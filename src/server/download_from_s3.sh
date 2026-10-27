#!/usr/bin/env bash
# Download the prebuilt serving files from S3 instead of building them (SETUP.md steps 2-6).
#
# Fetches, from S2ORC_S3_URI:
#   pyserini_index_v2/   Lucene index                   -> INDEX_DIR        (default data/s2orc_index)
#   doi_map.sqlite       DOI map for /doi                -> DOI_MAP_PATH     (default data/doi_map.sqlite)
#   raw_s3_map.sqlite    record -> S3 byte range map     -> RAW_S3_MAP_PATH  (default data/raw_s3_map.sqlite)
#
# With RAW_S3_MAP_PATH set, the API reads each result's raw record from S3
# (src/server/repack_raw_to_s3.py, docs/S3_RAW_DATA_BENCHMARK.md), so the
# 1.9 TB corpus is not needed on disk. About 62 GB in total. Reruns only fetch
# what is missing or changed (aws s3 sync).
#
# Needs the AWS CLI and credentials that can read the bucket. Run from anywhere:
#   S2ORC_S3_URI=s3://<bucket>/s2orc_index_v2 src/server/download_from_s3.sh
# then put the three paths it prints in .env and run ./start.sh.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -n "${S2ORC_S3_URI:-}" ] || fail "set S2ORC_S3_URI, e.g. S2ORC_S3_URI=s3://<bucket>/s2orc_index_v2"
command -v aws >/dev/null 2>&1 || fail "AWS CLI not found (https://docs.aws.amazon.com/cli/)"
URI="${S2ORC_S3_URI%/}"

INDEX_DIR="${INDEX_DIR:-$REPO_ROOT/data/s2orc_index}"
DOI_MAP_PATH="${DOI_MAP_PATH:-$REPO_ROOT/data/doi_map.sqlite}"
RAW_S3_MAP_PATH="${RAW_S3_MAP_PATH:-$REPO_ROOT/data/raw_s3_map.sqlite}"

aws s3 ls "$URI/" >/dev/null || fail "cannot list $URI/ (credentials? region? bucket name?)"

echo "index    $URI/pyserini_index_v2/ -> $INDEX_DIR"
mkdir -p "$INDEX_DIR"
aws s3 sync --only-show-errors "$URI/pyserini_index_v2/" "$INDEX_DIR/"

for pair in "doi_map.sqlite:$DOI_MAP_PATH" "raw_s3_map.sqlite:$RAW_S3_MAP_PATH"; do
    name="${pair%%:*}"; dest="${pair#*:}"
    echo "map      $URI/$name -> $dest"
    mkdir -p "$(dirname "$dest")"
    # sync on the parent prefix with --exclude/--include copies only this one file, and only if changed.
    aws s3 sync --only-show-errors "$URI/" "$(dirname "$dest")/" --exclude "*" --include "$name"
    [ "$(basename "$dest")" = "$name" ] || mv -f "$(dirname "$dest")/$name" "$dest"
done

echo
echo "Done. For .env (or the environment):"
echo "INDEX_DIR=$INDEX_DIR"
echo "DOI_MAP_PATH=$DOI_MAP_PATH"
echo "RAW_S3_MAP_PATH=$RAW_S3_MAP_PATH"
