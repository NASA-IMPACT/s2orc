# Setup: from raw S2ORC data to a running Scholar Search API

This is the end-to-end path with the sizes and durations we measured for the full corpus
(513 files, 27.7M papers) on a 32-core, 247 GB RAM machine with one NVMe volume. Each step links
to the README with the full flag reference. All commands run from the repo root and use the
default locations under `data/`; pass the flags or environment variables noted in each step to
put things elsewhere.

Plan for about **4 TB of disk** if you keep the intermediate files, or **2 TB** if you delete the
converted collections after the index is built (see step 5).

Steps 3, 4 and 6 are also wrapped in one script, `src/pyserini/build_index.sh`, which runs
convert → merge → index → DOI map with the paths from `.env`; the individual commands are shown
here so that each step can be understood and re-run on its own.

## 1. Prerequisites

Either install the tools on the machine, or build the Docker image, which contains them all
(details in [README.md](README.md#installation)):

```bash
# --- on the machine ---
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv; brings its own Python 3.13
uv sync                                                  # .venv from uv.lock
sudo yum install -y java-21-amazon-corretto-devel        # Java 21 for Pyserini/Lucene (Amazon Linux 2023;
sudo alternatives --set java /usr/lib/jvm/java-21-amazon-corretto.x86_64/bin/java   # other systems: README.md)
java -version
sudo yum install -y pigz                                 # optional: parallel gzip for decompression

# --- or Docker ---
docker compose build                                     # Java 21 + uv + packages in one image
```

Then copy `.env.example` to `.env` and set `DATA_DIR` (where everything below is written) and, if
they differ from the defaults under it, `INDEX_DIR`, `ORIGINAL_DATA_PATH` and `DOI_MAP_PATH`. All
scripts and docker compose read `.env`.

With Docker, prefix every `python`/`bash` command below with `docker compose run --rm index`
and use `/data/...` for the paths (the host `DATA_DIR` is mounted there), e.g.
`docker compose run --rm index src/pyserini/build_index.sh`.

A Semantic Scholar API key is needed for the download: https://www.semanticscholar.org/product/api.

## 2. Download and decompress the dataset

Details: [src/s2orc/README.md](src/s2orc/README.md).

```bash
python src/s2orc/download_data.py --api-key YOUR_API_KEY --local-path data/s2orc_papers --max-workers 10
python src/s2orc/verify_downloads.py --local-path data/s2orc_papers        # re-run verification any time
bash src/s2orc/decompress_sequential.sh data/s2orc_papers                  # one file at a time
```

Result: `data/s2orc_papers/*.jsonl`, 513 files, **~1.8 TB** decompressed. The download metadata
(link list, success/failure logs) is in `data/s2orc_papers_metadata/`.

Keep these files: the index stores no text, so the API reads every result back from them by
byte offset.

The corpus mixes two record formats, `v1` (older, `body.text` + `openaccessinfo`) and `v2`
(`content.text` + character-offset annotations). `docs/v1.json` and `docs/v2.json` hold one
sample of each; the conversion and server code handle both.

## 3. Convert to Pyserini documents

Details: [src/pyserini/README.md](src/pyserini/README.md).

```bash
python src/pyserini/convert_data_text.py    --input-path data/s2orc_papers --output-path data/s2orc_pyserini_text  --num-workers 8
python src/pyserini/convert_data_title.py   --input-path data/s2orc_papers --output-path data/s2orc_pyserini_title --num-workers 8
python src/pyserini/convert_data_merge.py   --text-path data/s2orc_pyserini_text --title-path data/s2orc_pyserini_title \
                                            --output-path data/s2orc_pyserini_text_title --num-workers 8
```

| Output | Size | Content |
|---|---|---|
| `data/s2orc_pyserini_text/` | ~1 TB | `{"id", "contents": <full text>}` |
| `data/s2orc_pyserini_title/` | ~5 GB | `{"id", "contents": <title>}` |
| `data/s2orc_pyserini_text_title/` | ~1 TB | `{"id", "title", "contents"}`, what the index is built from |

Each document id is `<file name>___<byte offset>__<v1|v2>`, which is how the server finds the
original record again. The merge is resumable; files already present in its output are skipped.

## 4. Build the index

```bash
python src/pyserini/build_pyserini_index.py \
  --input-path data/s2orc_pyserini_text_title \
  --index-path data/s2orc_index \
  --fields title --threads 20
```

Result: `data/s2orc_index`, **~60 GB**, 27.7M documents with two Lucene fields, `contents`
(body) and `title`. Indexing is bound by disk reads; RAM peaks at 60–80 GB.

Why these flags:

- `--fields title` indexes the `title` key as its own field, so the API can search title and body
  together with a title boost (`index=all`), or either alone.
- Term positions are left out by default. The API does not use phrase queries, and the index
  shrinks from ~247 GB to ~60 GB, small enough to sit entirely in the page cache so warm queries
  take tens of milliseconds. Add `--positions` if you want phrase queries from the command-line tool.

Check it:

```bash
python src/pyserini/search_pyserini.py --index-path data/s2orc_index --data-path data/s2orc_papers --query "insulin resistance" -k 5
```

## 5. Reclaim disk (optional)

Once the index exists, the three converted collections from step 3 (~2 TB) are no longer needed
by anything. Keep `data/s2orc_papers` (raw records) and `data/s2orc_index`.

## 6. Build the DOI map

Details: [src/server/README.md](src/server/README.md#setup).

```bash
python src/server/build_doi_map.py --data-path data/s2orc_papers --out data/doi_map.sqlite
```

Result: `data/doi_map.sqlite`, **~1.7 GB**, DOI → (file, offset) for the `/doi/{doi}` endpoint.
The scan reads all 1.8 TB once at disk speed, **~4–5 hours**; `--limit 2` tests it on two files
in under a minute. The API starts without it and answers `503` on `/doi` until the file exists.

If you have a file listing which raw files are `v1` and which `v2` (`{"v1": [...], "v2": [...]}`),
pass it with `--classification` so papers present in both formats keep their richer `v2` record.

## 7. API keys

Keys live outside the repo, one per person or service:

```bash
mkdir -p ~/.scholar && chmod 700 ~/.scholar
echo "$(openssl rand -hex 24)   # alice" >> ~/.scholar/api_keys
chmod 600 ~/.scholar/api_keys
```

`start.sh` reads `~/.scholar/api_keys` by default; `SCHOLAR_API_KEYS_FILE=<path>` or
`SCHOLAR_API_KEYS=key1,key2` override it. With no keys configured the API is open and the launcher
prints a warning.

## 8. Run the API

```bash
./start.sh                       # 0.0.0.0:8000, 16 workers
PORT=8001 WORKERS=4 ./start.sh   # other port / fewer workers
docker compose up -d api                  # the same, in Docker
```

Paths come from `.env` (or the environment, which wins) when the defaults don't apply:

```bash
INDEX_DIR=/mnt/idx/s2orc_index
ORIGINAL_DATA_PATH=/mnt/data/s2orc_papers
DOI_MAP_PATH=/mnt/idx/doi_map.sqlite
```

The launcher syncs the uv environment from `uv.lock`, checks Java, packages, paths and port,
then runs gunicorn through `uv run`. Each worker starts its own JVM (~1.3 GB RSS, ~15 s), so
the service is up about 15 s after launch. In Docker the same script is the container command;
the `.env` host paths are mounted read-only at `/data/...`.

Verify:

```bash
curl -s localhost:8000/health
KEY=$(head -1 ~/.scholar/api_keys | cut -d' ' -f1)
curl -G localhost:8000/search -H "Authorization: Bearer $KEY" --data-urlencode "q=insulin resistance" -d top_k=3
```

## 9. Operations

All from the repo root; the pid file is `src/server/gunicorn-<PORT>.pid`.

| Task | Command |
|---|---|
| Run detached | `nohup ./start.sh > server.log 2>&1 &` (Docker: `docker compose up -d api`, logs with `docker compose logs -f api`) |
| Reload code or keys (zero downtime) | `kill -HUP "$(cat src/server/gunicorn-8000.pid)"` |
| Stop gracefully | `kill -TERM "$(cat src/server/gunicorn-8000.pid)"` |
| Warm the page cache after a reboot or big disk job | `nohup src/server/cache_warm.sh > cache_warm.log 2>&1 &` (~8 min cold, safe while serving), or start with `./start.sh --cache-warm` |
| Load test | `python src/server/tests/stress_test.py --url http://localhost:8000 --clients 100 --seconds 45` |
| Unit tests | `pytest src/server/tests/test_snippets.py` |

For a persistent service run `start.sh` from a systemd unit and put a reverse proxy in front for
TLS. Throughput and latency figures are in
[src/server/README.md](src/server/README.md#operations-notes); how they were reached, and how to
size a serving machine, is in [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md).

## Adding newer papers later

Convert the new `.jsonl` files with the step 3 scripts into a separate directory, merge them, and
append to the existing index instead of rebuilding:

```bash
python src/pyserini/append_to_pyserini_index.py --index data/s2orc_index --input new_docs.jsonl --threads 8
```

Then rebuild the DOI map (step 6) so the new papers resolve by DOI, and `kill -HUP` the server
so workers reopen the index.
