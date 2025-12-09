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
3. [Run the FastAPI Server](src/api/README.md) - Set up API for searching the index

### Quick Start

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
