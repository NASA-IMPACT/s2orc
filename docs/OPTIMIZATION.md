# Scholar Search API: performance work and hardware requirements

Technical record of how the service was made fast enough for team use, what each change
measured, what did not help, and what hardware it needs. All numbers were measured on the
development box, an AWS `r6i.8xlarge` (32 vCPU, 247 GB RAM, one 5 TB gp3 volume at ~125 MB/s
sustained reads), between 2026-10-22 and 2026-10-23. Scripts referenced here are in `src/server/`
(server, snippets, DOI map, launcher, tests) and `src/pyserini/` (conversion and indexing); file
names are given without the directory. Default artifact locations in this repo are `data/s2orc_index`
(`INDEX_DIR`), `data/doi_map.sqlite` (`DOI_MAP_PATH`) and `data/s2orc_papers` (`ORIGINAL_DATA_PATH`).

## 1. Starting point

- A single `uvicorn` process (`uvicorn api:app --reload`) serving `GET /search`, which returned
  whole documents (50–500 KB each) for the top-k hits.
- Two Lucene indexes built with Pyserini from the S2ORC corpus (27.76 M papers, 1.8 TB of raw
  JSONL): a full-text index of 247 GB built with `--storePositions`, and a 3.5 GB title index.
- No authentication, no DOI lookup, no snippets.
- Observed behaviour: a query touching a part of the index not in the page cache took 1–2 s inside
  Lucene; any large disk job (conversions, index builds) evicted the cache and brought those stalls
  back; one process meant concurrent callers queued behind each other; the `--reload` poller
  walked the data and index directories four times a second and burned a core permanently.

## 2. Request path (what every optimization targets)

A `/search` request does, inside one gunicorn worker process:

1. **Lucene BM25 search** in the worker's JVM (via pyjnius): ~15–60 ms warm, 0.1–2 s if the
   postings it needs are not in the page cache. This call releases Python's GIL.
2. **Record loads**: for each of the `top_k` hits, parse the document id
   `<file>___<byte offset>__<version>`, `seek` into the raw JSONL file, `readline`, parse JSON.
   Records average ~220 KB. Disk/page-cache bound; the read releases the GIL, the parse does not.
3. **Passage extraction** (`snippets.py`): find query-term occurrences in the body text, group them,
   add context, score. Pure Python, holds the GIL. ~13–20 ms per request after optimization.
4. Build the pydantic response, serialize, write.

Steps 2 and 3 are the per-request **CPU cost under the GIL**, ~25–30 ms after optimization. Since
only one thread per process executes Python at a time, this sets the per-worker ceiling at roughly
25–35 requests/s, and total throughput scales with the number of worker processes until cores or
disk saturate. Everything below either shrinks that CPU cost, removes disk waits, or adds processes.

## 3. Changes, in the order they were made

### 3.1 Snippets instead of full documents (`snippets.py`)

The response for a hit went from the whole document (~220 KB) to passages around the query
match (~3 KB per paper, 32 KB per response with 5 passages × 10 papers). Mechanics:

- Query terms are stemmed with PyStemmer (Porter, the same family Lucene uses) and stopwords
  dropped; document words are matched stem-to-stem, so "resistance" matches "resistant".
- A regex prefilter built from the query stems (`\b(?:branch|chain|…)[^\W_]*`) runs at C speed over
  the body text, so only candidate words (a few hundred) are stemmed rather than the ~13,000 words
  of a typical paper. Stemming every word cost 260 ms per request; the prefilter brought the
  matcher to ~40 ms, and anchoring with `\b` instead of a lookbehind to ~13 ms.
- Passages are built from the densest cluster of occurrences within 300 characters, with
  `context_words` (default 20) words before the first and after the last matched word; up to
  `max_matches` (default 5) non-overlapping passages per paper, best first. Passage score is
  `(distinct query terms + mean tf/(tf+1)) / (query terms + 1)`.
- Only the paper body is scanned: the v2 author/affiliation block at the top and the reference list
  (first `bibentry` annotation) are skipped, because references match query terms densely but make
  poor snippets.

