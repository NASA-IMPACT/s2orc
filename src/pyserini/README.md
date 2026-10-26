## Building Pyserini Index for S2ORC

This directory contains scripts to convert S2ORC data to Pyserini format and build a searchable BM25 index.

### Hardware Requirements

We built this index on a 32-core, 247 GB RAM machine with no GPU and a single NVMe volume. Pyserini
used 50–60 GB of RAM at most; file I/O, not RAM, was the bottleneck throughout.

Indexing (full corpus, 513 files, 27.7M papers):
- RAM: 60–80 GB peak
- Disk: ~4 TB while the intermediate files exist (raw 1.8 TB, converted text ~1 TB, titles 5 GB,
  merged ~1 TB, index 60 GB). The converted collections can be deleted once the index is built.

Production:
- RAM: ~1.3 GB per API worker (16 workers ≈ 21 GB) plus page cache for the index; the 60 GB
  two-field index fits in RAM on this machine, so warm queries never touch disk
- Disk: ~2 TB (raw data 1.8 TB, which the API reads at query time, plus the 60 GB index and the
  1.7 GB DOI map)

Index sizes by build option:

| Build | Size | Use |
|---|---|---|
| two-field (`--fields title --no-positions`) | 60 GB | what the API serves; no phrase queries |
| full text with positions (default flags) | 247 GB | phrase queries from the command-line tool |
| titles only | 3.5 GB | title search without the API |

### Prerequisites

