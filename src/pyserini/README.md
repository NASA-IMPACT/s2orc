## Building Pyserini Index for S2ORC

This directory contains scripts to convert S2ORC data to Pyserini format and build a searchable BM25 index.

### Prerequisites

Install Pyserini and dependencies:

```bash
pip install pyserini ujson tqdm
```

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

### Step 2: Build Pyserini Index

After converting the data, build the Lucene index:

```bash
python src/pyserini/build_pyserini_index.py \
  --input-path s2orc_pyserini_text \
  --index-path s2orc_index \
  --threads 20
```

**Command-line Arguments:**
- `--input-path` (required): Path to converted JSONL files from Step 1
- `--index-path` (required): Output path for Pyserini index
- `--threads` (optional): Number of indexing threads (default: 20)

**What this does:**
- Creates a Lucene index optimized for BM25 search
- Stores term positions for phrase queries
- Does NOT store raw documents to save space

### Step 3: Search the Index

#### Interactive Search Mode

```bash
python src/pyserini/search_pyserini.py \
  --index-path s2orc_index \
  --data-path s2orc_papers
```

This launches an interactive prompt where you can enter queries.

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
# Step 1: Convert data (choose text or title)
python src/pyserini/convert_data_text.py \
  --input-path s2orc_papers \
  --output-path s2orc_pyserini_text

# Step 2: Build index
python src/pyserini/build_pyserini_index.py \
  --input-path s2orc_pyserini_text \
  --index-path s2orc_index

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
├── s2orc_pyserini_text/            # Converted JSONL files
└── s2orc_index/                    # Pyserini index
```

### Notes

- The conversion process extracts either full text or titles from both S2ORC v1 and v2 formats
- Document IDs encode the filename and byte offset for efficient retrieval
- The index does not store raw documents to save space
- Full document metadata is retrieved from original files during search
- Processing and indexing can be resource-intensive; adjust workers/threads based on available CPU and memory
