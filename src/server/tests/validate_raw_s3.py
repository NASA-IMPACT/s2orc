#!/usr/bin/env python3
"""
Check that the S3 copy of the raw records (repack_raw_to_s3.py) serves exactly
what the local corpus served.

Checks:
    coverage  every doc ID in the Lucene index and every row of the DOI map has
              an entry in the S3 map (all 27.65M docs, not a sample)
    bytes     for random doc IDs and DOIs, the record read from S3 (range GET +
              zstd) is byte-identical to the line at that offset in the local file
    servers   the same /search, /fulltext and /doi requests against a server on
              local disk and one on S3 return identical JSON (took_ms ignored)

Usage (from the repo root):
    python src/server/tests/validate_raw_s3.py coverage
    python src/server/tests/validate_raw_s3.py bytes --n 5000
    python src/server/tests/validate_raw_s3.py servers --local http://127.0.0.1:8002 --s3 http://127.0.0.1:8001
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stress_test import DEFAULT_KEY_FILE, TERMS, load_key, sample_dois  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
INDEX_DIR = os.environ.get("INDEX_DIR") or str(REPO / "data" / "s2orc_index")
DATA_PATH = Path(os.environ.get("ORIGINAL_DATA_PATH") or REPO / "data" / "s2orc_papers")
RAW_MAP = os.environ.get("RAW_S3_MAP_PATH") or str(REPO / "data" / "raw_s3_map.sqlite")
DOI_MAP = os.environ.get("DOI_MAP_PATH") or str(REPO / "data" / "doi_map.sqlite")


def open_map():
    db = sqlite3.connect(f"file:{RAW_MAP}?mode=ro", uri=True, check_same_thread=False)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    files = {name: fid for fid, name in db.execute("SELECT id, name FROM file")}
    return db, meta, files


def map_offsets(db):
    """file_id -> sorted numpy array of record offsets."""
    out = {}
    for fid, in db.execute("SELECT id FROM file"):
        out[fid] = np.fromiter((o for o, in db.execute("SELECT offset FROM record WHERE file_id = ?", (fid,))), dtype=np.int64)
    return out


def _docids(bounds):
    from pyserini.search.lucene import LuceneSearcher
    lo, hi = bounds
    s = LuceneSearcher(INDEX_DIR)
    out = []
    for i in range(lo, hi):
        name, rest = s.doc(i).docid().split("___")
        out.append((name, int(rest.split("__")[0])))
    return out


def check_coverage(args) -> bool:
    db, meta, files = open_map()
    offsets = map_offsets(db)
    print(f"map: {len(files)} files, {sum(len(v) for v in offsets.values()):,} records")
    local_files = {p.name for p in DATA_PATH.iterdir() if p.is_file()}
    print(f"local corpus: {len(local_files)} files; in map: {len(local_files & set(files))}; "
          f"missing from map: {len(local_files - set(files))}")

    from pyserini.search.lucene import LuceneSearcher
    n = LuceneSearcher(INDEX_DIR).num_docs
    step = n // args.procs + 1
    t0 = time.perf_counter()
    missing = checked = 0
    # spawn, not fork: this process already runs a JVM (num_docs above), and a forked JVM hangs.
    with multiprocessing.get_context("spawn").Pool(args.procs) as pool:
        for part in pool.imap_unordered(_docids, [(lo, min(n, lo + step)) for lo in range(0, n, step)]):
            by_file = {}
            for name, off in part:
                by_file.setdefault(name, []).append(off)
            for name, offs in by_file.items():
                fid = files.get(name)
                if fid is None:
                    missing += len(offs)
                    continue
                missing += int((~np.isin(np.array(offs, dtype=np.int64), offsets[fid])).sum())
            checked += len(part)
            print(f"  index doc IDs checked {checked:,}/{n:,}, missing {missing:,} ({time.perf_counter() - t0:.0f}s)", flush=True)
    print(f"index: {checked:,} doc IDs, {missing:,} without an S3 map entry")

    doi = sqlite3.connect(f"file:{DOI_MAP}?mode=ro", uri=True)
    doi_rows = doi_missing = 0
    by_file = {}
    for name, off in doi.execute("SELECT filename, offset FROM doi"):
        by_file.setdefault(name, []).append(off)
        doi_rows += 1
    for name, offs in by_file.items():
        fid = files.get(name)
        if fid is None:
            doi_missing += len(offs)
        else:
            doi_missing += int((~np.isin(np.array(offs, dtype=np.int64), offsets[fid])).sum())
    print(f"doi map: {doi_rows:,} rows, {doi_missing:,} without an S3 map entry")
    return missing == 0 and doi_missing == 0 and local_files <= set(files)


def check_bytes(args) -> bool:
    import boto3
    import zstandard
    from botocore.config import Config
    from pyserini.search.lucene import LuceneSearcher

    db, meta, files = open_map()
    rng = random.Random(args.seed)
    s = LuceneSearcher(INDEX_DIR)
    targets = []
    for i in rng.sample(range(s.num_docs), args.n):
        name, rest = s.doc(i).docid().split("___")
        targets.append(("index", name, int(rest.split("__")[0])))
    doi = sqlite3.connect(f"file:{DOI_MAP}?mode=ro", uri=True)
    for d in sample_dois(args.n // 2, args.seed):
        name, off = doi.execute("SELECT filename, offset FROM doi WHERE doi = ?", (d,)).fetchone()
        targets.append(("doi", name, off))

    s3 = boto3.client("s3", config=Config(max_pool_connections=32))
    rows = {}
    for kind, name, off in targets:
        rows[(name, off)] = db.execute("SELECT start, length FROM record WHERE file_id = ? AND offset = ?",
                                       (files[name], off)).fetchone()

    def one(t):
        kind, name, off = t
        with open(DATA_PATH / name, "rb") as f:
            f.seek(off)
            local = f.readline()
        start, length = rows[(name, off)]
        body = s3.get_object(Bucket=meta["bucket"], Key=f"{meta['prefix']}{name}.zst",
                             Range=f"bytes={start}-{start + length - 1}")["Body"].read()
        remote = zstandard.ZstdDecompressor().decompress(body)
        json.loads(remote)
        return kind, local == remote, len(local)

    bad = {"index": 0, "doi": 0}
    total = {"index": 0, "doi": 0}
    with ThreadPoolExecutor(32) as pool:
        for kind, same, _ in pool.map(one, targets):
            total[kind] += 1
            bad[kind] += not same
    for kind in total:
        print(f"bytes: {total[kind]:,} records via {kind}, {bad[kind]} differ from the local file")
    return not any(bad.values())


def check_servers(args) -> bool:
    headers = {"Authorization": f"Bearer {load_key(args)}"}
    rng = random.Random(args.seed)
    terms = [t.strip() for t in Path(args.terms_file).read_text().splitlines() if t.strip()] if args.terms_file else TERMS
    queries = [" ".join(rng.sample(terms, 3)) for _ in range(args.n)]
    paths = []
    for q in queries:
        qq = urllib.parse.quote(q)
        paths.append(f"/search?q={qq}&top_k=10")
        paths.append(f"/fulltext?q={qq}&top_k=10")
    paths += [f"/doi/{urllib.parse.quote(d, safe='/')}" for d in sample_dois(args.n, args.seed)]

    def get(base, path):
        req = urllib.request.Request(base + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.loads(r.read())
                status = r.status
        except urllib.error.HTTPError as e:
            body, status = e.read().decode()[:200], e.code
        if isinstance(body, dict):
            body.pop("took_ms", None)
        return status, body

    def compare(path):
        return path, get(args.local, path), get(args.s3, path)

    diff = {}
    counts = {}
    with ThreadPoolExecutor(8) as pool:
        for path, a, b in pool.map(compare, paths):
            ep = path.split("?")[0].split("/")[1]
            counts[ep] = counts.get(ep, 0) + 1
            if a != b:
                diff.setdefault(ep, []).append(path)
    for ep, n in counts.items():
        bad = diff.get(ep, [])
        print(f"servers: /{ep} {n} requests, {len(bad)} differ" + (f" (e.g. {bad[0]})" if bad else ""))
    return not diff


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=["coverage", "bytes", "servers"])
    ap.add_argument("--n", type=int, default=2000, help="bytes: random doc IDs (and n/2 DOIs); servers: queries and DOIs")
    ap.add_argument("--procs", type=int, default=16, help="coverage: processes reading doc IDs from the index")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--local", default="http://127.0.0.1:8002", help="servers: server reading the local corpus")
    ap.add_argument("--s3", default="http://127.0.0.1:8001", help="servers: server reading from S3")
    ap.add_argument("--terms-file", help="servers: query terms, one per line (default: stress_test's 60 terms)")
    ap.add_argument("--key")
    ap.add_argument("--key-file", default=str(DEFAULT_KEY_FILE))
    args = ap.parse_args()
    ok = {"coverage": check_coverage, "bytes": check_bytes, "servers": check_servers}[args.check](args)
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
