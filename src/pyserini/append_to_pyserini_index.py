#!/usr/bin/env python3
"""
Script to append new documents to an existing Pyserini Lucene index.
"""
import argparse
import sys
from pathlib import Path
from pyserini.index.lucene import LuceneIndexer

def main():
    parser = argparse.ArgumentParser(
        description='Append new documents to an existing Pyserini Lucene index.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        '--index',
        type=str,
        required=True,
        help='Path to the existing index directory'
    )

    parser.add_argument(
        '--input',
        type=str,
        required=True,
        help='Path to the new data file in JSONL format'
    )

    parser.add_argument(
        '--threads',
        type=int,
        default=1,
        help='Number of threads to use for indexing'
    )

    args = parser.parse_args()

    index_path = Path(args.index)
    if not index_path.exists():
        print(f"Error: Index directory does not exist: {args.index}", file=sys.stderr)
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file does not exist: {args.input}", file=sys.stderr)
        sys.exit(1)

    if not input_path.suffix == '.jsonl':
        print(f"Warning: Input file does not have .jsonl extension: {args.input}", file=sys.stderr)

    print(f"Appending documents to index: {args.index}")
    print(f"Reading new data from: {args.input}")
    print(f"Using {args.threads} thread(s)")

    try:
        indexer = LuceneIndexer(args.index, append=True, threads=args.threads)
        print("Adding documents to index...")
        indexer.add_batch_jsonl(args.input)
        indexer.close()
        print("New data successfully appended to the index.")
    except Exception as e:
        print(f"Error during indexing: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
