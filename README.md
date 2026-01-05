## Building S2ORC Papers Index

This project provides tools to download, process, and index the S2ORC dataset for efficient search.

### Installation

#### Using uv (Recommended)

We use [uv](https://github.com/astral-sh/uv) as our Python package manager for faster installations:

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create a virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

#### Using pip

Alternatively, you can use pip:

```bash
pip install -r requirements.txt
```

#### Java Installation

**Note:** Pyserini requires Java to be installed separately:

```bash
# Ubuntu/Debian
sudo apt-get install default-jdk

# macOS
brew install openjdk

# Verify installation
java -version
```

### Workflow

1. [Download the Dataset](src/s2orc/README.md) - Download S2ORC data from Semantic Scholar
2. [Build the Index](src/pyserini/README.md) - Convert data and build Pyserini search index
3. [Run the FastAPI Server](src/server/README.md) - Set up API for searching the index

### Quick Start

#### Option 1: End-to-End Script (Recommended)

The easiest way to get started is using the automated end-to-end script:

```bash
# Initial setup - builds everything from scratch
python make_index.py

# Incremental update - only downloads and indexes new files
python make_index.py --update

# Build/update and upload to S3
python make_index.py --upload
python make_index.py --update --upload
```

See [UPDATE_WORKFLOW.md](UPDATE_WORKFLOW.md) for update mode details.
See [S3_UPLOAD_GUIDE.md](S3_UPLOAD_GUIDE.md) for S3 configuration and naming conventions.

#### Option 2: Manual Steps

All data is stored in the `data/` directory by default (already in `.gitignore`):

```bash
# 1. Download the dataset
python src/s2orc/download_data.py --api-key YOUR_API_KEY

# 2. Decompress files
bash src/s2orc/decompress_sequential.sh data/s2orc_papers

# 3. Convert data to Pyserini format
python src/pyserini/convert_data_text.py

# 4. Build the search index
python src/pyserini/build_pyserini_index.py

# 5. Search the index
python src/pyserini/search_pyserini.py
```

**Note:** Steps 3-5 use default paths under `data/` directory. All arguments are optional if you use the defaults.

For detailed instructions and custom configurations, see the README files in each subdirectory.

### S3 Index Distribution

If you have pre-built indexes in S3, you can download and use them directly:

```bash
# Configure S3 in .env file
S3_BUCKET=your-bucket-name
S3_PREFIX=s2orc-indexes/
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret

# Run setup - will auto-detect and download from S3
python make_index.py
```

The script automatically:
- Detects pre-built indexes in S3
- Downloads and extracts them
- Uses size-based validation to ensure text/title indexes aren't swapped
- Handles any directory naming inside tar.gz files

#### S3 Naming Convention

When uploading indexes to S3, use this structure:
```
s3://bucket/prefix/YYYY-MM-DD_HHMMSS/
├── s2orc_index_text.tar.gz     # Full-text index (always .tar.gz)
├── s2orc_index_title.tar.bz2   # Title index (always .tar.bz2)
└── metadata.json               # Version metadata
```

See [S3_UPLOAD_GUIDE.md](S3_UPLOAD_GUIDE.md) for complete details on:
- Automated upload with `--upload` flag
- Manual upload procedures
- Version management
- Cost optimization
