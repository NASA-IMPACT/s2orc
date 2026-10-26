---
name: scholar-api-full-plan
description: "Full design for the team scholar search API (snippets, fulltext, doi, bearer auth, gunicorn) drafted 2026-10-22; snippet endpoint is being built first, the rest is deferred"
metadata:
  node_type: memory
  type: project
  originSessionId: e50d2492-a13c-4bc7-ba87-f39018f0064f
  modified: 2026-10-22T20:21:18.643Z
---

Design drafted 2026-10-22 for a shared high-throughput scholar search API over the bm25s indices in `indices/bm25s_index`. The user chose to build only the snippet endpoint first; `/fulltext/`, `/doi/`, and the gunicorn rollout are deferred but this is the agreed shape for them.

**Why:** The user asked to save this plan before trimming it to snippets-only, so the deferred parts are not lost.

**How to apply:** When the snippet endpoint is finalized and the user says to move on, pick up from the deferred sections below instead of redesigning.

## Findings (state of repo on 2026-10-22)
- Fulltext search exists: `bm25/search_with_metadata.py:BM25Searcher.search()` (title/text/combined), served by `bm25/server/api_new.py` as `/search` with `top_k`. No auth, no offset, single uvicorn process.
- DOI lookup does not exist. No DOI->doc map; DOI only in raw records at `externalids.doi` (~97% of sampled docs have one). Only `get_document_by_corpus_id()` (linear scan) exists.
- Snippets do not exist; bm25s stores no positions/text, so snippets are computed from raw JSONL via `filename` + `file_offset`.
- `bm25/server/api.py` targets the old pickle index and is dead. Leave old files alone; new code goes in `bm25/server/scholar/`.
- Current index: 44,984 docs from 2 of 513 files, 549 MB. mmap load ~3 s, ~1.5 GB RSS/process, first retrieve ~6.7 s (numba JIT), later ~0.1 ms. bm25s numba retrieve mutates global `set_num_threads`, so guard retrieve with a per-process lock.

## Snippet endpoint `/search/` (being built first)
- Only `q` required. Optional with defaults: `top_k=10`, `offset=0`, `index=text` (or `combined`), `snippet_chars=300`, `highlight=false`.
- Response: `{"searchParameters": {...}, "docs": [{"position", "corpus_id", "title", "doi", "link", "content": "<snippet>", "score"}], "took_ms"}`. User explicitly wants the key `docs` and the snippet in `content`, not Serper's `organic`/`snippet`.
- Snippet selection: sentence chunks, stem with same stemmer as indexing, pick chunk with most distinct query stems, expand to ~snippet_chars, fallback to first paragraph. Skip v2 front matter using annotation offsets.
- Pure functions in `snippets.py` with unit tests; engine loads once per worker and warms JIT; plain `def` endpoints so FastAPI threadpool handles concurrency.

## Deferred: `/fulltext/`
`GET/POST /fulltext/?q=&top_k=10&offset=0&index=combined|title|text&title_weight=1.5&include_full_text=false`. Thin wrapper over `BM25Searcher.search()`; reuse pydantic models from `api_new.py`, add `position`, `offset`, `took_ms`. Add `offset` support to the searcher (retrieve `offset+top_k`, slice).

## Deferred: `/doi/`
`GET /doi/?doi=...` and `GET /doi/{doi:path}`. Normalize (strip `https://doi.org/`, `doi:`, lowercase). Needs `indices/bm25s_index/doi_map.json` (`{doi: doc_id}`) built by a new `scripts/build_doi_map.py` walking `metadata.json`; also emit it from `BM25Indexer.save_indices()` for future builds (collect doi in `DocumentMetadata` in `process_file_chunk`). Returns the entire raw JSONL record as a dict plus corpus_id/title/filename/file_offset; 404 if unknown; 503 if map missing.

## Deferred: auth + gunicorn
- `HTTPBearer` dependency; tokens from `SCHOLAR_API_KEYS` env (comma-separated) or `SCHOLAR_API_KEYS_FILE`; `secrets.compare_digest`; `/health` open; refuse to start without keys unless `SCHOLAR_AUTH_DISABLED=1`.
- `gunicorn.conf.py`: `worker_class="uvicorn_worker.UvicornWorker"`, workers from `SCHOLAR_WORKERS` (default min(8, cpu//4)), `preload_app=False`, `timeout=120`, warm-up retrieve at startup. `run_server.sh` execs `.venv/bin/gunicorn -c gunicorn.conf.py bm25.server.scholar.app:app`. Optional `NUMBA_CACHE_DIR`.
- Settings env vars: `SCHOLAR_INDEX_PATH`, `SCHOLAR_DATA_DIR`, `SCHOLAR_MAX_TOP_K=100`, `SCHOLAR_MAX_OFFSET=1000`, `SCHOLAR_SNIPPET_CHARS=300`.

## Status (2026-10-22): implemented in `api.py`

All endpoints are GET only, in one FastAPI app served by gunicorn (`start.sh`, `gunicorn.conf.py`):
- `/search` snippet search (default), `/fulltext` full-document search with `fields`, `/doi/{doi}` lookup.
- Bearer auth via `SCHOLAR_API_KEYS` / `SCHOLAR_API_KEYS_FILE`; open with a warning when unset.
- `/doi` uses `indexes/doi_map.sqlite` built by `build_doi_map.py` (regex scan of all 513 raw files,
  v2 records win over v1 duplicates). The scan is bound by the data volume's ~125 MB/s read rate, so
  a full build takes ~4 h; `/doi` returns 503 until the file exists.
- Measured with 2 gunicorn workers, 32 concurrent clients on `/search`: 34 req/s, p50 0.7 s.
