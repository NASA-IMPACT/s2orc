#!/usr/bin/env python3
"""
Scholar Search: FastAPI server over a Pyserini (Lucene) BM25 index of S2ORC papers.

Usage (from the repo root):
    src/server/start.sh                                   # gunicorn, see src/server/README.md
    uvicorn --app-dir src/server api:app --host 0.0.0.0   # single dev process

Paths (environment variables, defaults relative to the repo root):
    INDEX_DIR            Lucene index with "contents" and "title" fields   (default data/s2orc_index)
    ORIGINAL_DATA_PATH   decompressed S2ORC .jsonl files                   (default data/s2orc_papers)
    DOI_MAP_PATH         SQLite DOI map built by build_doi_map.py           (default data/doi_map.sqlite)

API Endpoints:
    GET /search - Search the index with a query
        Query parameters:
            - query (required): search query string
            - k (optional, default=10): number of results
            - fields (optional): comma-separated list of fields to return (title, text, doi, authors, extra)
            - index (optional, default='all'): 'all' (title + body), 'text' (body only) or 'title'
        Response: List of documents with scores and full metadata
"""

import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Literal, Optional, Dict, Any, Tuple
import orjson
import ujson as json
from pyserini.search.lucene import LuceneSearcher
from pathlib import Path

import snippets
from build_doi_map import DOI_MAP_PATH, normalize_doi

REPO_ROOT = Path(__file__).resolve().parents[2]


def _path_from_env(default: str, *names: str) -> str:
    """First set environment variable among ``names``, else ``default`` under the repo root."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return str(Path(value).expanduser())
    return str(REPO_ROOT / default)


# One Lucene index with two fields per paper: "contents" (body text) and
# "title". Built from the merged collection (convert_data_merge.py) with
# `--fields title` (see src/pyserini/README.md). FULL_TEXT_INDEX_DIR is
# accepted as an older name for the same setting.
FULL_TEXT_INDEX_DIR = _path_from_env("data/s2orc_index", "INDEX_DIR", "FULL_TEXT_INDEX_DIR")
# Field weights for index=all: a title match counts this many times a body match.
TITLE_WEIGHT = float(os.environ.get("SCHOLAR_TITLE_WEIGHT", "2.0"))
# Threads per worker for request handling (see startup_event).
THREADS = int(os.environ.get("SCHOLAR_THREADS", "40"))
ORIGINAL_DATA_PATH = _path_from_env("data/s2orc_papers", "ORIGINAL_DATA_PATH")

"""

    authors 
    doi
    text
    title
    abstract
    extra
        sources -> List[urls]
        external_ids -> Dict
        references (biblography) -> List[Dict]

