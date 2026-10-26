#!/usr/bin/env python3
"""
Stress test for the Scholar Search API: /search, /fulltext and /doi/{doi}.

Runs N concurrent clients against a running server for a fixed time per
phase and reports throughput and latency percentiles. Only standard library
is used, so it runs anywhere Python 3 is installed.

Usage (from the repo root):
    python src/server/tests/stress_test.py                              # localhost:8000, key from ~/.scholar/api_keys
    python src/server/tests/stress_test.py --url http://host:8001 --clients 100 --seconds 45
    python src/server/tests/stress_test.py --phases search,doi          # subset of phases
    SCHOLAR_API_KEY=... python src/server/tests/stress_test.py          # explicit key

Phases:
    search        GET /search?q=<3 random terms>&top_k=10          (index=all)
    fulltext      GET /fulltext?...&fields=title,doi,authors
    fulltext-all  GET /fulltext?...                                 (includes full text)
    doi           GET /doi/<random DOI from the DOI map ($DOI_MAP_PATH, default data/doi_map.sqlite)>

Each phase is preceded by a warm-up unless --no-warmup is given; the warm-up
matters because every gunicorn worker JIT-compiles Lucene on its first few
hundred queries and the index pages come in from disk.

Measured 2026-10-23 on the 32-core box, 16 workers, 100 clients, 45 s per
phase, index pre-warmed with cache_warm.sh (pyserini_index_v2, 60 GB):

    phase                               rps    p50    p95    p99   resp
    /search                           345    272ms  504ms  618ms   32 KB
    /fulltext fields=title,doi,authors 560    157ms  362ms  493ms    7 KB
    /fulltext all fields               257    379ms  577ms  668ms  699 KB
    /doi/{doi}                       1964     49ms   84ms  102ms   40 KB

At 1,000 clients: /search 295 rps p50 1449 ms p95 14.2 s p99 21.2 s;
/fulltext (small) 556 rps p50 840 ms; /doi 1818 rps p50 264 ms; no errors.

Zero errors. Large deviations usually mean cold workers (no warm-up), a
cold page cache (run cache_warm.sh), another job saturating the disk, or
too few workers.
"""

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_KEY_FILE = Path.home() / ".scholar" / "api_keys"
DOI_MAP = Path(os.environ.get("DOI_MAP_PATH") or REPO / "data" / "doi_map.sqlite")

TERMS = [
    "insulin", "resistance", "graph", "neural", "network", "climate", "adaptation", "protein", "folding",
    "reinforcement", "learning", "robot", "vaccine", "efficacy", "quantum", "error", "correction", "lithium",
    "battery", "degradation", "transformer", "attention", "language", "model", "microbiome", "obesity",
    "perovskite", "solar", "cell", "sepsis", "detection", "wind", "turbine", "fatigue", "bayesian",
    "optimization", "mitochondrial", "aging", "cancer", "immunotherapy", "graphene", "sensor", "crispr",
    "genome", "editing", "supply", "chain", "resilience", "soil", "carbon", "sequestration", "wireless",
    "channel", "estimation", "turbulence", "simulation", "drug", "delivery", "nanoparticle", "polymer",
]


def load_key(args) -> str:
    if args.key:
        return args.key
    env = os.environ.get("SCHOLAR_API_KEY")
    if env:
        return env
    path = Path(args.key_file)
    if path.exists():
        for line in path.read_text().splitlines():
            key = line.split("#", 1)[0].strip()
            if key:
                return key
    return ""


