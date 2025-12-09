#!/bin/bash

set -e

if [ $# -eq 0 ]; then
    echo "Usage: $0 <data_directory> [parallel_jobs]"
    echo "  data_directory: Path to directory containing .gz files"
    echo "  parallel_jobs: Number of parallel jobs (default: 30)"
    exit 1
fi

DATA_DIR="$1"
PARALLEL_JOBS="${2:-30}"

if [ ! -d "$DATA_DIR" ]; then
    echo "Error: Directory '$DATA_DIR' does not exist"
    exit 1
fi

cd "$DATA_DIR"

decompress_and_delete() {
    local gzfile="$1"
    local outfile="${gzfile%.gz}"

    echo "Processing: $gzfile"

    if gunzip -c "$gzfile" > "$outfile"; then
        if [ -f "$outfile" ] && [ -s "$outfile" ]; then
            jsonl_file="${outfile}.jsonl"
            mv "$outfile" "$jsonl_file"
            rm -f "$gzfile"
            echo "Completed: $jsonl_file"
        else
            echo "ERROR: Decompressed file is empty or missing: $outfile"
            rm -f "$outfile"
            return 1
        fi
    else
        echo "ERROR: Failed to decompress: $gzfile"
        rm -f "$outfile"
        return 1
    fi
}

export -f decompress_and_delete

echo "Processing .gz files with $PARALLEL_JOBS parallel jobs..."

find . -maxdepth 1 -name "*.gz" -type f | \
    xargs -P "$PARALLEL_JOBS" -I {} bash -c 'decompress_and_delete "$@"' _ {}

echo "All files processed!"