"""

app = FastAPI(
    title="Scholar Search",
    description=(
        "Search 27.7M full-text academic papers (S2ORC) with BM25 over title and body. "
        "`/search` returns passages around the query match, `/fulltext` returns extracted "
        "document fields, `/doi/{doi}` looks up one paper. All search endpoints take an "
        "`Authorization: Bearer <key>` header."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Bearer-token auth
#
# Keys come from SCHOLAR_API_KEYS (comma-separated) and/or SCHOLAR_API_KEYS_FILE
# (one per line). With no keys configured the API is open and a warning is
# printed at startup.
# ---------------------------------------------------------------------------

def _load_api_keys() -> set:
    keys = set()
    keys.update(k.strip() for k in os.environ.get("SCHOLAR_API_KEYS", "").split(",") if k.strip())
    path = os.environ.get("SCHOLAR_API_KEYS_FILE")
    if path:
        with open(path) as f:
            for line in f:
                key = line.split("#", 1)[0].strip()   # allow "key   # comment"
                if key:
                    keys.add(key)
    return keys


API_KEYS = _load_api_keys()
_bearer = HTTPBearer(auto_error=False)


def require_api_key(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> None:
    """Route dependency: valid `Authorization: Bearer <key>` header, unless no keys are configured."""
    if not API_KEYS:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    if not any(secrets.compare_digest(credentials.credentials, key) for key in API_KEYS):
        raise HTTPException(status_code=401, detail="Invalid API key", headers={"WWW-Authenticate": "Bearer"})


# Initialized once per worker at startup.
searcher: Optional[LuceneSearcher] = None


@app.on_event("startup")
async def startup_event():
    """Open the Lucene index on startup."""
    global searcher

    # Size of the per-worker thread pool that runs the (sync) endpoints.
    # FastAPI's default is 40; under the GIL fewer threads can mean less
    # contention and better throughput. Tuned via stress tests (see README).
    import anyio
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADS
    print(f"thread pool per worker: {THREADS}")

    index_path = Path(FULL_TEXT_INDEX_DIR)
    if not index_path.exists():
        raise RuntimeError(f"Index not found at {FULL_TEXT_INDEX_DIR}. Build it first or set INDEX_DIR.")

    searcher = LuceneSearcher(str(index_path))
    print(f"searcher initialized with index: {FULL_TEXT_INDEX_DIR} ({searcher.num_docs:,} docs)")

    # Warm up: the first queries against a cold index take 1-2 s (disk pages,
    # JIT); pay that here instead of on the first user request.
    t0 = time.perf_counter()
    for warm_query in WARMUP_QUERIES:
        for index in INDEX_FIELDS:
            run_search(warm_query, index, k=10)
    print(f"warm-up done in {time.perf_counter() - t0:.1f}s")

    if API_KEYS:
        print(f"auth: {len(API_KEYS)} API key(s) loaded")
    else:
        print("auth: WARNING no API keys configured (SCHOLAR_API_KEYS / SCHOLAR_API_KEYS_FILE); API is open")
    if not Path(DOI_MAP_PATH).is_file():
        print(f"doi: WARNING {DOI_MAP_PATH} not found; /doi will return 503 until build_doi_map.py has run")


def read_json_at_offset(file_path: str, offset: int) -> Optional[Dict[str, Any]]:
    """
    Read a JSON object from a JSONL file at a specific byte offset.

    Args:
        file_path: Path to the JSONL file
        offset: Byte offset where the JSON object starts

    Returns:
        dict: Parsed JSON object or None if error
    """
    with open(file_path, 'rb') as f:
        f.seek(offset)
        line = f.readline().strip()
        if line:
            try:
                # orjson parses the raw bytes directly (no decode step);
                # measured ~3x faster than ujson on these 50-500 KB records.
                return orjson.loads(line)
            except orjson.JSONDecodeError as e:
                print(f"Error decoding JSON at offset {offset}: {e}")
                return None
    return None


def extract_text_from_annotations(text: str, annotations: Dict[str, Any], field_name: str) -> List:
    """
    Extract text from character offsets in v2 annotations.

    Args:
        text: Full text content
        annotations: Annotations dict containing character offsets
        field_name: Name of the field to extract (e.g., "title", "abstract")

    Returns:
        str: Extracted text from the specified field
    """
    if not text or not annotations:
        return []
    try:
        field_annotations = annotations.get(field_name, [])
        if not field_annotations:
            return []
        field_annotations = json.loads(field_annotations)

        results = []
        for span in field_annotations:
            if isinstance(span, dict) and "start" in span and "end" in span:
                start = int(span["start"])
                end = int(span["end"])
                results.append(text[start:end])
        return results
    except Exception as e:
        pass


def extract_document_fields(json_data: Dict[str, Any], version: str, requested_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Extract relevant fields from the document based on version.

    Returns: {
        "external_ids": dict,
        "source": dict,
        "text": str,
        "title": str
    }

    Args:
        json_data: Original JSON document
        version: Document version ("v1" or "v2")
        requested_fields: List of fields to include in response (None = all fields)

    Returns:
        dict: Document with only the required fields
    """
    # Define all available fields
    all_fields = {"text", "doi", "authors", "title", "extra"}

    # If no specific fields requested, return all
    if requested_fields is None:
        fields_to_include = all_fields
    else:
        # Only include requested fields that are valid
        fields_to_include = set(requested_fields) & all_fields

    result = {}

    # Any nested object may be null in the data (e.g. "externalids": null), so
    # every lookup below uses `or {}` rather than a dict default.
    if version == "v1":
        oa = json_data.get("openaccessinfo") or {}
        if "title" in fields_to_include:
            result["title"] = json_data.get("title") or ""

        if "text" in fields_to_include:
            result["text"] = (json_data.get("body") or {}).get("text") or ""

        if "doi" in fields_to_include:
            # v1 records spell the key "externalids" (no underscore), with "DOI" upper-cased.
            result["doi"] = (oa.get("externalids") or {}).get("DOI") or ""

        if "authors" in fields_to_include:
            result["authors"] = json_data.get("authors")

        if "extra" in fields_to_include:
            bib = json_data.get("bibliography") or {}
            bib_text = bib.get("text")
            bib_annotations = bib.get("annotations")
            references = [extract_text_from_annotations(bib_text, bib_annotations, key) for key in bib_annotations.keys()] if bib_annotations else []

            result["extra"] = {
                "external_ids": oa.get("externalids"),
                "source": oa.get("url"),
                "references": references
            }
    else:
        content = json_data.get("content") or {}
        text = content.get("text") or ""
        annotations = content.get("annotations") or {}
        external_ids = json_data.get("externalids") or {}

        if "title" in fields_to_include:
            title_list = extract_text_from_annotations(text, annotations, "title")
            result["title"] = title_list[0] if title_list else ""

        if "text" in fields_to_include:
            result["text"] = text

        if "doi" in fields_to_include:
            result["doi"] = external_ids.get("doi")

        if "authors" in fields_to_include:
            authors = [extract_text_from_annotations(text, annotations, "author")]
            result["authors"] = authors

        if "extra" in fields_to_include:
            references = [extract_text_from_annotations(text, annotations, key) for key in annotations.keys() if key.startswith("bib")]
            result["extra"] = {
                "external_ids": external_ids,
                "source": content.get("source"),
                "references": references
            }

    return result


