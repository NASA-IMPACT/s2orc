## Scholar Search API Server

FastAPI service over a Pyserini (Lucene) BM25 index of the S2ORC corpus (27.7M full-text papers,
title and body as separate fields), served by gunicorn.

Endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /search` | snippet search: top papers with a short passage around the query match |
| `GET /fulltext` | full-document search: top papers with their extracted fields |
| `GET /doi/{doi}` | exact lookup of one paper by DOI |
| `GET /health`, `GET /` | health checks (no auth) |

Interactive docs: `http://<host>:8000/docs`

New here? [SETUP.md](../../SETUP.md) at the repo root walks from the raw data to a running
server; this page is the reference for the server itself.

Files in this directory:

| File | Purpose |
|---|---|
| `api.py` | the FastAPI app |
| `snippets.py` | passage extraction for `/search` (pure Python, unit-tested, no JVM) |
| `build_doi_map.py` | one-off builder of the DOI → record map used by `/doi/{doi}` |
| `gunicorn.conf.py` | gunicorn settings (launched by `./start.sh` at the repo root) |
| `cache_warm.sh` | reads the index into the OS page cache |
| `api_client_example.py` | runnable Python client |
| `tests/test_snippets.py` | unit tests for `snippets.py` |
| `tests/stress_test.py` | load generator for a running server |

### Prerequisites

Python packages come from uv (`pyproject.toml` + `uv.lock` at the repo root): `uv sync` creates
`.venv`, and `start.sh` runs gunicorn through `uv run`. Plain pip users can install
`src/requirements.txt` (exported from the lock) into a Python 3.13 environment instead. The
Docker image (`docker compose build`) contains everything including Java.

Pyserini needs Java 21. The index must be the **two-field** index built from the merged
text+title collection with `--fields title` (see [src/pyserini/README.md](../pyserini/README.md),
steps 1b and 2). `index=all` and `index=title` search the `title` field, which a text-only index
does not have.

### Configuration

Settings are environment variables, read from `.env` at the repo root when present (copy
`.env.example`; shell variables take precedence). Path defaults are relative to the repo root,
so the server works from any working directory:

| Variable | Default | Description |
|---|---|---|
| `INDEX_DIR` | `data/s2orc_index` | Lucene index with `contents` and `title` fields (`FULL_TEXT_INDEX_DIR` is accepted as an older name) |
| `ORIGINAL_DATA_PATH` | `data/s2orc_papers` | decompressed S2ORC `.jsonl` files; records are read back by byte offset |
| `DOI_MAP_PATH` | `data/doi_map.sqlite` | DOI map built by `build_doi_map.py` |
| `SCHOLAR_API_KEYS`, `SCHOLAR_API_KEYS_FILE` | none | bearer tokens, see Authentication |
| `SCHOLAR_TITLE_WEIGHT` | `2.0` | title boost for `index=all` |
| `SCHOLAR_THREADS` | `40` | request threads per worker |
| `HOST`, `PORT`, `WORKERS` | `0.0.0.0`, `8000`, `16` | gunicorn bind address and worker count (`start.sh`) |

```bash
export INDEX_DIR="/path/to/s2orc_index"
export ORIGINAL_DATA_PATH="/path/to/s2orc_papers"
export DOI_MAP_PATH="/path/to/doi_map.sqlite"
```

### Setup

```bash
# one-off, ~4-5 h for the full 1.8 TB corpus (disk-bound): builds the DOI map for /doi/{doi}
python src/server/build_doi_map.py                       # -> data/doi_map.sqlite
python src/server/build_doi_map.py --limit 2             # quick test on two files
python src/server/build_doi_map.py --classification docs/dataset_classification.json
#   optional {"v1": [...], "v2": [...]} file listing which data files are which format;
#   v2 files are scanned first so a paper present in both keeps its richer v2 record
```

`/doi/{doi}` returns `503` until the map exists; everything else works without it.

### Running

Production (gunicorn, from the repo root):

```bash
./start.sh                                   # http://0.0.0.0:8000, 16 workers, keys from ~/.scholar/api_keys
PORT=8001 WORKERS=2 ./start.sh               # other port / worker count
SCHOLAR_API_KEYS=key1,key2 ./start.sh        # keys from the environment instead
./start.sh --reload                          # dev: restart workers on code change
./start.sh --warm-cache                      # also warm the page cache in the background (log: cache_warm.log)
src/server/cache_warm.sh                              # read the index into the page cache (safe while serving)
```

`start.sh` loads `.env`, runs `uv sync --frozen` so the environment matches `uv.lock`, checks
Java, the required packages, the index/data paths and the port, then runs
`gunicorn -c src/server/gunicorn.conf.py api:app` via `uv run`. Without uv it falls back to
`$PYTHON` or `.venv/bin/python`. Each worker starts its own JVM and Lucene searcher (~1.3 GB
RSS, ~15 s); the service is ready about 15 s after launch. Server settings (timeouts, logging,
pid file) are in `gunicorn.conf.py`. The header of `start.sh` documents how to reload
(`kill -HUP`), stop and run detached.

