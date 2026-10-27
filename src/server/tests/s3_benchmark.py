#!/usr/bin/env python3
"""
Latency benchmark for /search with raw records on S3 (docs/plans/02_raw_data_on_s3.md).

Modes:
    terms    build a query-term file from real paper titles (GET /doi on random DOIs)
    direct   S3 alone, no server: random records from the map, one range GET at a
             time and 10 in parallel (what one search does)
    clients  closed loop: N clients, each sends its next request when the last returns
    rate     open loop: requests start at a fixed rate whatever the server does;
             latency counts from the scheduled start, so queueing is included

clients/rate report client latency plus the server's Server-Timing split:
search = BM25, fetch = slowest of the 10 record reads (the parallel fetch's
critical path; S3 GET + decompress, or the local file read). Each phase draws
fresh 3-word queries from the term file with its own seed, so results are not
in any cache from an earlier phase.

Usage (from the repo root):
    python src/server/tests/s3_benchmark.py terms --url http://127.0.0.1:8001 --out terms.txt
    python src/server/tests/s3_benchmark.py direct
    python src/server/tests/s3_benchmark.py clients --clients 10 --seconds 60 --terms-file terms.txt
    python src/server/tests/s3_benchmark.py rate --rate 50 --seconds 60 --terms-file terms.txt
"""

import argparse
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stress_test import DEFAULT_KEY_FILE, load_key, sample_dois  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
RAW_MAP = os.environ.get("RAW_S3_MAP_PATH") or str(REPO / "data" / "raw_s3_map.sqlite")
STOP = set("""about above after again against also among analysis based been before being between both case
cases could does during each effect effects from have having into more most other over same some such than
that their them then there these they this those through under using very were what when where which while
with within without would study studies""".split())


def pct(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * p))] if v else None


def summary(values):
    if not values:
        return {}
    return {"p50": round(statistics.median(values)), "p95": round(pct(values, 0.95)),
            "p99": round(pct(values, 0.99)), "max": round(max(values))}


def parse_server_timing(header):
    out = {}
    for part in (header or "").split(","):
        m = re.match(r"\s*(\w+);dur=([\d.]+)", part)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


class Client:
    def __init__(self, args):
        self.base = args.url
        self.headers = {"Authorization": f"Bearer {load_key(args)}"}
        self.lock = threading.Lock()
        self.rows = []   # (latency_ms, server_timing dict)
        self.errors = []

    def get(self, path, started=None):
        started = started or time.perf_counter()
        try:
            req = urllib.request.Request(self.base + path, headers=self.headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
                timing = parse_server_timing(r.headers.get("Server-Timing"))
            json.loads(body)
            with self.lock:
                self.rows.append(((time.perf_counter() - started) * 1000, timing))
            return body
        except Exception as e:
            with self.lock:
                self.errors.append(repr(e)[:120])

    def report(self, name, wall, **extra):
        lat = [r[0] for r in self.rows]
        out = {"phase": name, **extra, "requests": len(lat), "errors": len(self.errors),
               "achieved_rps": round(len(lat) / wall, 1), "latency_ms": summary(lat)}
        for key in ("search", "fetch", "total"):
            vals = [r[1][key] for r in self.rows if key in r[1]]
            out[f"server_{key}_ms"] = summary(vals)
        if self.errors:
            out["first_error"] = self.errors[0]
        print(json.dumps(out), flush=True)
        return out


def queries(args, seed):
    terms = [t.strip() for t in Path(args.terms_file).read_text().splitlines() if t.strip()]
    rng = random.Random(seed)
    while True:
        yield "/search?" + urllib.parse.urlencode({"q": " ".join(rng.sample(terms, 3)), "top_k": 10})


def run_clients(args):
    c = Client(args)
    pool = queries(args, args.seed)
    lock = threading.Lock()
    stop = time.perf_counter() + args.seconds

    def worker():
        while time.perf_counter() < stop:
            with lock:
                path = next(pool)
            c.get(path)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return c.report(f"{args.clients} clients", time.perf_counter() - t0, clients=args.clients, seconds=args.seconds)


def run_rate(args):
    c = Client(args)
    pool = queries(args, args.seed)
    n = int(args.rate * args.seconds)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.max_inflight) as ex:
        for i in range(n):
            due = t0 + i / args.rate
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            ex.submit(c.get, next(pool), due)
    return c.report(f"{args.rate:g} req/s", time.perf_counter() - t0, target_rps=args.rate, seconds=args.seconds)


