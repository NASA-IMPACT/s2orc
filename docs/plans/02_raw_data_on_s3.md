# Scholar Search API — Step 2: raw data on S3, smaller disk (`s2orc`)

Status: proposal (2026-10-27). No code yet.

## Context

- Goal: cut serving cost, target ~$100/month. AWS only (org requirement).
- Today the server holds: 60 GB Lucene index, 1.8 TB decompressed S2ORC JSONL (`ORIGINAL_DATA_PATH`),
  1.7 GB DOI map. Sizing report: r6i.4xlarge + 2.5 TB gp3 (6,000 IOPS) = $959/month; disk ≈ $223 of it.
- Index already minimal: no `--storeRaw`, `--storeContents`, `--storeDocvectors`, `--storePositions`.
  The 60 GB is postings + term dictionary + norms. There is no "strip the index" saving left.
- Lucene returns doc IDs only (`{filename}___{offset}__{version}`); the API reads each record by byte
  offset from local JSONL (`api.py:175 read_json_at_offset`, `api.py:362`, `/doi` at `api.py:717`).
  Snippets need the whole record text (`snippets.py`).
- Have: 300 GB compressed copy (S2ORC download, gzip). gzip is one stream per file: no random access
  to a single record, so it cannot be range-read from S3 as is.

## Proposal

Keep the index and the doc IDs. Move the raw records to S3, compressed one record at a time, and read
each search result with an S3 byte-range GET.

### Flow
1. Lucene BM25 → top-k doc IDs (unchanged).
2. Local lookup table: doc ID → (S3 key, compressed start byte, length).
3. k parallel S3 range GETs.
4. zstd-decompress in RAM → same JSON as today → `snippets.py` unchanged.

### One-time repack
- Per source file: decompress; zstd-compress **each record as its own frame**; append frames to a new
  file; upload to S3.
- While writing, record `(filename, uncompressed offset) → (s3_key, start, length)` in a SQLite table
  (27.7M rows, est. 1–2 GB, not measured). Same shape as `doi_map.sqlite`.
- Old doc IDs stay valid → **no index rebuild**. DOI map keeps `(filename, offset)` and goes through
  the same table.
- Run on a temporary build box/volume; delete afterwards.

### Code changes (small)
- `read_json_at_offset` / `retrieve_original_document`: local file read → lookup + S3 range GET +
  decompress. Keep the local-file path behind a setting so dev boxes still work.
- `/search/`: fetch the k records in parallel (thread pool; boto3 client is thread-safe).
- `/doi`, `/fulltext`: same fetch function.
- `/health`: report S3 reachability instead of `data_path` existence.
- Optional: small RAM/disk LRU cache of recent records (cuts latency and GET fees on repeat traffic).

### Infra
- EBS: ~100 GB gp3 at baseline (3,000 IOPS / 125 MB/s): index 60 GB + lookup table + DOI map + OS.
- S3 Standard, same region as EC2.
- **S3 gateway VPC endpoint required.** Through a NAT gateway, S3 traffic is billed ~$0.045/GB.
- Future index rebuilds: temporary large instance + ~4 TB volume reading from S3, then deleted.

## Costs (estimates, us-east-1, on-demand unless noted)

| Item | Now | Proposed |
|---|---|---|
| EBS | 2.5 TB gp3 + IOPS ≈ $223 | 100 GB gp3 ≈ $8 |
| S3 storage | – | ~300 GB × $0.023 ≈ $7 |
| S3 GETs ($0.40 / million, 10 per search) | – | 1 search/s ≈ $10; 10/s ≈ $104; 150/s ≈ $1,550 |
| Compute | r6i.4xlarge (128 GB) | see below |

Compute decides the total; S3 does not change it:
- 64 GB RAM (r6i.2xlarge ≈ $370 on-demand; Graviton / savings plan lower, prices not checked):
  measured 0.11 s p50 at 10 clients, ~100 searches/s.
- 32 GB RAM: measured 0.56 s p50, 3.4 s at 100 clients (index does not fit). Raw records leaving the
  disk may help (no page-cache competition) — unproven.
- ~$100/month total is reachable only if 32 GB is fast enough, with a long commitment.

## Trade-offs

- **Latency**: S3 GET ~10–30 ms, tails 100 ms+; a search waits for the slowest of 10. Expect
  +30–100 ms, so ~100–200 ms median vs ~70 ms warm today. Sub-second is acceptable (stated).
- **GET fees scale with traffic**; at our measured peak they exceed the server cost.
- **Records are large** (~220 KB avg uncompressed), not 2 KB passages; each search downloads
  ~10 compressed records.
- Disk saving (~$215/month) is safe; compute saving is not, until measured.

## Corrections to the Gemini analysis

- "Strip index to 12–30 GB": assumes stored raw/docvectors/positions; we store none.
- "60 GB index on 32 GB, hot terms cached, 15–35 ms": guess; our 32 GB measurement says otherwise.
- "2 KB passages": our records average ~220 KB; snippets need the full text.
- t4g burstable: throttles under steady load; unsuitable.
- "Use the 300 GB compressed dataset directly": gzip is not range-readable; repack required.

## Steps

1. Measure zstd per-record ratio on a few sample files → real S3 size and per-search bytes.
2. Repack one file + lookup table; measure S3 range-GET p50/p95 from EC2 in-region.
3. Implement the S3 read path behind a setting; unit-test against the local path (same JSON).
4. Repack full corpus; upload; build the full lookup table.
5. Rerun the sizing benchmark (`/search`, real-title queries, 10 and 100 clients) with S3 reads on
   32 GB and 64 GB. Pick the instance.
6. Move serving to the ~100 GB volume; keep raw data only in S3. Update `README.md` hardware table,
   `docs/OPTIMIZATION.md`, `src/server/README.md`.

## Unresolved questions

- Expected traffic: searches/day and peak? (sets GET fees and instance size)
- Acceptable latency: median 200 ms, p95 ~1 s OK?
- 1-yr / 3-yr AWS commitment allowed by the org?
- Must `/fulltext` and `/doi` stay, or is `/search` the main use?
- If 32 GB is too slow: accept a title+abstract-only index (product change) or pay for 64 GB?
- Keep a local copy of the raw data anywhere (backup), or S3 only?
