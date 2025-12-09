## S2ORC Search API Server

FastAPI server for searching the Pyserini index and retrieving full documents with scores.

### Prerequisites

Install required packages:

```bash
pip install fastapi uvicorn pyserini ujson
```

### Configuration

The server uses environment variables for configuration. You can set these before running the server:

```bash
export FULL_TEXT_INDEX_DIR="s2orc_index_text"
export TITLE_INDEX_DIR="s2orc_index_title"
export ORIGINAL_DATA_PATH="s2orc_papers"
```

**Environment Variables:**
- `FULL_TEXT_INDEX_DIR` (default: `s2orc_index_text`): Path to full-text Pyserini index
- `TITLE_INDEX_DIR` (default: `s2orc_index_title`): Path to title-only Pyserini index
- `ORIGINAL_DATA_PATH` (default: `s2orc_papers`): Path to original S2ORC data files

### Running the Server

#### Basic Usage

```bash
cd src/server
uvicorn api:app --host 0.0.0.0 --port 8000
```

#### With Custom Paths

```bash
export FULL_TEXT_INDEX_DIR="/path/to/text_index"
export TITLE_INDEX_DIR="/path/to/title_index"
export ORIGINAL_DATA_PATH="/path/to/s2orc_papers"

uvicorn api:app --host 0.0.0.0 --port 8000
```

#### Development Mode (with auto-reload)

```bash
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

### API Endpoints

#### GET /

Health check endpoint.

**Response:**
```json
{
  "status": "running",
  "service": "Pyserini Search API",
  "index": "s2orc_index_text"
}
```

#### GET /search

Search the index and return results with full document metadata.

**Query Parameters:**
- `query` (required): Search query string
- `k` (optional, default: 10): Number of results to return (1-1000)
- `fields` (optional): Comma-separated list of fields to include in response
  - Valid values: `title`, `text`, `doi`, `authors`, `extra`
  - Example: `?fields=title,doi`
  - If not specified, all fields are returned
- `index_type` (optional, default: `text`): Index to search
  - `text`: Full-text search
  - `title`: Title-only search

**Example Requests:**

```bash
# Basic search
curl "http://localhost:8000/search?query=machine+learning&k=10"

# Search with specific fields
curl "http://localhost:8000/search?query=deep+learning&fields=title,doi&k=5"

# Title-only search
curl "http://localhost:8000/search?query=neural+networks&index_type=title&k=20"
```

**Response:**
```json
{
  "query": "machine learning",
  "total_results": 10,
  "results": [
    {
      "score": 15.234,
      "doc_id": "filename.jsonl___12345__v2",
      "document": {
        "title": "Paper Title",
        "text": "Full paper text...",
        "doi": "10.1234/example",
        "authors": ["Author 1", "Author 2"],
        "extra": {
          "external_ids": {...},
          "source": {...},
          "references": [...]
        }
      }
    }
  ]
}
```

#### GET /health

Detailed health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "index_exists": true,
  "index_path": "s2orc_index_text",
  "data_exists": true,
  "data_path": "s2orc_papers",
  "searcher_full_text_initialized": true
}
```

### Document Fields

The API returns documents with the following fields (based on `fields` parameter):

- **title**: Paper title
- **text**: Full paper text content
- **doi**: Digital Object Identifier
- **authors**: List of authors
- **extra**: Additional metadata
  - `external_ids`: Dictionary of external identifiers
  - `source`: Source URLs
  - `references`: Bibliography/references

### API Documentation

Once the server is running, you can access:

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

These provide interactive API documentation where you can test endpoints directly.

### Using with Docker (Optional)

Create a `.env` file:

```env
FULL_TEXT_INDEX_DIR=/data/s2orc_index_text
TITLE_INDEX_DIR=/data/s2orc_index_title
ORIGINAL_DATA_PATH=/data/s2orc_papers
```

Example `docker-compose.yml`:

```yaml
version: '3.8'
services:
  api:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
    volumes:
      - ./s2orc_index_text:/data/s2orc_index_text
      - ./s2orc_index_title:/data/s2orc_index_title
      - ./s2orc_papers:/data/s2orc_papers
```

### Notes

- The server loads indexes into memory at startup for fast search
- Both full-text and title indexes must be available
- Original data files are accessed on-demand to retrieve full document metadata
- CORS is enabled for all origins (modify `allow_origins` in `api.py` for production)
