#!/usr/bin/env python3
"""
Example client for the scholar search API.

Usage:
    python api_client_example.py "machine learning" [top_k]
    SCHOLAR_API_KEY=key1 python api_client_example.py "insulin resistance" 5
    API_URL=http://host:8000 python api_client_example.py "graph neural network"
"""

import os
import sys

import requests

API_URL = os.environ.get("API_URL", "http://localhost:8000")
HEADERS = {"Authorization": f"Bearer {os.environ['SCHOLAR_API_KEY']}"} if os.environ.get("SCHOLAR_API_KEY") else {}


def search(query: str, top_k: int = 10) -> dict:
    """Snippet search: top papers with a passage around the query match."""
    r = requests.get(f"{API_URL}/search", params={"q": query, "top_k": top_k}, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.json()


def fulltext(query: str, top_k: int = 10, fields: str = "title,doi,authors") -> dict:
    """Full-document search with a chosen subset of fields."""
    r = requests.get(f"{API_URL}/fulltext", params={"q": query, "top_k": top_k, "fields": fields}, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.json()


def lookup_doi(doi: str) -> dict:
    """Exact lookup of one paper by DOI."""
    r = requests.get(f"{API_URL}/doi/{doi}", headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.json()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    query = sys.argv[1]
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    result = search(query, top_k)
    print(f"Query: {query!r}  ({result['took_ms']} ms)")
    print("=" * 80)
    for i, doc in enumerate(result["docs"], 1):
        print(f"{i}. [{doc['score']:.2f}] {doc['title'] or '(no title)'}")
        if doc["link"]:
            print(f"   {doc['link']}")
        print(f"   {doc['content']}")
        if len(doc["matches"]) > 1:
            print(f"   (+{len(doc['matches']) - 1} more passages shown, {doc['match_count']} occurrences in the paper)")
        print()

    first = next((d for d in result["docs"] if d["doi"]), None)
    if first:
        try:
            paper = lookup_doi(first["doi"])
            print(f"DOI lookup {first['doi']}: {paper['title'][:80]!r}, {len(paper.get('text') or ''):,} chars of text")
        except requests.HTTPError as e:
            print(f"DOI lookup {first['doi']}: {e.response.status_code} {e.response.text[:120]}")


if __name__ == "__main__":
    main()