Python packages come from uv (`uv sync` at the repo root, see the
[root README](../../README.md#installation)); Pyserini needs Java 21. The Docker image has both:
run any command below as `docker compose run --rm index <command>` with the data under `/data`.

`build_index.sh` in this directory chains Steps 1–2 plus the DOI map with the paths from `.env`:

```bash
src/pyserini/build_index.sh                 # convert, merge, index, doi
src/pyserini/build_index.sh index doi       # only some steps
```

The end-to-end sequence with sizes and timings is in [SETUP.md](../../SETUP.md).

### Step 1: Convert S2ORC Data to Pyserini Format

You can index either the full text or just the titles of papers. Choose one:

#### Option A: Convert Full Text Content

```bash
python src/pyserini/convert_data_text.py \
  --input-path s2orc_papers \
  --output-path s2orc_pyserini_text \
  --num-workers 8
```

#### Option B: Convert Title Content Only

```bash
python src/pyserini/convert_data_title.py \
  --input-path s2orc_papers \
  --output-path s2orc_pyserini_title \
  --num-workers 8
```

**Command-line Arguments:**
- `--input-path` (required): Path to downloaded S2ORC files
- `--output-path` (optional): Output directory for converted files (default: `s2orc_pyserini_text` or `s2orc_pyserini_title`)
- `--num-workers` (optional): Number of parallel workers (default: 8)

**Output:**
The scripts convert S2ORC data files to Pyserini JSONL format:
```json
{"id": "filename___offset__version", "contents": "document text or title"}
```

#### Option C (recommended for the API server): Merge Text and Title into One Two-Field Collection

The search API in `src/server` searches title and body together with a title boost, which needs
**one** index with a separate `title` field. Run both conversions above, then merge them:

```bash
python src/pyserini/convert_data_merge.py \
  --text-path s2orc_pyserini_text \
  --title-path s2orc_pyserini_title \
  --output-path s2orc_pyserini_text_title \
  --num-workers 8
```

**Command-line Arguments:**
- `--text-path` (optional): Converted full-text files from Option A (default: `data/s2orc_pyserini_text`)
- `--title-path` (optional): Converted title files from Option B (default: `data/s2orc_pyserini_title`)
- `--output-path` (optional): Output directory (default: `data/s2orc_pyserini_text_title`)
- `--num-workers` (optional): Number of parallel workers (default: 8)
- `--limit` (optional): Only merge the first N files (for testing)

The merge is resumable (files already present in the output directory are skipped) and documents
are matched by `id`. Output:
```json
{"id": "filename___offset__version", "title": "paper title", "contents": "full text"}
```

### Step 2: Build Pyserini Index

After converting the data, build the Lucene index:

```bash
python src/pyserini/build_pyserini_index.py \
  --input-path s2orc_pyserini_text \
  --index-path s2orc_index \
  --threads 20
```

For the merged two-field collection (Option C), index the `title` key as its own field. Positions
can be dropped too: the API does not use phrase queries and the index shrinks from ~250 GB to ~60 GB
for the full corpus, small enough to sit in the page cache:

```bash
python src/pyserini/build_pyserini_index.py \
  --input-path s2orc_pyserini_text_title \
  --index-path s2orc_index \
  --fields title \
  --no-positions \
  --threads 20
```

**Command-line Arguments:**
- `--input-path` (required): Path to converted JSONL files from Step 1
- `--index-path` (required): Output path for Pyserini index
- `--threads` (optional): Number of indexing threads (default: 20)
- `--fields` (optional): Extra JSON keys to index as separate Lucene fields besides `contents` (use `--fields title` for the merged collection)
- `--no-positions` (optional): Do not store term positions (smaller index; phrase queries unsupported)

**What this does:**
- Creates a Lucene index optimized for BM25 search
- Stores term positions for phrase queries (unless `--no-positions`)
- Does NOT store raw documents to save space

### Step 3: Search the Index

#### Interactive Search Mode

```bash
python src/pyserini/search_pyserini.py \
  --index-path s2orc_index \
  --data-path s2orc_papers
```

This launches an interactive prompt where you can enter queries. The tool searches the default
`contents` field, so it works with any of the index builds above.

#### Command-line Query Mode

```bash
python src/pyserini/search_pyserini.py \
  --index-path s2orc_index \
  --data-path s2orc_papers \
  --query "machine learning" \
  -k 20
```

**Command-line Arguments:**
- `--index-path` (required): Path to Pyserini index
- `--data-path` (required): Path to original S2ORC data files
- `--query` (optional): Search query (if not provided, runs interactive mode)
- `-k` (optional): Number of results to return (default: 10)
- `--no-metadata` (optional): Skip retrieving full metadata from original files

**How it works:**
1. Searches the Pyserini index using BM25
2. Returns document IDs and scores
3. Retrieves full metadata from original files using document ID offsets
4. Displays title, corpus ID, DOI, and ArXiv ID for each result

### Step 4: Append New Documents to Existing Index (Optional)

If you need to add new documents to an existing index without rebuilding from scratch:

```bash
python src/pyserini/append_to_pyserini_index.py \
  --index s2orc_index \
  --input new_documents.jsonl \
  --threads 8
```

**Command-line Arguments:**
- `--index` (required): Path to the existing Pyserini index
- `--input` (required): Path to new JSONL file with documents to add
- `--threads` (optional): Number of indexing threads (default: 1)

**Use case:**
- Add newly downloaded S2ORC papers to an existing index
- Incrementally update the index without rebuilding everything
- The new documents must be in the same JSONL format as Step 1

### Complete Workflow Example

```bash
# Step 1: Convert data (text and title), then merge into one two-field collection
python src/pyserini/convert_data_text.py \
  --input-path s2orc_papers \
  --output-path s2orc_pyserini_text
python src/pyserini/convert_data_title.py \
  --input-path s2orc_papers \
  --output-path s2orc_pyserini_title
python src/pyserini/convert_data_merge.py \
  --text-path s2orc_pyserini_text \
  --title-path s2orc_pyserini_title \
  --output-path s2orc_pyserini_text_title

# Step 2: Build the two-field index (what src/server/api.py expects)
python src/pyserini/build_pyserini_index.py \
  --input-path s2orc_pyserini_text_title \
  --index-path s2orc_index \
  --fields title --no-positions

# Step 3: Search
python src/pyserini/search_pyserini.py \
  --index-path s2orc_index \
  --data-path s2orc_papers \
  --query "deep learning transformers"

# Step 4 (Optional): Append new documents later
python src/pyserini/append_to_pyserini_index.py \
  --index s2orc_index \
  --input new_documents.jsonl
```

### Directory Structure After Setup

```
.
├── s2orc_papers/                    # Original downloaded data
├── s2orc_papers_metadata/           # Download metadata
├── s2orc_pyserini_text/            # Converted JSONL files (full text)
├── s2orc_pyserini_title/           # Converted JSONL files (titles)
├── s2orc_pyserini_text_title/      # Merged two-field collection
├── s2orc_index/                    # Pyserini index
└── doi_map.sqlite                  # DOI lookup map for the API (src/server/build_doi_map.py)
```

### Notes

- The conversion process extracts either full text or titles from both S2ORC v1 and v2 formats
- Document IDs encode the filename and byte offset for efficient retrieval
- The index does not store raw documents to save space
- Full document metadata is retrieved from original files during search
- Processing and indexing can be resource-intensive; adjust workers/threads based on available CPU and memory