Docker (same script inside the image; paths from `.env` mounted at `/data/...`):

```bash
docker compose up -d api
docker compose logs -f api
docker compose restart api        # after editing keys or .env
docker compose down
```

Single process for development (uvicorn):

```bash
uv run uvicorn --app-dir src/server api:app --host 0.0.0.0 --port 8000
```

### Authentication

All search endpoints require `Authorization: Bearer <key>` when keys are configured. Keys come from:

- `~/.scholar/api_keys` if it exists (the default `start.sh` picks up), or `SCHOLAR_API_KEYS_FILE=<path>`:
  one key per line, `#` comments allowed, e.g. `3f9a…   # alice`;
- and/or `SCHOLAR_API_KEYS=key1,key2` in the environment.

Create the file once, outside the repo, with one key per person or service so keys can be revoked
individually; keys are loaded at startup, so restart (or `kill -HUP`) after editing:

```bash
mkdir -p ~/.scholar && chmod 700 ~/.scholar
echo "$(openssl rand -hex 24)   # alice" >> ~/.scholar/api_keys && chmod 600 ~/.scholar/api_keys
```

With no keys from any source the API is open and `start.sh` prints a warning. `/health` and `/` never
need a key. A missing or wrong key returns `401`.

### Endpoints

Common query parameters for `/search` and `/fulltext` (only `q` is required):

| Parameter | Default | Description |
|---|---|---|
| `q` | required | search query |
| `top_k` | 10 | number of docs (1–100) |
| `offset` | 0 | pagination offset; `offset + top_k` ≤ 1000 |
| `index` | `all` | `all` = title + body (title matches weighted 2x), `text` = body only, `title` = title only |

All endpoints are GET. Docs are ordered by relevance. `link` is the DOI resolver URL when a DOI is
known, otherwise an open-access or PDF URL, otherwise `null`.

#### Snippet search: `GET /search`

Extra parameters:

| Parameter | Default | Description |
|---|---|---|
| `context_words` | 20 | words of context before the first and after the last matched word of a passage (1–200) |
| `max_matches` | 5 | passages returned per paper, best first (1–50) |
| `highlight` | false | wrap matched words in `<b>…</b>` |

For each paper, `matches` lists the passages where the query terms occur, best first; `content` is
the best one (same text as `matches[0].content`) and `match_count` is the total number of query-term
occurrences in the paper body. Matches closer than ~300 characters are grouped into one passage;
passages never overlap. A passage's `score` (0–1) is `(distinct query terms it contains + density) /
(query terms + 1)`, where density is the mean of `tf/(tf+1)` over those terms, so one more distinct term
always outweighs repetition. The paper's `score` is its BM25 relevance. Matching is stem-based
("resistance" also matches "resistant"); passages are taken from the paper body, not the author block
or the reference list.

```bash
curl -G http://localhost:8000/search -H "Authorization: Bearer key1" \
  --data-urlencode "q=branched chain amino acid insulin resistance" -d top_k=3 -d context_words=10
```

```json
{
  "searchParameters": {"q": "branched chain amino acid insulin resistance", "top_k": 3, "offset": 0,
                       "index": "all", "context_words": 10, "max_matches": 5, "highlight": false},
  "docs": [
    {
      "title": "The Effects of BCAAs on Insulin Resistance in Athletes",
      "doi": "10.3177/jnsv.65.383",
      "link": "https://doi.org/10.3177/jnsv.65.383",
      "content": "…and hepatic insulin resistance. In adipose tissue, the expression of branched-chain amino acid catabolic enzymes…",
      "matches": [
        {"content": "…and hepatic insulin resistance. In adipose tissue, the expression of branched-chain amino acid catabolic enzymes…", "score": 0.929},
        {"content": "…plasma branched-chain amino acids were elevated in…", "score": 0.571}
      ],
      "match_count": 212,
      "score": 12.15
    }
  ],
  "took_ms": 95
}
```

#### Full-document search: `GET /fulltext`

Extra parameter: `fields`, a comma-separated subset of `title, text, doi, authors, extra`
(all when omitted). `extra` holds `external_ids`, `source` and the parsed `references`.

```bash
curl -G http://localhost:8000/fulltext -H "Authorization: Bearer key1" \
  --data-urlencode "q=graph neural network" -d top_k=2 -d fields=title,doi,authors
```

```json
{
  "searchParameters": {"q": "graph neural network", "top_k": 2, "offset": 0, "index": "all",
                       "fields": ["title", "doi", "authors"]},
  "docs": [
    {"score": 18.2, "title": "...", "doi": "10.1000/xyz", "authors": [...], "link": "https://doi.org/10.1000/xyz"}
  ],
  "took_ms": 40
}
```

#### DOI lookup: `GET /doi/{doi}`

The DOI may carry a `https://doi.org/` or `doi:` prefix; matching is case-insensitive. Returns the
paper's full extracted data: `title`, `text`, `doi`, `authors`, `extra`, `link`.

