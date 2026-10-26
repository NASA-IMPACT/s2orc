#!/usr/bin/env python3
"""
Merge the converted text and title files into one Pyserini collection with
two fields per document:

    {"id": "<file>___<offset>__<version>", "title": "...", "contents": "<full text>"}

Input:  <text-path>/<file>.jsonl    (id, contents = full text, from convert_data_text.py)
        <title-path>/<file>.jsonl   (id, contents = title, from convert_data_title.py)
Output: <output-path>/<file>.jsonl

Index the output with `build_pyserini_index.py --fields title` so title and
body are separate Lucene fields that can be searched together with per-field
weights (see src/server/api.py).

Usage (from the repo root):
    python src/pyserini/convert_data_merge.py                # all files, 8 workers
    python src/pyserini/convert_data_merge.py --limit 1      # test on one file
"""

import argparse
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import ujson as json


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text-path", default="data/s2orc_pyserini_text", help="Converted full-text files (default: data/s2orc_pyserini_text)")
    parser.add_argument("--title-path", default="data/s2orc_pyserini_title", help="Converted title files (default: data/s2orc_pyserini_title)")
    parser.add_argument("--output-path", default="data/s2orc_pyserini_text_title", help="Output directory (default: data/s2orc_pyserini_text_title)")
    parser.add_argument("--num-workers", type=int, default=8, help="Number of parallel workers (default: 8)")
    parser.add_argument("--limit", type=int, default=None, help="Only merge the first N files (testing)")
    return parser.parse_args()


def merge_file(args_tuple):
    """(name, docs written, docs without a title, error)"""
    name, text_dir, title_dir, out_dir = args_tuple
    text_path, title_path, out_path = Path(text_dir) / name, Path(title_dir) / name, Path(out_dir) / name
    tmp_path = out_path.with_suffix(".jsonl.part")
    try:
        titles = {}
        if title_path.exists():
            with open(title_path, "rb", buffering=8 * 1024 * 1024) as f:
                for line in f:
                    if len(line) > 1:
                        d = json.loads(line)
                        titles[d["id"]] = d.get("contents") or ""
        written = missing = 0
        with open(text_path, "rb", buffering=8 * 1024 * 1024) as fin, \
             open(tmp_path, "w", buffering=8 * 1024 * 1024) as fout:
            for line in fin:
                if len(line) <= 1:
                    continue
                d = json.loads(line)
                title = titles.get(d["id"], "")
                if not title:
                    missing += 1
                fout.write(json.dumps({"id": d["id"], "title": title, "contents": d.get("contents") or ""}) + "\n")
                written += 1
        tmp_path.replace(out_path)
        return name, written, missing, None
    except Exception as e:
        return name, 0, 0, repr(e)


def main():
    args = parse_args()
    text_dir, title_dir, out_dir = Path(args.text_path), Path(args.title_path), Path(args.output_path)
    if not text_dir.is_dir():
        sys.exit(f"ERROR: text directory not found: {text_dir}")
    if not title_dir.is_dir():
        print(f"WARNING: title directory not found: {title_dir}; every document will get an empty title", file=sys.stderr)
    out_dir.mkdir(parents=True, exist_ok=True)

    names = sorted(p.name for p in text_dir.glob("*.jsonl"))
    # Resumable: skip files already fully written.
    todo = [n for n in names if not (out_dir / n).exists()]
    already = len(names) - len(todo)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(names)} text files, {already} already merged, {len(todo)} to do, {args.num_workers} workers -> {out_dir}", flush=True)

    jobs = [(n, str(text_dir), str(title_dir), str(out_dir)) for n in todo]
    t0 = time.time()
    total = total_missing = errors = 0
    with Pool(args.num_workers) as pool:
        for i, (name, written, missing, err) in enumerate(pool.imap_unordered(merge_file, jobs), 1):
            if err:
                errors += 1
                print(f"ERROR {name}: {err}", file=sys.stderr, flush=True)
            total += written
            total_missing += missing
            if i % 10 == 0 or i == len(todo):
                print(f"[{i}/{len(todo)}] {total:,} docs ({total_missing:,} without title), {(time.time()-t0)/60:.1f} min", flush=True)
    print(f"Done: {total:,} docs, {total_missing:,} without title, {errors} errors, {(time.time()-t0)/60:.1f} min", flush=True)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
