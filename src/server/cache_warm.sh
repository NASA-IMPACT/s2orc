#!/usr/bin/env bash
# Pull the search index (and the DOI map) into the OS page cache so that
# queries never wait on disk.
#
# Lucene reads the index through memory-mapped files; a page is served from
# RAM once the kernel has it cached, and from disk (~125 MB/s on a typical
# cloud volume) otherwise. Reading every index file once makes the whole
# index resident. A ~60 GB index takes ~8 min cold; re-running it when most
# pages are already cached takes seconds.
#
# Safe to run at any time, including while the API is serving: it only reads.
# It runs at the lowest I/O and CPU priority so live requests are not starved.
# Pages stay cached until something else needs the memory (e.g. a 1 TB
# conversion or index build), so re-run this after such jobs or after a reboot.
#
# Paths come from the same environment variables as api.py / start.sh:
#   INDEX_DIR      (default: <repo>/data/s2orc_index; FULL_TEXT_INDEX_DIR also accepted)
#   DOI_MAP_PATH   (default: <repo>/data/doi_map.sqlite)
#
# Usage:
#   src/server/cache_warm.sh                       # warm the index api.py serves + DOI map
#   src/server/cache_warm.sh /path/to/other/index  # warm a specific directory
#   nohup src/server/cache_warm.sh > cache_warm.log 2>&1 &    # detached

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$APP_DIR/../.." && pwd)"

if [ $# -ge 1 ]; then
    TARGETS=("$@")
else
    INDEX="${INDEX_DIR:-${FULL_TEXT_INDEX_DIR:-$REPO_ROOT/data/s2orc_index}}"
    DOI_MAP="${DOI_MAP_PATH:-$REPO_ROOT/data/doi_map.sqlite}"
    [ -d "$INDEX" ] || { echo "ERROR: index not found: $INDEX (set INDEX_DIR)" >&2; exit 1; }
    TARGETS=("$INDEX")
    [ -f "$DOI_MAP" ] && TARGETS+=("$DOI_MAP")
fi

cache_gb() { free -g | awk 'NR==2 {print $6}'; }

echo "page cache before: $(cache_gb) GB"
start=$(date +%s)
for t in "${TARGETS[@]}"; do
    size=$(du -sh "$t" | cut -f1)
    echo "$(date '+%H:%M:%S') reading $t ($size)"
    t0=$(date +%s)
    # nice/ionice: lowest CPU and I/O priority; cat streams every byte through
    # the page cache and discards it.
    if [ -d "$t" ]; then
        find "$t" -type f -print0 | nice -n 19 ionice -c 3 xargs -0 cat > /dev/null
    else
        nice -n 19 ionice -c 3 cat "$t" > /dev/null
    fi
    echo "$(date '+%H:%M:%S') done in $(( $(date +%s) - t0 )) s"
done
echo "page cache after:  $(cache_gb) GB  (total $(( $(date +%s) - start )) s)"
