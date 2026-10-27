# Raw records on S3: repack, validation and latency

Measured 2026-10-27 on one EC2 r6i.8xlarge in us-east-1 (hardware below), index `pyserini_index_v2`
(27,650,390 docs), against `s3://<bucket>/s2orc_raw_zstd/` in the same region.
This is steps 1–4 of [plans/02_raw_data_on_s3.md](plans/02_raw_data_on_s3.md); step 5 (smaller
instances) is not done yet.

## Summary

- The whole corpus is on S3: **1.94 TB → 505 GB** (3.85x), 513 objects, one zstd frame per record.
  S3 storage ≈ **$11.60/month**.
- Serving from S3 returns **exactly** what serving from local disk returned: every doc ID and DOI is
  covered, sampled records are byte-identical, and 1,500 API responses are identical.
- S3 adds **~140–200 ms** to a search (median), because a search waits for the slowest of 10 S3
  reads and a **cold** S3 range read takes ~100 ms. Searches stay well under a second:
  **p50 258 ms, p95 419 ms, p99 819 ms at 100 req/s**, no errors.
- GET fees at a steady 100 searches/s would be ~$1,040/month (10 GETs per search); at 1 search/s, ~$10.

## Hardware and software

| | |
|---|---|
| Instance | EC2 **r6i.8xlarge**, us-east-1a, on-demand |
| CPU | Intel Xeon Platinum 8375C @ 2.90 GHz, 16 cores × 2 threads = 32 vCPU |
| RAM | 247 GB (no swap) |
| Disk | one EBS gp3 volume, 5 TB. Baseline 3,000 IOPS / 125 MB/s; raised temporarily to **6,000 IOPS / 1,000 MB/s** for the repack and the benchmarks |
| Network | ENA, up to 12.5 Gbit/s; S3 reached through the VPC's internet gateway (no NAT), same region |
| S3 | Standard storage class, same region as the instance |
| OS / runtimes | Amazon Linux 2023, kernel 6.1; OpenJDK 21.0.9; Python 3.13.5 |
| Packages | pyserini 2.4.0, gunicorn 26.2.0, boto3 1.43, zstandard 0.25 |
| Server | 16 gunicorn workers per server (one JVM + Lucene searcher each); 64 record-loading threads per worker for S3, 16 for local disk |

Everything ran on this one box: repack, validation, both servers and the load generator. This is the
dev box, larger than the planned serving instance; smaller sizes are step 5 of the plan.

## Repack

`src/server/repack_raw_to_s3.py --workers 28` read the 513 raw files
(`data/s2orc_papers`), compressed every JSONL line as its own zstd frame (level 9), uploaded one
object per file to `s2orc_raw_zstd/<file>.zst`, checked each object's size, and wrote the lookup table
`raw_s3_map.sqlite` (0.66 GB): `(file, uncompressed offset) → (start, length)` for 27,771,312 records.

| | |
|---|---|
| Raw corpus | 1,941 GB, 27,771,312 records, avg 70 KB |
| On S3 | 505 GB, avg record 17.7 KB compressed |
| Ratio | 3.92x (`…123432…` files), 3.77x (`…130651…` files), 3.85x overall |
| Time | 44 min, disk-bound (EBS raised to 1,000 MB/s for the run; ~4 h at the 125 MB/s baseline) |

27,771,312 = 27,650,390 indexed + 120,922 that the index build logged as empty: the map holds
every line of the corpus.

Other per-record settings, measured on 2,000 records and scaled to the corpus:

| Option | Ratio | S3 size | Compress speed / core |
|---|---|---|---|
| zstd-9 (used) | 3.85x | 505 GB | 36 MB/s |
| zstd-19 | ~4.1x | ~475 GB | 2 MB/s (~8 h for the corpus) |
| zstd-9 + trained dictionary | ~4.2x | ~460 GB | 26 MB/s |
| zstd-19 + trained dictionary | ~4.7x | ~415 GB | 3 MB/s |
| *gzip-6, whole file (no random access)* | ~3.8x | – | – |

