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
| [`src/pyserini/`](src/pyserini/README.md) | convert the data to Pyserini documents, build and query the Lucene index |
| [`src/server/`](src/server/README.md) | the Scholar Search API: FastAPI app, snippet extraction, DOI map builder, gunicorn launcher, tests |
| [`SETUP.md`](SETUP.md) | end-to-end setup guide with sizes, timings and operations notes |
| [`docs/plans/`](docs/plans/README.md) | design notes for the API |
| `docs/v1.json`, `docs/v2.json` | one sample record of each S2ORC record format |
| `src/requirements.txt` | Python dependencies |

All data, indexes and the DOI map live under `data/` by default (git-ignored). Every script takes
flags or environment variables to put them elsewhere.

### Hardware requirements

Measured on a 32-core, 247 GB RAM machine with a single 5 TB NVMe volume:

| Phase | RAM | Disk | Notes |
|---|---|---|---|
| Download + decompress | few GB | ~1.8 TB for the decompressed `.jsonl` files | disk-bound |
| Convert + merge | few GB per worker | ~2 TB of intermediate files (full text ~1 TB, titles 5 GB, merged ~1 TB) | can be deleted after indexing |
| Index build | 60–80 GB peak | 60 GB (two-field, no positions) or 247 GB (with positions) | file I/O is the bottleneck, not RAM |
| DOI map | < 1 GB | 1.7 GB SQLite file | ~4–5 h, reads all raw data once |
| Serving | ~1.3 GB per worker + page cache | raw data (1.8 TB) + index (60 GB) + DOI map | 16 workers ≈ 21 GB; the 60 GB index fits in the page cache |

The raw `.jsonl` files are needed at serving time: the index stores no text, and every search
reads the matching records back from disk by byte offset.

### Installation

#### Python environment

We use [uv](https://github.com/astral-sh/uv) for fast installs; plain `pip` works too.

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create a virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -r src/requirements.txt

# or with pip
pip install -r src/requirements.txt
```

Python 3.10 or newer is required (the server code uses modern typing). `src/server/start.sh`
picks up `.venv/bin/python` at the repo root automatically.

#### Java 21

Pyserini runs Lucene inside a JVM and needs Java 21:

```bash
# Amazon Linux 2023 / RHEL / Fedora
sudo yum install -y java-21-amazon-corretto-devel
sudo alternatives --set java /usr/lib/jvm/java-21-amazon-corretto.x86_64/bin/java

# Ubuntu / Debian
sudo apt-get install openjdk-21-jdk

# macOS
brew install openjdk@21

# Verify installation
java -version
```

#### pigz (optional)

`pigz` speeds up decompression of the downloaded archives; the scripts fall back to `gunzip`
without it. See [src/s2orc/README.md](src/s2orc/README.md).

### Workflow

1. [Download the dataset](src/s2orc/README.md): fetch and verify the S2ORC archives, decompress
   them to `.jsonl`.
2. [Build the index](src/pyserini/README.md): convert full text and titles to Pyserini documents,
   merge them into one two-field collection, build the Lucene index.
3. [Run the Scholar Search API](src/server/README.md): build the DOI map, configure API keys,
   serve with gunicorn.

[SETUP.md](SETUP.md) walks through all three with expected sizes and durations.

### Quick start

```bash
# 1. Download the dataset (compressed archives + metadata)
python src/s2orc/download_data.py --api-key YOUR_API_KEY --local-path data/s2orc_papers

# 2. Decompress to .jsonl (one file at a time, safe on limited disk)
bash src/s2orc/decompress_sequential.sh data/s2orc_papers

# 3. Convert to Pyserini documents (full text and titles), then merge into one two-field collection
python src/pyserini/convert_data_text.py
python src/pyserini/convert_data_title.py
python src/pyserini/convert_data_merge.py

# 4. Build the index (title as its own field; term positions are not needed by the API)
python src/pyserini/build_pyserini_index.py --input-path data/s2orc_pyserini_text_title --fields title --no-positions

# 5. Try it from the command line
python src/pyserini/search_pyserini.py --query "machine learning"

# 6. Build the DOI map used by /doi/{doi} (one-off, hours; the API runs without it)
python src/server/build_doi_map.py

# 7. Create an API key and serve
mkdir -p ~/.scholar && echo "$(openssl rand -hex 24)   # me" >> ~/.scholar/api_keys && chmod 600 ~/.scholar/api_keys
src/server/start.sh
curl -s localhost:8000/health
```

Steps 3–7 use the default paths under `data/`; every flag is optional when the defaults are used.
Run the commands from the repo root.

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
pytest src/server/tests/test_snippets.py                       # unit tests, no index or JVM needed
python src/server/tests/stress_test.py --url http://localhost:8000   # load test against a running server
```