def get_version(json_data: Dict[str, Any]) -> str:
    """
    Determine file version of the JSON document.

    Args:
        json_data: Original JSON document

    Returns:
        str: "v1" or "v2"
    """
    version = "v1" if (json_data.get("body") or {}).get("text") else "v2"
    return version


def parse_doc_id(doc_id: str) -> Tuple[str, int, str]:
    """
    Split a Pyserini document ID into (filename, offset, version).

    Document ID format: {filename}___{offset}__{version}
    """
    parts = doc_id.split('___')
    if len(parts) != 2:
        raise ValueError(f"Invalid document ID format: {doc_id}")

    filename = parts[0]
    offset_version = parts[1].split('__')
    if len(offset_version) != 2:
        raise ValueError(f"Invalid document ID format: {doc_id}")

    return filename, int(offset_version[0]), offset_version[1]


def load_raw_document(doc_id: str) -> Tuple[Dict[str, Any], str]:
    """
    Read the original JSON record for a Pyserini document ID.

    Returns:
        (document dict, version) where version is "v1" or "v2"
    """
    filename, offset, version = parse_doc_id(doc_id)

    file_path = Path(ORIGINAL_DATA_PATH) / filename
    if not file_path.exists():
        raise FileNotFoundError(f"Original data file not found: {file_path}")

    doc = read_json_at_offset(str(file_path), offset)
    if doc is None:
        raise ValueError(f"Could not read document at offset {offset} in {filename}")

    return doc, version