### 3.2 Multi-process serving with gunicorn (`start.sh`, `gunicorn.conf.py`)

- `gunicorn` with `uvicorn_worker.UvicornWorker`, one JVM per worker (`preload_app = False`
  because a JVM cannot be forked). Each worker: ~1.3 GB RSS, ~15 s to start (JVM + index open +
  warm-up queries).
- Endpoints are synchronous `def`s, so Starlette runs each request on a thread from a per-worker
  pool (default 40). Inside a request, the `top_k` record loads are fanned out to a 16-thread loader
  pool so they overlap on I/O.
- Worker count sweep with 32 concurrent clients, old index:

  | workers | `/search` req/s | p50 |
  |---|---|---|
  | 1 | 17.5 | 1.77 s |
  | 2 | 31.4 | 0.97 s |
  | 4 | 53.6 | 0.48 s |
  | 8 | 72.7 | 0.33 s |

  and with 100 clients after a proper JIT warm-up (the 16-worker figure in the 32-client sweep was
  depressed by cold JVMs):

  | workers | `/search` req/s | p50 |
  |---|---|---|
  | 8 | 161 | 0.58 s |
  | 16 | 225 → 246 (with the `\b` regex) | 0.42 → 0.39 s |

  16 workers (one per two vCPUs) is the default. `timeout = 300` so a worker is never killed during
  JVM start or a cold read; `graceful_timeout = 30`; pid file `gunicorn-<PORT>.pid`.
- Verified that pyjnius releases the GIL during Lucene calls (4 threads: 2.5× speed-up on
  concurrent searches), so Lucene work overlaps inside a worker and the Python CPU work is the
  binding constraint.
- **Thread pool size does not matter**: 8, 16 and 40 request threads per worker measured
  336/337/343 req/s at 100 clients. Left at 40 (`SCHOLAR_THREADS`).
- `--reload` is passed to gunicorn, whose reloader only watches imported modules, instead of
  uvicorn's, which stat-polled the entire repo including the data directories.

### 3.3 Index rebuild: no positions, two fields (`convert_data_merge.py`, SETUP.md)

The single largest latency change.

- **Why**: `--storePositions` records the position of every term occurrence, needed only for phrase
  and proximity queries, which the API never issues (the parser turns the query into a bag of
  terms; snippets are computed in Python from the raw text). Positions were ~75% of the index:
  in the non-compound segments, 6.8 GB postings + 2.4 GB term dictionary versus 27.6 GB positions.
  The 247 GB index could not be kept resident on a 247 GB box, and 209 GB of it was in compound
  files that mix positions with the data we need, so partial warming was impossible.
- **What**: the converted body and title collections were merged into one two-field collection
  (1 TB, 4.5 h, disk-bound) with one JSON document per paper `{"id", "title", "contents"}`, then
  indexed with `--fields title` and without `--storePositions` (2 h 48 min, 20 threads).
- **Result**: the two-field index (`data/s2orc_index` here), **60 GB**, 27,650,390 documents (120,922
  records with an empty body are skipped by the indexer; they were title-only entries before).
  Same query gives the same top-10 as the old index. Cold query 130–500 ms instead of 1–2 s;
  warm 14–61 ms. The whole index fits in the page cache with >150 GB to spare.
- **Combined search**: Pyserini's multi-field search lets one index serve `index=all` (body +
  title, title weighted `SCHOLAR_TITLE_WEIGHT` = 2.0, the default), `index=text` and `index=title`,
  so the separate title index is no longer opened.
- Quoted queries do not error on the position-free index (the parser does not emit phrase queries),
  so no query sanitising was needed.

### 3.4 Page-cache warm-up (`cache_warm.sh`)

Lucene reads the index through memory-mapped files; a page not in the OS page cache is a disk read.
`cache_warm.sh` streams every index file and the DOI map through `cat` at the lowest CPU and I/O
priority (`nice -n 19 ionice -c 3`), which makes the whole index resident: ~8 min from cold at
125 MB/s, seconds if mostly cached. Safe while serving. Run after boot, after a quiet period, and
after any large disk job. Note that `free` reports the cache under `buff/cache`, not `used`; a
server with 16 workers and the index resident shows ~22 GB used and 60+ GB in cache.

