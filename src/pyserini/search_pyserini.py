#!/usr/bin/env python3

import ujson as json
import argparse
import sys
from pyserini.search.lucene import LuceneSearcher
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Search the Pyserini index")
    parser.add_argument("--index-path", default="data/s2orc_index", help="Path to Pyserini index (default: data/s2orc_index)")
    parser.add_argument("--data-path", default="data/s2orc_papers", help="Path to original S2ORC data files (default: data/s2orc_papers)")
    parser.add_argument("--query", help="Search query (if not provided, runs interactive mode)")
    parser.add_argument("-k", type=int, default=10, help="Number of results to return (default: 10)")
    parser.add_argument("--no-metadata", action="store_true", help="Skip retrieving full metadata")
    return parser.parse_args()


def read_json_at_offset(file_path, offset):
    with open(file_path, 'rb') as f:
        f.seek(offset)
        line = f.readline()
        if line:
            line_str = line.decode('utf-8').strip()
            if line_str:
                try:
                    return json.loads(line_str)
                except json.JSONDecodeError as e:
                    print(f"Error decoding JSON at offset {offset}: {e}")
                    return None
    return None


def retrieve_original_document(doc_id, data_path):
    parts = doc_id.split('___')
    if len(parts) != 2:
        raise ValueError(f"Invalid document ID format: {doc_id}")

    filename = parts[0]
    offset_version = parts[1].split('__')
    if len(offset_version) != 2:
        raise ValueError(f"Invalid document ID format: {doc_id}")

    offset = int(offset_version[0])

    file_path = Path(data_path) / filename

    if not file_path.exists():
        raise FileNotFoundError(f"Original data file not found: {file_path}")

    doc = read_json_at_offset(str(file_path), offset)

    if doc is None:
        raise ValueError(f"Could not read document at offset {offset} in {filename}")

    return doc


def search_and_display(searcher, data_path, query, k=10, show_full_metadata=True):
    print(f"\nQuery: '{query}'")
    print("="*70)

    hits = searcher.search(query, k=k)

    if not hits:
        print("No results found.")
        return

    print(f"Found {len(hits)} results:\n")

    for i, hit in enumerate(hits, 1):
        print(f"{i:2}. Score: {hit.score:.5f}")
        print(f"    Doc ID: {hit.docid}")

        if show_full_metadata:
            try:
                doc = retrieve_original_document(hit.docid, data_path)

                text = doc.get('content', {}).get('text', '')
                annotations = doc.get('content', {}).get('annotations', {})

                title = ''
                if annotations:
                    title_annotations_str = annotations.get('title')
                    if title_annotations_str:
                        title_annotations = json.loads(title_annotations_str) if isinstance(title_annotations_str, str) else title_annotations_str
                        if title_annotations and len(title_annotations) > 0:
                            start = title_annotations[0].get('start', 0)
                            end = title_annotations[0].get('end', 0)
                            title = text[start:end].strip()

                print(f"    Title: {title[:100]}{'...' if len(title) > 100 else ''}")
                print(f"    Corpus ID: {doc.get('corpusid')}")
                print(f"    DOI: {doc.get('externalids', {}).get('doi', 'N/A')}")
                print(f"    ArXiv: {doc.get('externalids', {}).get('arxiv', 'N/A')}")

            except Exception as e:
                print(f"    Warning: Could not retrieve full metadata: {e}")

        print()


def interactive_search(index_path, data_path):
    print("="*70)
    print("PYSERINI INTERACTIVE SEARCH")
    print("="*70)
    print(f"Index: {index_path}")
    print("\nEnter your queries (type 'quit' to exit)")
    print("="*70)

    searcher = LuceneSearcher(str(index_path))

    while True:
        try:
            query = input("\nQuery: ").strip()

            if query.lower() in ['quit', 'exit', 'q']:
                print("Goodbye!")
                break

            if not query:
                continue

            search_and_display(searcher, data_path, query, k=10, show_full_metadata=True)

        except KeyboardInterrupt:
            print("\n\nGoodbye!")
            break
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    args = parse_args()

    index_path = Path(args.index_path)
    data_path = Path(args.data_path)

    if not index_path.exists():
        print(f"ERROR: Index not found at {index_path}")
        print("Please run build_pyserini_index.py first to create the index.")
        sys.exit(1)

    if not data_path.exists():
        print(f"ERROR: Data path not found at {data_path}")
        sys.exit(1)

    if args.query:
        searcher = LuceneSearcher(str(index_path))
        search_and_display(searcher, str(data_path), args.query, k=args.k, show_full_metadata=not args.no_metadata)
    else:
        interactive_search(index_path, str(data_path))