def retrieve_original_document(doc_id: str, requested_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Retrieve the original document from the data files using the Pyserini document ID.

    Document ID format: {filename}___{offset}__{version}

    Args:
        doc_id: Pyserini document ID (e.g., "filename.jsonl___12345__v2")
        requested_fields: List of fields to include in response (None = all fields)

    Returns:
        dict: Document with extracted fields (external_ids, source, text, title)
    """
    doc, version = load_raw_document(doc_id)

    # Extract only the required fields based on version
    extracted_doc = extract_document_fields(doc, version, requested_fields)

    return extracted_doc


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "running",
        "service": "Scholar Search",
        "index": FULL_TEXT_INDEX_DIR
    }


# ---------------------------------------------------------------------------
# Snippet search: GET /search
#
# Returns, for each hit, a short passage of the paper centred on where the
# query terms cluster (see snippets.py), instead of the full document.
# ---------------------------------------------------------------------------

MAX_TOP_K = 100
MAX_OFFSET = 1000          # offset + top_k may not exceed this
DEFAULT_CONTEXT_WORDS = 20   # words of context before/after a matched passage
DEFAULT_MAX_MATCHES = 5      # passages returned per paper (incl. the best one)
MAX_MATCHES = 50
WARMUP_QUERIES = ("machine learning", "insulin resistance", "graph neural network")

# `index` parameter -> Lucene fields and weights. "all" scores title and body
# together (a title hit counts TITLE_WEIGHT times a body hit); "text" is the
# body only; "title" the title only.
INDEX_FIELDS = {
    "all": {"contents": 1.0, "title": TITLE_WEIGHT},
    "text": {},                      # pyserini default: the contents field
    "title": {"title": 1.0},
}
IndexName = Literal["all", "text", "title"]


def run_search(q: str, index: str, k: int):
    """BM25 search over the chosen field(s); returns pyserini hits."""
    if searcher is None:
        raise HTTPException(status_code=503, detail="Searcher not initialized")
    fields = INDEX_FIELDS[index]
    return searcher.search(q, k=k, fields=fields) if fields else searcher.search(q, k=k)

# Records are read from disk in parallel; file reads release the GIL.
_doc_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="docload")
# PyStemmer instances are not thread-safe: one per thread.
_thread_local = threading.local()


def _stemmer():
    stemmer = getattr(_thread_local, "stemmer", None)
    if stemmer is None:
        stemmer = _thread_local.stemmer = snippets.make_stemmer()
    return stemmer


class SnippetSearchRequest(BaseModel):
    q: str = Field(..., min_length=1, description="Search query")
    top_k: int = Field(10, ge=1, le=MAX_TOP_K, description="Number of docs to return")
    offset: int = Field(0, ge=0, le=MAX_OFFSET, description="Pagination offset (skip this many hits)")
    index: IndexName = Field("all", description="all = title + body (default), text = body only, title = title only")
    context_words: int = Field(
        DEFAULT_CONTEXT_WORDS, ge=1, le=200,
        description="Words of context before the first and after the last matched word of a passage",
    )
    max_matches: int = Field(
        DEFAULT_MAX_MATCHES, ge=1, le=MAX_MATCHES,
        description="Maximum passages returned per paper in `matches` (best first; the first is also `content`)",
    )
    highlight: bool = Field(False, description="Wrap matched words in <b>…</b>")


class SnippetMatch(BaseModel):
    content: str = Field(..., description="Passage of the paper with context around the matched words")
    score: float = Field(..., description="Fraction of the query terms this passage contains (0..1)")


class SnippetDoc(BaseModel):
    title: str
    doi: Optional[str] = None
    link: Optional[str] = Field(None, description="https://doi.org/<doi>, else an open-access/PDF URL when known")
    content: str = Field(..., description="Best passage of the paper for the query (same as matches[0].content)")
    matches: List[SnippetMatch] = Field(..., description="Passages where the query terms occur, best first")
    match_count: int = Field(..., description="Total occurrences of the query terms in the paper body")
    score: float = Field(..., description="BM25 relevance score of the paper")


class SnippetSearchResponse(BaseModel):
    searchParameters: Dict[str, Any]
    docs: List[SnippetDoc]
    took_ms: int


def build_link(doi: Optional[str], source: Any) -> Optional[str]:
    """Best public URL for a paper: DOI resolver, else open-access URL, else first PDF URL."""
    if doi:
        return f"https://doi.org/{doi}"
    if isinstance(source, str) and source:
        return source                                   # v1: openaccessinfo.url
    if isinstance(source, dict):                        # v2: content.source
        url = (source.get("oainfo") or {}).get("openaccessurl")
        if url:
            return url
        pdfurls = source.get("pdfurls") or []
        if pdfurls:
            return pdfurls[0]
    return None


def _snippet_doc(hit, stems: set, context_words: int, max_matches: int, highlight: bool) -> SnippetDoc:
    """Load one hit's record and build its snippet. Runs in _doc_pool."""
    doc, version = load_raw_document(hit.docid)
    fields = extract_document_fields(doc, version, ["title", "doi", "text"])
    text = fields.get("text") or ""

    if version == "v1":
        # v1 body text has no front matter and the bibliography is a separate field.
        source = (doc.get("openaccessinfo") or {}).get("url")
        start_at, end_at = 0, None
    else:
        content = doc.get("content") or {}
        source = content.get("source")
        annotations = content.get("annotations")
        # Skip the author/affiliation block at the top and the reference list
        # at the bottom of v2 texts; both make poor snippets.
        start_at = snippets.content_start(annotations)
        end_at = snippets.content_end(annotations, len(text))

    passages, match_count = snippets.extract_passages(
        text, stems, _stemmer(), context_words, max_matches, highlight, start_at, end_at
    )
    doi = fields.get("doi") or None
    return SnippetDoc(
        title=(fields.get("title") or "").strip(),
        doi=doi,
        link=build_link(doi, source),
        content=passages[0].content if passages else "",
        matches=[SnippetMatch(content=p.content, score=p.score) for p in passages],
        match_count=match_count,
        score=float(hit.score),
    )


def _snippet_search(req: SnippetSearchRequest) -> SnippetSearchResponse:
    if searcher is None:
        raise HTTPException(status_code=503, detail="Searcher not initialized")
    if not req.q.strip():
        raise HTTPException(status_code=422, detail="q must not be blank")
    if req.offset + req.top_k > MAX_OFFSET:
        raise HTTPException(status_code=422, detail=f"offset + top_k must be <= {MAX_OFFSET}")

    t0 = time.perf_counter()
    stems = snippets.query_stems(req.q, _stemmer())

    hits = run_search(req.q, req.index, k=req.offset + req.top_k)[req.offset:]

    futures = [_doc_pool.submit(_snippet_doc, hit, stems, req.context_words, req.max_matches, req.highlight) for hit in hits]
    docs: List[SnippetDoc] = []
    for hit, future in zip(hits, futures):
        try:
            docs.append(future.result())
        except Exception as e:
            # Skip a hit whose record cannot be read; keep the rest of the page.
            print(f"Error building snippet for {hit.docid}: {e}")

    return SnippetSearchResponse(
        searchParameters=req.model_dump(),
        docs=docs,
        took_ms=int((time.perf_counter() - t0) * 1000),
    )


@app.get("/search", response_model=SnippetSearchResponse, dependencies=[Depends(require_api_key)])
def snippet_search_get(
    q: str = Query(..., min_length=1, description="Search query"),
    top_k: int = Query(10, ge=1, le=MAX_TOP_K, description="Number of docs to return"),
    offset: int = Query(0, ge=0, le=MAX_OFFSET, description="Pagination offset"),
    index: IndexName = Query("all", description="all = title + body (default), text = body only, title = title only"),
    context_words: int = Query(DEFAULT_CONTEXT_WORDS, ge=1, le=200, description="Words of context before/after each passage"),
    max_matches: int = Query(DEFAULT_MAX_MATCHES, ge=1, le=MAX_MATCHES, description="Max passages per paper"),
    highlight: bool = Query(False, description="Wrap matched words in <b>…</b>"),
):
    """
    Snippet search: the top papers for a query, each with the passages where
    the query terms occur (`matches`, best first; the best one is also
    `content`), with `context_words` words of context on either side.

    Only ``q`` is required. Papers are ordered by relevance.
    """
    return _snippet_search(SnippetSearchRequest(
        q=q, top_k=top_k, offset=offset, index=index,
        context_words=context_words, max_matches=max_matches, highlight=highlight,
    ))


# ---------------------------------------------------------------------------
# Full-document search: GET /fulltext
#
# Same ranking as /search but returns the extracted document fields instead
# of a snippet. `fields` selects which of title, text, doi, authors, extra.
# ---------------------------------------------------------------------------

DOC_FIELDS = ("title", "text", "doi", "authors", "extra")


class FulltextRequest(BaseModel):
    q: str = Field(..., min_length=1, description="Search query")
    top_k: int = Field(10, ge=1, le=MAX_TOP_K, description="Number of docs to return")
    offset: int = Field(0, ge=0, le=MAX_OFFSET, description="Pagination offset (skip this many hits)")
    index: IndexName = Field("all", description="all = title + body (default), text = body only, title = title only")
    fields: Optional[List[Literal["title", "text", "doi", "authors", "extra"]]] = Field(
        None, description="Document fields to return; all when omitted"
    )


class FulltextResponse(BaseModel):
    searchParameters: Dict[str, Any]
    docs: List[Dict[str, Any]] = Field(..., description="Per doc: score, link, and the requested fields")
    took_ms: int


def _document_payload(doc: Dict[str, Any], version: str, fields: Optional[List[str]]) -> Dict[str, Any]:
    """Extracted fields for a raw record plus a `link`, independent of record version."""
    wanted = list(fields) if fields else list(DOC_FIELDS)
    out = extract_document_fields(doc, version, wanted + ["doi"])
    doi = out.get("doi") or None
    if version == "v1":
        source = (doc.get("openaccessinfo") or {}).get("url")
    else:
        source = (doc.get("content") or {}).get("source")
    out["link"] = build_link(doi, source)
    if "doi" not in wanted:
        out.pop("doi", None)
    else:
        out["doi"] = doi
    return out


def _fulltext_doc(hit, fields: Optional[List[str]]) -> Dict[str, Any]:
    doc, version = load_raw_document(hit.docid)
    payload = {"score": float(hit.score)}
    payload.update(_document_payload(doc, version, fields))
    return payload


def _fulltext_search(req: FulltextRequest) -> FulltextResponse:
    if searcher is None:
        raise HTTPException(status_code=503, detail="Searcher not initialized")
    if not req.q.strip():
        raise HTTPException(status_code=422, detail="q must not be blank")
    if req.offset + req.top_k > MAX_OFFSET:
        raise HTTPException(status_code=422, detail=f"offset + top_k must be <= {MAX_OFFSET}")

    t0 = time.perf_counter()
    hits = run_search(req.q, req.index, k=req.offset + req.top_k)[req.offset:]

    futures = [_doc_pool.submit(_fulltext_doc, hit, req.fields) for hit in hits]
    docs: List[Dict[str, Any]] = []
    for hit, future in zip(hits, futures):
        try:
            docs.append(future.result())
        except Exception as e:
            print(f"Error loading document {hit.docid}: {e}")

    return FulltextResponse(
        searchParameters=req.model_dump(),
        docs=docs,
        took_ms=int((time.perf_counter() - t0) * 1000),
    )


@app.get("/fulltext", response_model=FulltextResponse, dependencies=[Depends(require_api_key)])
def fulltext_search_get(
    q: str = Query(..., min_length=1, description="Search query"),
    top_k: int = Query(10, ge=1, le=MAX_TOP_K, description="Number of docs to return"),
    offset: int = Query(0, ge=0, le=MAX_OFFSET, description="Pagination offset"),
    index: IndexName = Query("all", description="all = title + body (default), text = body only, title = title only"),
    fields: Optional[str] = Query(None, description="Comma-separated subset of: title, text, doi, authors, extra"),
):
    """
    Full-document search: the top documents for a query with their extracted
    fields (title, text, doi, authors, extra) and a link.
    """
    field_list = None
    if fields:
        field_list = [f.strip() for f in fields.split(",") if f.strip()]
        bad = [f for f in field_list if f not in DOC_FIELDS]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown fields {bad}; valid: {list(DOC_FIELDS)}")
    return _fulltext_search(FulltextRequest(q=q, top_k=top_k, offset=offset, index=index, fields=field_list))


# ---------------------------------------------------------------------------
# DOI lookup: GET /doi/{doi}
#
# Resolves a DOI to exactly one paper via indexes/doi_map.sqlite, built by
# build_doi_map.py, and returns its full extracted data.
# ---------------------------------------------------------------------------

_doi_local = threading.local()


def _doi_db() -> Optional[sqlite3.Connection]:
    """Per-thread read-only connection to the DOI map, or None if it has not been built."""
    conn = getattr(_doi_local, "conn", None)
    if conn is None:
        if not Path(DOI_MAP_PATH).is_file():
            return None
        conn = sqlite3.connect(f"file:{DOI_MAP_PATH}?mode=ro", uri=True, check_same_thread=False)
        _doi_local.conn = conn
    return conn


def _lookup_doi(doi: str) -> Dict[str, Any]:
    key = normalize_doi(doi)
    if not key:
        raise HTTPException(status_code=422, detail="doi must not be blank")
    db = _doi_db()
    if db is None:
        raise HTTPException(status_code=503, detail="DOI map not built yet (run build_doi_map.py)")
    row = db.execute("SELECT filename, offset FROM doi WHERE doi = ?", (key,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No paper with DOI {key}")
    filename, offset = row
    doc = read_json_at_offset(str(Path(ORIGINAL_DATA_PATH) / filename), offset)
    if doc is None:
        raise HTTPException(status_code=500, detail="Record for this DOI could not be read")
    payload = _document_payload(doc, get_version(doc), None)
    payload["doi"] = payload.get("doi") or key
    return payload


@app.get("/doi/{doi:path}", dependencies=[Depends(require_api_key)])
def doi_lookup_path(doi: str):
    """Look up one paper by DOI, e.g. GET /doi/10.1155/2016/2794591."""
    return _lookup_doi(doi)


@app.get("/health")
async def health():
    """Detailed health check endpoint."""
    index_exists = Path(FULL_TEXT_INDEX_DIR).exists()
    data_exists = Path(ORIGINAL_DATA_PATH).exists()

    return {
        "status": "healthy" if (searcher and index_exists and data_exists) else "unhealthy",
        "index_exists": index_exists,
        "index_path": FULL_TEXT_INDEX_DIR,
        "data_exists": data_exists,
        "data_path": ORIGINAL_DATA_PATH,
        "searcher_initialized": searcher is not None,
        "num_docs": searcher.num_docs if searcher else 0,
        "doi_map_exists": Path(DOI_MAP_PATH).is_file(),
        "doi_map_path": str(DOI_MAP_PATH),
        "title_weight": TITLE_WEIGHT
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