```bash
curl http://localhost:8000/doi/10.1155/2016/2794591 -H "Authorization: Bearer key1"
```

Status codes: `404` unknown DOI, `503` if the DOI map has not been built yet.

#### Health: `GET /health`

```json
{"status": "healthy", "index_exists": true, "index_path": "...", "data_exists": true,
 "data_path": "...", "searcher_initialized": true, "num_docs": 27700000,
 "doi_map_exists": true, "doi_map_path": "...", "title_weight": 2.0}
```

### Document Fields

- **title**: Paper title
- **text**: Full paper text content
- **doi**: Digital Object Identifier
- **authors**: List of authors
- **extra**: Additional metadata
  - `external_ids`: Dictionary of external identifiers
  - `source`: Source URLs
  - `references`: Bibliography/references

### Python client

```python
import requests

API = "http://localhost:8000"
HEADERS = {"Authorization": "Bearer key1"}

r = requests.get(f"{API}/search", params={"q": "insulin resistance", "top_k": 5}, headers=HEADERS)
for doc in r.json()["docs"]:
    print(doc["title"], doc["link"])
    print("  ", doc["content"])

paper = requests.get(f"{API}/doi/10.1155/2016/2794591", headers=HEADERS).json()
print(paper["title"], len(paper["text"]))
```

`api_client_example.py` is a runnable version: `python src/server/api_client_example.py "machine learning" 5`.

### Error handling

- `401` missing/invalid bearer token
- `422` invalid parameters (missing `q`, `offset + top_k` > 1000, unknown `fields`)
- `404` unknown DOI
- `503` searcher not initialised, or DOI map not built
- A hit whose record cannot be read from the data directory is logged and dropped from the page.

### Tests

```bash
pytest src/server/tests/test_snippets.py                 # unit tests, no index or JVM needed
python src/server/tests/stress_test.py --url http://localhost:8000 --clients 100 --seconds 45
```

### Operations notes

The full performance record (what was tried, measurements, hardware sizing) is in
[docs/OPTIMIZATION.md](../../docs/OPTIMIZATION.md).

- Throughput scales with `WORKERS`: Lucene calls release the GIL, so each worker is limited by its
  Python work per request (~30 ms for parsing ten records and extracting passages). Each worker is
  ~1.3 GB RSS, so the default 16 use ~21 GB.
- Stress test (`tests/stress_test.py`, 100 concurrent clients, 16 workers, index pre-warmed, 32-core box):

  | endpoint | req/s | p50 | p95 | p99 | response |
  |---|---|---|---|---|---|
  | `/search` | 355 | 264 ms | 481 ms | 610 ms | 32 KB |
  | `/fulltext` fields=title,doi,authors | 590 | 145 ms | 362 ms | 489 ms | 7 KB |
  | `/fulltext` all fields | 257 | 379 ms | 577 ms | 668 ms | 699 KB |
  | `/doi/{doi}` | 1964 | 49 ms | 84 ms | 102 ms | 40 KB |

  With fewer clients latency drops accordingly (single client: `/search` ~60–90 ms, `/doi` ~5 ms).
- Per-worker request threads (`SCHOLAR_THREADS`, default 40) make no measurable difference at 100
  clients: throughput is bound by Python CPU across the worker processes, not by thread count.
  At 1,000 concurrent clients throughput stays about the same (`/search` 295 req/s, `/fulltext`
  556, `/doi` 1,818; no errors) but requests queue: `/search` p50 1.4 s, p95 14 s, p99 21 s.
- The two-field index built without term positions is ~60 GB and fits in the page cache, so after
  warm-up nearly every query runs in tens of milliseconds; a cold query is 100–500 ms. Loading ten
  records and extracting passages adds ~50 ms. Workers run a few warm-up queries at startup.
- CORS is open to all origins (configure in `api.py` for production). Put a reverse proxy in front for
  TLS. For a persistent service, run `start.sh` from a systemd unit.

### Docker

`Dockerfile` and `docker-compose.yml` at the repo root build one image (Eclipse Temurin Java 21,
uv-managed Python 3.13, the locked packages) and define the `api` service on it:

- `.env` supplies the **host** paths `INDEX_DIR`, `ORIGINAL_DATA_PATH`, `DOI_MAP_PATH`, which
  compose mounts read-only at `/data/index`, `/data/s2orc_papers`, `/data/doi_map.sqlite`; the
  container-side variables are fixed to those paths.
- Keys: `SCHOLAR_API_KEYS` in `.env`, or `SCHOLAR_API_KEYS_FILE=<host path>`, which is mounted at
  `/run/secrets/api_keys`.
- `PORT` in `.env` is the host port; the container always listens on 8000. `WORKERS` and the
  `SCHOLAR_*` tuning variables pass straight through.
- The service restarts unless stopped, has a `/health` healthcheck with a 3-minute start period
  (JVM start per worker), a 40 s stop grace period for in-flight requests, and a raised open-file
  limit for many concurrent connections.

```bash
cp .env.example .env && $EDITOR .env
docker compose build
docker compose up -d api
curl -s localhost:8000/health
```
