#!/usr/bin/env python3
"""
Repack the raw S2ORC records for serving from S3 (docs/plans/02_raw_data_on_s3.md).

Each record (one JSONL line) is compressed as its own zstd frame and the frames
of one source file are concatenated into one S3 object, s3://BUCKET/PREFIX<name>.zst.
Any single record can then be read with one S3 range GET and decompressed alone.
The Lucene doc IDs ({filename}___{offset}__{version}) and the DOI map keep
pointing at uncompressed offsets; a SQLite table maps those to compressed bytes:

    table meta(key TEXT PRIMARY KEY, value TEXT)          -- bucket, prefix, level
    table file(id INTEGER PRIMARY KEY, name TEXT UNIQUE)  -- source filename = S3 key suffix
    table record(file_id INTEGER, offset INTEGER, start INTEGER, length INTEGER,
                 PRIMARY KEY (file_id, offset)) WITHOUT ROWID

Work happens in --work-dir: each file is packed to a local .zst, uploaded,
checked by size, then the .zst is deleted and its (offset, start, length) rows are
kept as a .map file. A rerun skips files with a .map, so the job can be resumed.
The SQLite table is built from the .map files at the end.

Usage (from the repo root):
    export RAW_S3_BUCKET=<bucket>                                   # or --bucket <bucket>
    python src/server/repack_raw_to_s3.py --limit 1                 # first file (test)
    python src/server/repack_raw_to_s3.py --workers 28              # all files (~45 min at 1,000 MB/s disk)
    python src/server/repack_raw_to_s3.py --map-only                # rebuild SQLite from .map files

The map then goes next to the index on S3 so new servers can fetch it with
src/server/download_from_s3.sh:
    aws s3 cp data/raw_s3_map.sqlite s3://<bucket>/s2orc_index_v2/raw_s3_map.sqlite
"""

import argparse
import os
import sqlite3
import sys
import time
from array import array
from multiprocessing import Pool
from pathlib import Path
from typing import Tuple

import boto3
import zstandard

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_PATH = Path(os.environ.get("ORIGINAL_DATA_PATH") or REPO_ROOT / "data" / "s2orc_papers")
DEFAULT_OUT = Path(os.environ.get("RAW_S3_MAP_PATH") or REPO_ROOT / "data" / "raw_s3_map.sqlite")
DEFAULT_BUCKET = os.environ.get("RAW_S3_BUCKET")
DEFAULT_PREFIX = "s2orc_raw_zstd/"
LEVEL = 9  # per-record ratio on a sample: level 3 2.94x, level 9 3.14x, whole-file gzip 3.18x


def pack_records(src: Path, zst: Path, level: int = LEVEL) -> Tuple[array, int, int]:
    """Write src's lines to zst, one zstd frame each. Returns ((offset, start, length) triples, raw bytes, packed bytes)."""
    cctx = zstandard.ZstdCompressor(level=level)
    rows = array("q")
    offset = start = 0
    with open(src, "rb") as f, open(zst, "wb") as out:
        for line in f:
            frame = cctx.compress(line)
            out.write(frame)
            rows.extend((offset, start, len(frame)))
            offset += len(line)
            start += len(frame)
    return rows, offset, start


def pack_file(job):
    """Pack, upload and verify one source file; write its .map. Returns (name, records, raw, packed, seconds)."""
    src, work_dir, bucket, prefix, level = job
    t0 = time.perf_counter()
    zst = work_dir / (src.name + ".zst")
    rows, offset, start = pack_records(src, zst, level)

    key = prefix + zst.name
    s3 = boto3.client("s3")
    s3.upload_file(str(zst), bucket, key)
    remote = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    if remote != start:
        raise RuntimeError(f"{key}: S3 size {remote} != local {start}")
    zst.unlink()

    tmp = work_dir / (src.name + ".map.tmp")
    with open(tmp, "wb") as f:
        rows.tofile(f)
    tmp.rename(work_dir / (src.name + ".map"))
    return src.name, len(rows) // 3, offset, start, time.perf_counter() - t0


def build_map(work_dir: Path, out: Path, bucket: str, prefix: str, level: int) -> None:
    maps = sorted(work_dir.glob("*.map"))
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE file(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE record(file_id INTEGER, offset INTEGER, start INTEGER, length INTEGER,
                            PRIMARY KEY (file_id, offset)) WITHOUT ROWID;
    """)
    db.executemany("INSERT INTO meta VALUES (?, ?)",
                   [("bucket", bucket), ("prefix", prefix), ("level", str(level))])
    total = 0
    for file_id, m in enumerate(maps):
        name = m.name[: -len(".map")]
        rows = array("q")
        rows.frombytes(m.read_bytes())
        db.execute("INSERT INTO file VALUES (?, ?)", (file_id, name))
        db.executemany("INSERT INTO record VALUES (?, ?, ?, ?)",
                       ((file_id, rows[i], rows[i + 1], rows[i + 2]) for i in range(0, len(rows), 3)))
        total += len(rows) // 3
    db.commit()
    db.close()
    tmp.rename(out)
    print(f"map: {len(maps)} files, {total:,} records -> {out} ({out.stat().st_size / 1e9:.2f} GB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-path", type=Path, default=DEFAULT_DATA_PATH)
    ap.add_argument("--work-dir", type=Path, default=REPO_ROOT / "data" / "raw_s3_repack")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="SQLite map (default $RAW_S3_MAP_PATH)")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET, help="S3 bucket (default $RAW_S3_BUCKET; required)")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--level", type=int, default=LEVEL)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=None, help="only the first N files (testing)")
    ap.add_argument("--map-only", action="store_true", help="skip packing, build SQLite from .map files")
    args = ap.parse_args()

    if not args.bucket:
        sys.exit("ERROR: no bucket (use --bucket or RAW_S3_BUCKET)")
    if not args.data_path.is_dir():
        sys.exit(f"ERROR: data directory not found: {args.data_path} (use --data-path or ORIGINAL_DATA_PATH)")
    args.work_dir.mkdir(parents=True, exist_ok=True)

    if not args.map_only:
        files = sorted(p for p in args.data_path.iterdir() if p.is_file())[: args.limit]
        todo = [p for p in files if not (args.work_dir / (p.name + ".map")).exists()]
        print(f"{len(files)} files, {len(files) - len(todo)} already done, {len(todo)} to pack "
              f"-> s3://{args.bucket}/{args.prefix} (zstd {args.level}, {args.workers} workers)", flush=True)
        t0 = time.perf_counter()
        raw_total = packed_total = 0
        jobs = [(p, args.work_dir, args.bucket, args.prefix, args.level) for p in todo]
        with Pool(args.workers) as pool:
            for i, (name, n, raw, packed, secs) in enumerate(pool.imap_unordered(pack_file, jobs), 1):
                raw_total += raw
                packed_total += packed
                elapsed = time.perf_counter() - t0
                print(f"[{i}/{len(todo)}] {name}  {n:,} recs  {raw / 1e9:.2f} GB -> {packed / 1e9:.2f} GB "
                      f"({raw / max(packed, 1):.2f}x) in {secs:.0f}s | total {raw_total / 1e9:.0f} GB -> "
                      f"{packed_total / 1e9:.0f} GB, {raw_total / 1e6 / elapsed:.0f} MB/s", flush=True)

    build_map(args.work_dir, args.out, args.bucket, args.prefix, args.level)


if __name__ == "__main__":
    main()