### 3.5 DOI lookup (`build_doi_map.py`, `/doi/{doi}`)

- The index has no DOI field and never will: Lucene tokenizes `10.1155/2016/2794591` into three
  numeric tokens, and a phrase query for a DOI against the body text found the right paper top-1
  for only 15/40 v2 and 2/40 v1 DOIs, at ~6 s per query. Identifiers need exact-key lookup, not
  ranked retrieval.
- The DOI map (`data/doi_map.sqlite`, 1.7 GB): table `doi(doi PRIMARY KEY, filename, offset) WITHOUT ROWID`,
  15.4 M unique DOIs. Built by a regex over the first 8 KB of every raw record (no JSON parsing),
  v2 files first so that papers present in both formats resolve to the v2 record. 4 h 8 min,
  disk-bound. Lookups are 10–100 µs; the endpoint answers in a few ms including the record read.
  Read-only connections per thread; safe across all workers.
- DOI normalization: lowercase (DOIs are case-insensitive by spec), resolver prefixes stripped.

### 3.6 Record parsing with orjson

Records are parsed from raw bytes with `orjson.loads`, skipping the decode step: 6.4 → 2.2 ms per
10 records. `/search` 345 → 355 req/s, `/fulltext` 560 → 590 req/s at 100 clients.

### 3.7 Correctness fixes found by the load tests

- v1 records' DOI was read from a non-existent key (`external_ids` instead of `externalids`), so
  v1 papers never reported a DOI.
- Some records have `"externalids": null` (and other nested objects can be null); the extraction
  assumed dicts and raised, and the paper was silently dropped from results. All nested lookups
  are now null-safe.

## 4. Final measurements

`src/server/tests/stress_test.py`, 16 workers, 100 concurrent clients, 45 s per phase, index pre-warmed, zero
errors:

| endpoint | req/s | p50 | p95 | p99 | response |
|---|---|---|---|---|---|
| `/search` (top_k=10, 5 passages) | 355 | 264 ms | 481 ms | 610 ms | 32 KB |
| `/fulltext` fields=title,doi,authors | 590 | 145 ms | 362 ms | 489 ms | 7 KB |
| `/fulltext` all fields (incl. text) | 257 | 379 ms | 577 ms | 668 ms | 699 KB |
| `/doi/{doi}` | 1,964 | 49 ms | 84 ms | 102 ms | 40 KB |

Single client, warm: `/search` 60–90 ms, `/doi` ~5 ms.

1,000 concurrent clients (same server): `/search` 295 req/s, p50 1.4 s, p95 14 s, p99 21 s;
`/fulltext` (small) 556 req/s p50 0.8 s; `/doi` 1,818 req/s p50 0.26 s; no errors. Throughput is
unchanged from 100 clients; the extra callers queue. Concurrency is not the limit (thousands of
open connections are fine, worker fd limit 16,384, backlog 2,048); completed searches per second
is, at ~350 on this box.

Before/after for the dominant cost, a `/search` with 10 hits:

| stage | before | after |
|---|---|---|
| Lucene search, cold | 1–2 s | 0.13–0.5 s (and stays warm: index fits in RAM) |
| Lucene search, warm | ~40 ms | 15–60 ms |
| parse 10 records | 8.6 ms (ujson) | 2.2 ms (orjson) |
| find matches | 260 ms | 13 ms |
| cluster + render 5 passages | — | ~8 ms |
| Python CPU per request | ~300 ms | ~25–30 ms |

## 5. What did not help, or was rejected

- **Request-thread pool size** (8/16/40): no effect; CPU-bound under the GIL.
- **Warming the old index**: impossible to make 247 GB resident on a 247 GB machine, and compound
  segments cannot be partially warmed.
- **DOI through the text index**: see 3.5.
- **Async endpoints**: the work is CPU, not waiting; no gain.
- **Free-threaded Python**: the JVM bridge (pyjnius) is not ready for it.
- **Qdrant/OpenSearch**: a different retrieval model (vectors) or the same Lucene engine with a
  network hop; neither addresses the measured bottlenecks (cold cache, single process, Python CPU).

