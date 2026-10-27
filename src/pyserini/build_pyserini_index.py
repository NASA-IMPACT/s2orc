#!/usr/bin/env python3

import subprocess
import sys
import argparse
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Build Pyserini (Lucene) index from converted JSONL files")
    parser.add_argument("--input-path", default="data/s2orc_pyserini_text", help="Path to converted JSONL files (default: data/s2orc_pyserini_text)")
    parser.add_argument("--index-path", default="data/s2orc_index", help="Output path for Pyserini index (default: data/s2orc_index)")
    parser.add_argument("--threads", type=int, default=20, help="Number of threads for indexing (default: 20)")
    parser.add_argument("--fields", nargs="*", default=[], help="Extra JSON keys to index as separate Lucene fields besides 'contents', e.g. --fields title for the merged text+title collection")
    parser.add_argument("--positions", action="store_true", help="Store term positions (needed for phrase queries; index grows ~4x)")
    return parser.parse_args()


def build_index(jsonl_dir, index_dir, threads, fields=(), store_positions=False):
    print("="*70)
    print("PYSERINI INDEX BUILDER")
    print("="*70)
    print(f"Input Directory: {jsonl_dir}")
    print(f"Index Directory: {index_dir}")
    print(f"Threads: {threads}")
    print(f"Extra fields: {list(fields) or 'none'}")
    print(f"Store positions: {store_positions}")
    print("="*70)

    if not jsonl_dir.exists():
        print(f"ERROR: Input directory does not exist: {jsonl_dir}")
        sys.exit(1)

    print("\nChecking file extensions...")
    files_without_extension = [f for f in jsonl_dir.glob("*") if f.is_file() and not f.suffix]

    if files_without_extension:
        print(f"Found {len(files_without_extension)} files without .jsonl extension")
        print("Adding .jsonl extension to files...")

        for file_path in files_without_extension:
            new_path = file_path.with_suffix('.jsonl')
            file_path.rename(new_path)

        print("Done adding .jsonl extensions")
    else:
        print("All files already have extensions")

    jsonl_files = list(jsonl_dir.glob("*.jsonl"))
    print(f"\nFound {len(jsonl_files)} JSONL files to index")

    if not jsonl_files:
        print(f"ERROR: No JSONL files found in {jsonl_dir}")
        sys.exit(1)

    index_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "pyserini.index.lucene",
        "--collection", "JsonCollection",
        "--input", str(jsonl_dir),
        "--index", str(index_dir),
        "--generator", "DefaultLuceneDocumentGenerator",
        "--threads", str(threads),
    ]
    if store_positions:
        cmd.append("--storePositions")
    if fields:
        cmd.extend(["--fields", *fields])

    print("\nRunning Pyserini indexing command:")
    print(" ".join(cmd))
    print("\n" + "="*70)

    try:
        result = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        print(result.stdout)

    except subprocess.CalledProcessError as e:
        print(f"\nERROR: Indexing failed with exit code {e.returncode}")
        print(e.stdout)
        sys.exit(1)

    print("\n" + "="*70)
    print("INDEXING COMPLETE!")
    print("="*70)
    print(f"Index saved to: {index_dir}")
    print("\nTo search the index:")
    print(f"  from pyserini.search.lucene import LuceneSearcher")
    print(f"  searcher = LuceneSearcher('{index_dir}')")
    print(f"  hits = searcher.search('your query', k=10)")
    print(f"  for hit in hits:")
    print(f"      print(hit.docid, hit.score)")

    try:
        result = subprocess.run(
            ["du", "-sh", str(index_dir)],
            capture_output=True,
            text=True
        )
        print(f"\nIndex size on disk: {result.stdout.split()[0]}")
    except Exception as e:
        print(f"Could not determine index size: {e}")


if __name__ == "__main__":
    args = parse_args()
    build_index(Path(args.input_path), Path(args.index_path), args.threads, args.fields, args.positions)