The best option saves ~$2/month; not worth an 8 h repack or a dictionary to ship with the server.
Decompression is 400–650 MB/s in every case (<0.2 ms per record).

## How a search reads a record

1. Lucene BM25 → top 10 doc IDs `{file}___{offset}__{version}` (unchanged).
2. `raw_s3_map.sqlite`: `(file, offset)` → `(start, length)` in the compressed object.
3. S3 `GetObject` with `Range: bytes=start-(start+length-1)`, ~18 KB.
4. zstd-decompress that frame alone → the original JSON line → `orjson` → same dict as before.

The 10 reads run in parallel on the worker's record-loading pool (`api.py read_json_from_s3`,
`_doc_pool`). The boto3 client is shared; each thread has its own SQLite connection and zstd
decompressor (a shared decompressor corrupts output under threads). `/doi` uses the same path.
Enabled by `RAW_S3_MAP_PATH`; without it the server reads the local corpus as before.

## Validation (`src/server/tests/validate_raw_s3.py`)

| Check | Result |
|---|---|
| `coverage`: every index doc ID has a map entry | 27,650,390 / 27,650,390 |
| `coverage`: every DOI map row has a map entry | 15,431,358 / 15,431,358 |
| `coverage`: every corpus file is in the map | 513 / 513 |
| `bytes`: S3 record == local line at the offset | 3,000 / 3,000 identical (2,000 via doc IDs, 1,000 via DOIs) |
| `servers`: local-disk server (8002) vs S3 server (8001), same requests | `/search` 500, `/fulltext` 500, `/doi` 500: all identical (`took_ms` ignored) |

## Latency (`src/server/tests/s3_benchmark.py`)