def run_direct(args):
    import boto3
    import zstandard
    from botocore.config import Config

    db = sqlite3.connect(f"file:{RAW_MAP}?mode=ro", uri=True, check_same_thread=False)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    names = dict(db.execute("SELECT id, name FROM file"))
    rng = random.Random(args.seed)
    picks = []
    while len(picks) < args.n + args.batches * 10:
        fid = rng.choice(list(names))
        (count,) = db.execute("SELECT count(*) FROM record WHERE file_id = ?", (fid,)).fetchone()
        start, length = db.execute("SELECT start, length FROM record WHERE file_id = ? LIMIT 1 OFFSET ?",
                                   (fid, rng.randrange(count))).fetchone()
        picks.append((f"{meta['prefix']}{names[fid]}.zst", start, length))
    s3 = boto3.client("s3", config=Config(max_pool_connections=32))

    def fetch(p):
        key, start, length = p
        t = time.perf_counter()
        body = s3.get_object(Bucket=meta["bucket"], Key=key, Range=f"bytes={start}-{start + length - 1}")["Body"].read()
        zstandard.ZstdDecompressor().decompress(body)
        return (time.perf_counter() - t) * 1000, length

    fetch(picks[0])  # open the connection
    single = [fetch(p) for p in picks[: args.n]]
    batches = []
    with ThreadPoolExecutor(10) as ex:
        for b in range(args.batches):
            group = picks[args.n + b * 10: args.n + (b + 1) * 10]
            t = time.perf_counter()
            list(ex.map(fetch, group))
            batches.append((time.perf_counter() - t) * 1000)
    out = {"phase": "direct S3", "single_get_ms": summary([s[0] for s in single]),
           "ten_parallel_ms": summary(batches), "records": len(single) + 10 * len(batches),
           "compressed_kb": {"mean": round(statistics.mean(s[1] for s in single) / 1024, 1),
                             "p50": round(statistics.median(s[1] for s in single) / 1024, 1)}}
    print(json.dumps(out), flush=True)
    return out


def run_terms(args):
    c = Client(args)
    words = set()
    with ThreadPoolExecutor(16) as ex:
        for body in ex.map(lambda d: c.get("/doi/" + urllib.parse.quote(d, safe="/")), sample_dois(args.n, args.seed)):
            if body:
                title = json.loads(body).get("title") or ""
                words.update(w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in STOP)
    Path(args.out).write_text("\n".join(sorted(words)) + "\n")
    print(f"{len(words)} terms from {len(c.rows)} titles -> {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["terms", "direct", "clients", "rate"])
    ap.add_argument("--url", default="http://127.0.0.1:8001")
    ap.add_argument("--terms-file", default="terms.txt")
    ap.add_argument("--out", default="terms.txt", help="terms: output file")
    ap.add_argument("--clients", type=int, default=10)
    ap.add_argument("--rate", type=float, default=50)
    ap.add_argument("--max-inflight", type=int, default=1000, help="rate: max concurrent requests")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--n", type=int, default=300, help="terms: DOIs to sample; direct: single GETs")
    ap.add_argument("--batches", type=int, default=100, help="direct: groups of 10 parallel GETs")
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--key")
    ap.add_argument("--key-file", default=str(DEFAULT_KEY_FILE))
    args = ap.parse_args()
    {"terms": run_terms, "direct": run_direct, "clients": run_clients, "rate": run_rate}[args.mode](args)


if __name__ == "__main__":
    main()