def sample_dois(n: int, seed: int):
    if not DOI_MAP.exists():
        return []
    db = sqlite3.connect(f"file:{DOI_MAP}?mode=ro", uri=True)
    # ORDER BY RANDOM() would scan all ~15M rows (minutes). Instead probe the
    # primary-key B-tree at random points and take a run of consecutive DOIs
    # from each: not uniformly random, but spread across registrants and fast.
    rng = random.Random(seed)
    per_probe = 50
    out = []
    for _ in range(n // per_probe + 1):
        start = "10.%d/%s" % (rng.randint(1000, 9999), rng.choice("0123456789abcdefghijklmnopqrstuvwxyz"))
        rows = db.execute("SELECT doi FROM doi WHERE doi >= ? LIMIT ?", (start, per_probe)).fetchall()
        out.extend(r[0] for r in rows)
    rng.shuffle(out)
    return out[:n]


class Phase:
    def __init__(self, name, make_url):
        self.name, self.make_url = name, make_url


def run_phase(base, headers, phase, clients, seconds, timeout):
    lat, errs, sizes = [], [], []
    lock = threading.Lock()
    counter = [0]
    stop = time.perf_counter() + seconds

    def worker():
        while time.perf_counter() < stop:
            with lock:
                i = counter[0]
                counter[0] += 1
            url = base + phase.make_url(i)
            t = time.perf_counter()
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read()
                json.loads(body)
                with lock:
                    sizes.append(len(body))
            except Exception as e:  # count and keep going
                with lock:
                    errs.append(repr(e)[:100])
            with lock:
                lat.append((time.perf_counter() - t) * 1000)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(clients)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = time.perf_counter() - t0
    lat.sort()
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else float("nan")
    return {
        "phase": phase.name, "clients": clients, "seconds": round(wall, 1), "requests": len(lat),
        "rps": round(len(lat) / wall, 1) if wall else 0.0,
        "p50_ms": round(statistics.median(lat)) if lat else None,
        "p95_ms": round(pct(0.95)) if lat else None, "p99_ms": round(pct(0.99)) if lat else None,
        "max_ms": round(lat[-1]) if lat else None,
        "resp_kb": round(statistics.median(sizes) / 1024, 1) if sizes else None,
        "errors": len(errs), "first_error": errs[0] if errs else None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="server base URL")
    ap.add_argument("--clients", type=int, default=100, help="concurrent clients per phase")
    ap.add_argument("--seconds", type=int, default=45, help="duration of each phase")
    ap.add_argument("--warmup-seconds", type=int, default=40)
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--phases", default="search,fulltext,fulltext-all,doi", help="comma-separated subset")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--index", default="all", choices=["all", "text", "title"])
    ap.add_argument("--queries", type=int, default=300, help="distinct random queries in the pool")
    ap.add_argument("--dois", type=int, default=2000, help="random DOIs sampled from the DOI map")
    ap.add_argument("--key", help="bearer token (else $SCHOLAR_API_KEY, else first key in --key-file)")
    ap.add_argument("--key-file", default=str(DEFAULT_KEY_FILE))
    ap.add_argument("--timeout", type=float, default=120.0, help="per-request timeout, seconds")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--json", action="store_true", help="print results as JSON lines")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    key = load_key(args)
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    try:
        with urllib.request.urlopen(f"{base}/health", timeout=10) as r:
            health = json.load(r)
    except Exception as e:
        sys.exit(f"server not reachable at {base}: {e}")
    if health.get("status") != "healthy":
        sys.exit(f"server unhealthy: {health}")
    print(f"server {base}: {health.get('num_docs', '?'):,} docs, index {health.get('index_path')}, "
          f"auth {'on' if key else 'off (no key)'}", flush=True)

    rng = random.Random(args.seed)
    pool = [" ".join(rng.sample(TERMS, 3)) for _ in range(args.queries)]
    q = lambda i: pool[i % len(pool)]
    enc = urllib.parse.urlencode
    wanted = [p.strip() for p in args.phases.split(",") if p.strip()]
    phases = {
        "search": Phase("/search", lambda i: "/search?" + enc({"q": q(i), "top_k": args.top_k, "index": args.index})),
        "fulltext": Phase("/fulltext fields=title,doi,authors", lambda i: "/fulltext?" + enc(
            {"q": q(i), "top_k": args.top_k, "index": args.index, "fields": "title,doi,authors"})),
        "fulltext-all": Phase("/fulltext all fields", lambda i: "/fulltext?" + enc(
            {"q": q(i), "top_k": args.top_k, "index": args.index})),
    }
    if "doi" in wanted:
        dois = sample_dois(args.dois, args.seed)
        if dois:
            phases["doi"] = Phase("/doi/{doi}", lambda i: "/doi/" + urllib.parse.quote(dois[i % len(dois)], safe=""))
        else:
            print("skipping doi phase: DOI map not found", flush=True)
    unknown = [p for p in wanted if p not in phases and p != "doi"]
    if unknown:
        sys.exit(f"unknown phases: {unknown}")

    if not args.no_warmup and args.warmup_seconds > 0:
        print(f"warm-up: {args.warmup_seconds}s of /search with 32 clients", flush=True)
        run_phase(base, headers, phases["search"], 32, args.warmup_seconds, args.timeout)

    hdr = f"{'phase':36s} {'clients':>7s} {'rps':>7s} {'p50':>6s} {'p95':>6s} {'p99':>6s} {'max':>6s} {'resp':>8s} {'n':>6s} {'err':>4s}"
    if not args.json:
        print(hdr); print("-" * len(hdr))
    for name in wanted:
        if name not in phases:
            continue
        r = run_phase(base, headers, phases[name], args.clients, args.seconds, args.timeout)
        if args.json:
            print(json.dumps(r), flush=True)
        else:
            print(f"{r['phase']:36s} {r['clients']:7d} {r['rps']:7.1f} {r['p50_ms']:5d}ms {r['p95_ms']:5d}ms "
                  f"{r['p99_ms']:5d}ms {r['max_ms']:5d}ms {r['resp_kb']:6.1f}KB {r['requests']:6d} {r['errors']:4d}"
                  + (f"   first error: {r['first_error']}" if r["errors"] else ""), flush=True)


if __name__ == "__main__":
    main()