## 6. Remaining levers

1. **Response cache** keyed on the full request (in-process, or Redis to share across workers): if
   callers repeat queries, repeats become sub-millisecond. Needs traffic data to justify.
2. **Client-side defaults**: `top_k=5` roughly halves per-request CPU; `max_matches=1` drops the
   extra passages.
3. **More workers measured with the load generator on another machine**: the 16-worker ceiling was
   measured with the Python client's threads sharing the same 32 cores.
4. **Per-worker concurrency limit** (fast 503 under overload) to bound tail latency instead of
   queueing 20 s.
5. **Capping the body scan** (e.g. first 200 KB) to shave the 13 ms regex cost on very long papers,
   at the price of `match_count` accuracy.

## 7. Hardware requirements (serving only; no index builds)

### Artifacts that must be on the serving machine

| artifact | size | role |
|---|---|---|
| two-field index (`INDEX_DIR`, default `data/s2orc_index`) | 60 GB | every search; should be resident in RAM |
| DOI map (`DOI_MAP_PATH`, default `data/doi_map.sqlite`) | 1.7 GB | `/doi` lookups |
| raw corpus (`ORIGINAL_DATA_PATH`, default `data/s2orc_papers`, 513 JSONL files) | 1.8 TB | every result reads its record by byte offset |

Nothing else is read at query time (not the old index, title index, archives or converted
collections).

### Disk

- Capacity: ~1.9 TB of data; a **2.5 TB** volume leaves headroom for logs and a future index.
- Access pattern: random reads of 50–500 KB records plus occasional index page faults. **IOPS matter
  more than bandwidth.** gp3 at 6,000 IOPS / 250 MB/s or better is a sensible floor; at the gp3
  baseline (3,000 IOPS) a cold cache caps `/search` around 150 req/s until hot records are cached.
  Local NVMe is better still if the data can be copied onto it.

### Memory

| component | per worker | 8 workers | 16 workers |
|---|---|---|---|
| worker (JVM heap + Lucene + Python) | ~1.3 GB | 11 GB | 21 GB |
| index in page cache | | 60 GB | 60 GB |
| DOI map in cache | | 2 GB | 2 GB |
| hot records, OS, headroom | | 15–20 GB | 15–20 GB |
| **total** | | **~90 GB** | **~100 GB** |

**128 GB** is the practical size. 64 GB cannot hold the index alongside the workers, which
reinstates the cold-query stalls.

### CPU

Throughput ≈ 22 `/search` req/s per worker; one worker per two vCPUs is the sweet spot.

| instance (AWS) | vCPU / RAM | workers | approx. `/search` | notes |
|---|---|---|---|---|
| r6i.4xlarge | 16 / 128 GB | 8 | ~160 req/s | cheapest that keeps the index resident |
| m6i.8xlarge | 32 / 128 GB | 16 | ~355 req/s | same throughput as measured, half the RAM |
| r6i.8xlarge (dev box) | 32 / 247 GB | 16 | ~355 req/s | ~150 GB RAM unused when only serving |

Set `WORKERS` explicitly to half the vCPU count; the config caps at the core count but does not
scale with memory.

### Network and process limits

- Responses are small (`/search` 32 KB) except `/fulltext` with text (~700 KB); 1 Gbit/s is ample.
- Worker file-descriptor limit 16,384 and listen backlog 2,048 handle thousands of concurrent
  connections; beyond ~100 concurrent callers requests queue rather than fail.

### Startup checklist on a new machine

1. Copy the three artifacts; point `INDEX_DIR`, `DOI_MAP_PATH` and `ORIGINAL_DATA_PATH` at them
   (or place them at the defaults under `data/`).
2. Java 21, `uv venv && uv pip install -r src/requirements.txt` (see [SETUP.md](../SETUP.md)).
3. Keys in `~/.scholar/api_keys` (one per caller).
4. `WORKERS=<vCPU/2> nohup src/server/start.sh > server.log 2>&1 &`
5. `src/server/cache_warm.sh` (≈8 min from cold), then `python src/server/tests/stress_test.py`
   to confirm numbers.
