#!/bin/bash

set -e

if [ $# -eq 0 ]; then
    echo "Usage: $0 <data_directory> [threads]"
    echo "  data_directory: Path to directory containing .gz files"
    echo "  threads: Number of threads for pigz (default: 30)"
    exit 1
fi

DATA_DIR="$1"
THREADS="${2:-30}"

if [ ! -d "$DATA_DIR" ]; then
    echo "Error: Directory '$DATA_DIR' does not exist"
    exit 1
fi

cd "$DATA_DIR"

if command -v pigz &> /dev/null; then
    DECOMPRESS_CMD="pigz -d -p $THREADS"
    echo "Using pigz with $THREADS threads per file"
else
    DECOMPRESS_CMD="gunzip"
    echo "Using gunzip (pigz not found)"
fi

total_files=$(find . -maxdepth 1 -name "*.gz" -type f | wc -l)
current=0

echo "Found $total_files .gz files to decompress"
echo "Processing one file at a time to avoid disk space issues..."
echo ""

find . -maxdepth 1 -name "*.gz" -type f | while read -r gzfile; do
    current=$((current + 1))
    outfile="${gzfile%.gz}"

    echo "[$current/$total_files] Processing: $gzfile"

    available_kb=$(df . | tail -1 | awk '{print $4}')
    available_gb=$((available_kb / 1024 / 1024))
    echo "  Available disk space: ${available_gb}GB"

    if [ $available_gb -lt 100 ]; then
        echo "  WARNING: Low disk space! Only ${available_gb}GB available"
    fi

    if $DECOMPRESS_CMD "$gzfile"; then
        if [ -f "$outfile" ] && [ -s "$outfile" ]; then
            jsonl_file="${outfile}.jsonl"
            mv "$outfile" "$jsonl_file"
            echo "  ✓ Successfully decompressed to: $jsonl_file"
        else
            echo "  ERROR: Decompressed file is empty or missing: $outfile"
            exit 1
        fi
    else
        echo "  ERROR: Failed to decompress: $gzfile"
        exit 1
    fi

    echo ""
done

echo "All files processed successfully!"
