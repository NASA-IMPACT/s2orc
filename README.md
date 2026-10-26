## S2ORC Scholar Search

Tools to download the [S2ORC](https://github.com/allenai/s2orc) full-text corpus from Semantic
Scholar, build a Pyserini (Lucene) BM25 index over it, and serve a search API on top:

- **Snippet search** (`GET /search`): the top papers for a query, each with the passages where the
  query terms occur.
- **Full-document search** (`GET /fulltext`): the same ranking with the extracted fields
  (title, text, DOI, authors, references).
- **DOI lookup** (`GET /doi/{doi}`): one paper by DOI.

The index covers 27.7M full-text papers (title and body as separate fields) and is served by
gunicorn with bearer-token authentication.

### Repository layout

| Path | Contents |
|---|---|
| [`src/s2orc/`](src/s2orc/README.md) | download, verify and decompress the S2ORC dataset |
| [`src/pyserini/`](src/pyserini/README.md) | convert the data to Pyserini documents, build and query the Lucene index; `build_index.sh` runs the whole pipeline |
| [`src/server/`](src/server/README.md) | the Scholar Search API: FastAPI app, snippet extraction, DOI map builder, `start.sh` launcher, tests |
| `pyproject.toml`, `uv.lock` | Python dependencies, managed with [uv](https://docs.astral.sh/uv/) (`src/requirements.txt` is exported from them for plain pip) |
| `Dockerfile`, `docker-compose.yml` | one image with Java 21 + uv; an `index` service for building and an `api` service for serving |
| `.env.example` | every setting: where the index, data and DOI map live, port, workers, API keys |
| [`SETUP.md`](SETUP.md) | end-to-end setup guide with sizes, timings and operations notes |
| [`docs/OPTIMIZATION.md`](docs/OPTIMIZATION.md) | performance work, measurements and hardware sizing for serving |
| [`docs/plans/`](docs/plans/README.md) | design notes for the API |
| `docs/v1.json`, `docs/v2.json` | one sample record of each S2ORC record format |

All data, indexes and the DOI map live under `data/` by default (git-ignored); `.env` or the
scripts' flags put them elsewhere.

### Hardware requirements

Two different jobs, two different machines. Building the index is a one-off batch job that
needs lots of disk and a fair amount of RAM; serving needs enough RAM to keep the index in
memory and a few cores per worker. The same box can do both, but a serving box does not need
the build headroom.

**Building the index** (full corpus, 513 files, 27.7M papers):

| Resource | Minimum | Comfortable | Why |
|---|---|---|---|
| Disk | 2.5 TB | 4 TB | 1.8 TB raw data, ~2 TB converted collections (deletable after the build), 60 GB index. With 2.5 TB, convert and index in batches or delete intermediates as you go |
| RAM | 96 GB | 128 GB+ | the Lucene build peaks at 60–80 GB |
| CPU | 8 cores | 16–32 cores | conversion runs 8 worker processes, indexing 20 threads; more cores shorten the ~12 h of batch work |
| Disk speed | SSD | NVMe or gp3 at 250 MB/s+ | every step is bound by sequential read/write of the 1.8 TB |
| Time | | ~1 day | download (bandwidth-bound) + convert and merge ~5 h + index ~3 h + DOI map ~4 h |

**Serving** (what the API reads: 1.8 TB raw data, 60 GB index, 1.7 GB DOI map):

| Resource | Minimum | Recommended | Why |
|---|---|---|---|
| RAM | 64 GB | 128 GB | the 60 GB index must stay in the page cache to avoid 1–2 s cold queries; each worker adds ~1.3 GB. 64 GB serves a few users with cold-cache stalls; 128 GB holds the index plus 8–16 workers |
| CPU | 4 vCPU | 16–32 vCPU | ~22 `/search` req/s per worker; run one worker per two vCPUs (`WORKERS`). 16 workers ≈ 355 req/s |
| Disk | 2 TB | 2.5 TB SSD, 6,000 IOPS+ | results are random 50–500 KB record reads from the raw data; IOPS matter more than bandwidth |
| Network | 1 Gbit/s | | responses are 7–700 KB |

On AWS that is roughly an `r6i.4xlarge` (16 vCPU, 128 GB) for a small team or an
`m6i.8xlarge` (32 vCPU, 128 GB) for the measured throughput, each with a gp3 volume. The raw
`.jsonl` files are needed at serving time: the index stores no text, and every search reads the
matching records back from disk by byte offset. Sizing details and the measurements behind them
are in [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md#7-hardware-requirements-serving-only-no-index-builds).

For reference, everything in this repo was built and measured on one AWS `r6i.8xlarge`:
32 vCPU, 247 GB RAM, a single 5 TB gp3 volume (~125 MB/s sustained), no GPU. That machine is
larger than needed; about 150 GB of its RAM sat unused while serving.

### Installation

Everything Python comes from **uv** (`pyproject.toml` + `uv.lock`, Python 3.13); Lucene needs
**Java 21**. You can install both on the machine, or use the Docker image, which contains both.
Either way, configuration is the `.env` file:

```bash
cp .env.example .env     # then edit: INDEX_DIR, ORIGINAL_DATA_PATH, DOI_MAP_PATH, WORKERS, SCHOLAR_API_KEYS, ...
```

Every script (`build_index.sh`, `start.sh`, `cache_warm.sh`) and docker compose read it;
variables set in the shell take precedence.

#### Option A: on the machine (uv + Java 21)

```bash
# uv (downloads a managed Python 3.13 for the project when none is present)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Python environment: creates .venv from uv.lock (exact versions, including a CPU-only torch)
uv sync

# Java 21
sudo yum install -y java-21-amazon-corretto-devel        # Amazon Linux 2023 / RHEL / Fedora
sudo alternatives --set java /usr/lib/jvm/java-21-amazon-corretto.x86_64/bin/java
sudo apt-get install -y openjdk-21-jdk                   # Ubuntu / Debian
brew install openjdk@21                                  # macOS
java -version

# optional: parallel gzip for faster decompression of the downloads
sudo yum install -y pigz        # or apt-get install pigz / brew install pigz
```

Run things with `uv run python ...` or activate the venv (`source .venv/bin/activate`). The
launcher scripts call uv themselves. Without uv, `pip install -r src/requirements.txt` into a
Python 3.13 environment gives the same package set, and the scripts fall back to
`.venv/bin/python` or `$PYTHON`.

To change dependencies, edit `pyproject.toml`, then:

```bash
uv lock && uv sync
uv export --frozen --no-hashes --no-dev --no-header > src/requirements.txt   # keep the pip file in step
```

#### Option B: Docker (Java 21 and uv inside the image)

Needs Docker with the compose plugin. The image is built from `Dockerfile`: Eclipse Temurin
Java 21, uv-managed Python 3.13 and the locked packages; nothing is downloaded at run time.

```bash
cp .env.example .env         # host paths of the data, index and DOI map; keys; workers
docker compose build         # once
```

`docker-compose.yml` defines two services from that image:

| Service | Purpose | Run |
|---|---|---|
| `api` | serve the API on an existing index | `docker compose up -d api` |
| `index` | shell with every build script, for creating the index | `docker compose run --rm index src/pyserini/build_index.sh` |

Inside the containers the data is always at `/data/...`; the host directories named in `.env`
are mounted there, so no path in the code changes between host and container.

### 1. Building the index: what you need

| Requirement | Detail |
|---|---|
| Semantic Scholar API key | for the download, https://www.semanticscholar.org/product/api |
| Disk | ~4 TB while building (1.8 TB raw, ~2 TB converted collections, 60 GB index); ~1.9 TB kept afterwards |
| RAM | 60–80 GB peak during the Lucene build |
| Time | download hours (bandwidth-bound), convert + merge ~5 h, index ~3 h, DOI map ~4 h |
| Software | Java 21 + uv environment (Option A) or the `index` Docker service (Option B) |

The pipeline, from the repo root:

```bash
# a) download and decompress (see src/s2orc/README.md)
uv run python src/s2orc/download_data.py --api-key "$S2_API_KEY" --local-path data/s2orc_papers
bash src/s2orc/decompress_sequential.sh data/s2orc_papers

# b) convert -> merge -> index -> DOI map, in one go (or pass a subset of the step names)
src/pyserini/build_index.sh
src/pyserini/build_index.sh index doi
```

With Docker, the same inside the `index` service (the host `DATA_DIR` from `.env` is mounted
at `/data`):

```bash
docker compose run --rm index uv run python src/s2orc/download_data.py --api-key "$S2_API_KEY" --local-path /data/s2orc_papers
docker compose run --rm index bash src/s2orc/decompress_sequential.sh /data/s2orc_papers
docker compose run --rm index src/pyserini/build_index.sh
```

Outputs: `data/s2orc_index` (the index), `data/doi_map.sqlite` (DOI map), and the converted
collections under `data/s2orc_pyserini_*`, which can be deleted once the index exists. Each
step, its flags and what it produces are described in
[src/pyserini/README.md](src/pyserini/README.md); the full walk-through with sizes is
[SETUP.md](SETUP.md).

### 2. Serving an index

Three artifacts are needed, and nothing else from the build:

| Artifact | `.env` variable | Size |
|---|---|---|
| the two-field index | `INDEX_DIR` | 60 GB |
| the decompressed `.jsonl` files, with the same file names as when the index was built | `ORIGINAL_DATA_PATH` | 1.8 TB |
| the DOI map (optional; `/doi` returns 503 without it) | `DOI_MAP_PATH` | 1.7 GB |

```bash
# keys: one per caller, outside the repo (or SCHOLAR_API_KEYS=key1,key2 in .env)
mkdir -p ~/.scholar && echo "$(openssl rand -hex 24)   # me" >> ~/.scholar/api_keys && chmod 600 ~/.scholar/api_keys

# Option A: on the machine. start.sh syncs the uv environment, checks Java, paths and port, runs gunicorn.
src/server/start.sh                       # or: nohup src/server/start.sh > server.log 2>&1 &

# Option B: Docker
docker compose up -d api
docker compose logs -f api

curl -s localhost:8000/health
```

The service is ready about 15 s after launch (each worker starts a JVM). Reload, stop, cache
warming and load testing are in [src/server/README.md](src/server/README.md#running) and
[SETUP.md](SETUP.md#9-operations).

### Using the API

```bash
KEY=$(head -1 ~/.scholar/api_keys | cut -d' ' -f1)
curl -G localhost:8000/search -H "Authorization: Bearer $KEY" --data-urlencode "q=insulin resistance" -d top_k=3
curl -G localhost:8000/fulltext -H "Authorization: Bearer $KEY" --data-urlencode "q=graph neural network" -d fields=title,doi
curl localhost:8000/doi/10.1155/2016/2794591 -H "Authorization: Bearer $KEY"
```

Interactive docs are at `http://<host>:8000/docs`. Endpoint parameters, response formats, auth,
operations and load-test numbers are in [src/server/README.md](src/server/README.md).

### Tests

```bash
uv run pytest                                                  # unit tests, no index or JVM needed
uv run python src/server/tests/stress_test.py --url http://localhost:8000   # load test against a running server
```
