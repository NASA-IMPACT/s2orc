#!/usr/bin/env python3
"""
Build the DOI lookup table used by the /doi endpoint.

Scans every record in the original S2ORC data directory and writes a SQLite
file (default data/doi_map.sqlite, or $DOI_MAP_PATH):

    table doi(doi TEXT PRIMARY KEY, filename TEXT, offset INTEGER)

No JSON parsing: the DOI field sits in the first few hundred bytes of each
record (`"externalids":{...,"doi":"..."}` in v2, `"openaccessinfo":{...,
"externalids":{...,"DOI":"..."}}` in v1), so a regex over the line prefix is
enough and the job is I/O-bound. When a classification file is given, v2
files are processed first so that a paper present in both formats keeps its
v2 record (richer metadata).

Usage (from the repo root):
    python src/server/build_doi_map.py                      # all files, ~4-5 h for 1.8 TB (disk-bound)
    python src/server/build_doi_map.py --limit 2            # first 2 files (test)
    python src/server/build_doi_map.py --data-path data/s2orc_papers --out data/doi_map.sqlite
    python src/server/build_doi_map.py --classification docs/dataset_classification.json
"""

import argparse
import os
import re
import sqlite3
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import ujson as json

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_PATH = Path(os.environ.get("ORIGINAL_DATA_PATH") or REPO_ROOT / "data" / "s2orc_papers")
# Imported by api.py: where the /doi endpoint looks for the map.
DOI_MAP_PATH = Path(os.environ.get("DOI_MAP_PATH") or REPO_ROOT / "data" / "doi_map.sqlite")
WORKERS = 12
HEAD_BYTES = 8192  # the DOI field is within this many bytes of the record start

# Unescaped "doi": "..." (case-insensitive: v1 uses "DOI").
DOI_RE = re.compile(rb'(?<!\\)"doi":\s*"([^"\\]{1,300})"', re.IGNORECASE)

DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:")


def normalize_doi(doi: str) -> str:
    """Lowercase, strip resolver prefixes and surrounding junk. DOIs are case-insensitive."""
    d = doi.strip().lower()
    for p in DOI_PREFIXES:
        if d.startswith(p):
            d = d[len(p):]
            break
    return d.strip(" /")


def scan_file(args_tuple):
    """(filename, [(doi, offset), ...], records_seen)"""
    data_path, filename = args_tuple
    rows = []
    offset = 0
    seen = 0
    try:
        with open(Path(data_path) / filename, "rb", buffering=8 * 1024 * 1024) as f:
            for line in f:
                if len(line) > 1:
                    seen += 1
                    m = DOI_RE.search(line, 0, HEAD_BYTES)
                    if m:
                        doi = normalize_doi(m.group(1).decode("utf-8", "replace"))
                        if doi:
                            rows.append((doi, offset))
                offset += len(line)
    except Exception as e:  # keep going; report at the end
        print(f"ERROR {filename}: {e}", file=sys.stderr, flush=True)
    return filename, rows, seen


def file_passes(data_path: Path, classification_file):
    """
    Data files grouped into passes: [v2 files, v1 files, unclassified].
    Earlier passes take precedence for duplicate DOIs. Without a
    classification file ({"v1": [names], "v2": [names]}) there is one pass.
    """
    present = {p.name for p in data_path.iterdir() if p.is_file()}
    if not classification_file or not Path(classification_file).exists():
        return [sorted(present)]
    with open(classification_file) as f:
        cls = json.load(f)

    def matching(names):
        out = []
        for n in names:
            base = n.removesuffix(".jsonl")
            for candidate in (n, base, base + ".jsonl"):
                if candidate in present and candidate not in out:
                    out.append(candidate)
                    break
        return out

    v2 = matching(cls.get("v2", []))
    v1 = matching(cls.get("v1", []))
    rest = sorted(present - set(v2) - set(v1))
    return [p for p in (v2, v1, rest) if p]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-path", type=Path, default=DEFAULT_DATA_PATH,
                    help=f"Directory with the decompressed S2ORC .jsonl files (default: {DEFAULT_DATA_PATH})")
    ap.add_argument("--classification", type=Path, default=None,
                    help='Optional JSON {"v1": [file names], "v2": [file names]}; v2 files are scanned first')
    ap.add_argument("--limit", type=int, default=None, help="only scan the first N files of each pass (testing)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--out", type=Path, default=DOI_MAP_PATH, help=f"Output SQLite file (default: {DOI_MAP_PATH})")
    args = ap.parse_args()

    if not args.data_path.is_dir():
        sys.exit(f"ERROR: data directory not found: {args.data_path} (use --data-path or ORIGINAL_DATA_PATH)")

    passes = file_passes(args.data_path, args.classification)
    if args.limit:
        passes = [p[: args.limit] for p in passes]
    n_files = sum(len(p) for p in passes)
    print(f"Scanning {n_files} files in {len(passes)} passes with {args.workers} workers -> {args.out}", flush=True)

    tmp = args.out.with_suffix(".sqlite.building")
    tmp.unlink(missing_ok=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA cache_size=-512000;
        CREATE TABLE doi (doi TEXT PRIMARY KEY, filename TEXT NOT NULL, offset INTEGER NOT NULL) WITHOUT ROWID;
        """
    )

    t0 = time.time()
    total_rows = total_seen = done = 0
    with Pool(args.workers) as pool:
        # Passes run one after another so every v2 row is inserted before any
        # v1 row; INSERT OR IGNORE then keeps the v2 record for duplicate DOIs.
        for files in passes:
            jobs = [(str(args.data_path), name) for name in files]
            for filename, rows, seen in pool.imap_unordered(scan_file, jobs):
                db.executemany(
                    "INSERT OR IGNORE INTO doi(doi, filename, offset) VALUES (?, ?, ?)",
                    ((doi, filename, off) for doi, off in rows),
                )
                total_rows += len(rows)
                total_seen += seen
                done += 1
                if done % 10 == 0 or done == n_files:
                    el = time.time() - t0
                    print(f"[{done}/{n_files}] {total_rows:,} DOIs from {total_seen:,} records, {el/60:.1f} min", flush=True)
            db.commit()
    db.commit()
    db.close()
    tmp.replace(args.out)
    print(f"Done: {total_rows:,} DOIs ({total_seen:,} records) in {(time.time()-t0)/60:.1f} min -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