Setup: two servers on the same box, 16 gunicorn workers each, same index; **8001** reads records from
S3, **8002** from the local corpus (today's serving). Queries: 3 words drawn from 11,339 words in
4,952 real paper titles, `top_k=10`; each phase used a new seed, so its results had not been read
before. 30 s warm-up, then 60 s per phase. `10 clients` = closed loop (each client sends the next
request when the last returns); `50/100 req/s` = open loop at a fixed rate, latency counted from the
scheduled send time, so queueing is included. Server-side split from the `Server-Timing` header:
*search* = BM25, *fetch* = slowest of the 10 record reads.

### End to end (client latency)

| Load | Records from | Throughput | p50 | p95 | p99 | max | errors |
|---|---|---|---|---|---|---|---|
| 10 clients | local disk | 142 req/s | 65 ms | 130 ms | 170 ms | 444 ms | 0 |
| 10 clients | **S3** | 48 req/s | 199 ms | 289 ms | 418 ms | 1.8 s | 0 |
| 50 req/s | local disk | 50 req/s | 34 ms | 64 ms | 86 ms | 116 ms | 0 |
| 50 req/s | **S3** | 50 req/s | 204 ms | 313 ms | 571 ms | 17.7 s* | 0 |
| 100 req/s | local disk | 100 req/s | 53 ms | 105 ms | 131 ms | 177 ms | 0 |
| 100 req/s | **S3** | 100 req/s | 258 ms | 419 ms | 819 ms | 1.5 s | 0 |

\* one request; its slowest S3 read took 17.6 s (an S3 retry). All others in that phase < 2 s.

With 10 closed-loop clients, throughput is set by latency (10 clients / 0.2 s ≈ 50 req/s); the S3
server is not saturated there, since it then sustained 100 req/s.

### What S3 adds

| Load | fetch p50, local | fetch p50, S3 | added p50 | fetch p95, local | fetch p95, S3 | added p95 |
|---|---|---|---|---|---|---|
| 10 clients | 29 ms | 170 ms | +141 ms | 63 ms | 233 ms | +170 ms |
| 50 req/s | 16 ms | 170 ms | +154 ms | 32 ms | 246 ms | +214 ms |
| 100 req/s | 23 ms | 192 ms | +169 ms | 52 ms | 298 ms | +246 ms |

BM25 time is the same on both servers (p50 11–16 ms).

S3 alone, no server (`direct`, 1,300 random records, cold):

| | p50 | p95 | p99 | max |
|---|---|---|---|---|
| one range GET + decompress | 112 ms | 159 ms | 175 ms | 186 ms |
| 10 in parallel (one search) | 155 ms | 218 ms | 351 ms | 351 ms |

### Cold vs warm S3 reads

S3 caches recently written or read data. The same 40 ranges of one object read three times:
**74 ms** p50 the first time, **26 ms** the second and third. Freshly uploaded objects (up to 1 GB)
read at 25–35 ms; the repacked objects, written 30–60 min earlier, at ~100 ms. Real traffic asks for
random papers out of 505 GB, so most reads are cold: **plan on ~100 ms per GET, not the 10–30 ms
often quoted** (and measured in the first small test, which was warm). Repeat queries are faster.

### Things that mattered

- **Record-loading threads.** With the local-disk pool size (16 threads per worker) the S3 server
  managed only 14 req/s (p50 718 ms) at 10 clients, and fell behind at 50 and 100 req/s (queues of
  tens of seconds): 10 reads × ~150 ms per search queued behind each other. With 64 threads per
  worker (`SCHOLAR_DOC_THREADS`, now the default when `RAW_S3_MAP_PATH` is set) it sustains 100 req/s.
- Multipart vs single-part objects and object size do not change GET latency; only cold vs warm does.

### Caveats

- Local-disk numbers are partly page-cache-hot: the repack had just read the whole corpus through the
  page cache. The earlier sizing benchmarks measured fully cold local reads at a ceiling of ~150 searches/s on this
  volume.
- Load generator, both servers and S3 traffic shared the one r6i.8xlarge; only one server was under load
  at a time. Smaller instances (step 5 of the plan) are not measured yet.
- 60 s per phase; p99 and max rest on a few dozen requests.

## Reproduce

```
export RAW_S3_BUCKET=<bucket> INDEX_DIR=data/s2orc_index DOI_MAP_PATH=data/doi_map.sqlite
export ORIGINAL_DATA_PATH=data/s2orc_papers RAW_S3_MAP_PATH=data/raw_s3_map.sqlite
python src/server/repack_raw_to_s3.py --workers 28                  # once; writes data/raw_s3_map.sqlite
aws s3 cp data/raw_s3_map.sqlite s3://<bucket>/s2orc_index_v2/raw_s3_map.sqlite
python src/server/tests/validate_raw_s3.py coverage
python src/server/tests/validate_raw_s3.py bytes --n 2000
PORT=8001 ./start.sh &                                  # RAW_S3_MAP_PATH set: S3
PORT=8002 RAW_S3_MAP_PATH= ./start.sh &                 # local corpus
python src/server/tests/s3_benchmark.py terms --url http://127.0.0.1:8002 --n 5000 --out terms.txt
python src/server/tests/validate_raw_s3.py servers --n 500 --terms-file terms.txt
python src/server/tests/s3_benchmark.py direct --seed 77
python src/server/tests/s3_benchmark.py clients --url http://127.0.0.1:8001 --clients 10 --seed 4
python src/server/tests/s3_benchmark.py rate --url http://127.0.0.1:8001 --rate 50 --seed 5
python src/server/tests/s3_benchmark.py rate --url http://127.0.0.1:8001 --rate 100 --seed 6
```

A new server needs only the index and the two maps, not the corpus:
`S2ORC_S3_URI=s3://<bucket>/s2orc_index_v2 src/server/download_from_s3.sh` (~62 GB), then
`RAW_S3_MAP_PATH=data/raw_s3_map.sqlite ./start.sh`.

Use new `--seed` values for every rerun: a seed already used leaves its records warm in S3 (and in
the page cache for the local server).

