#!/usr/bin/env python3

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import ujson as json
import os
from pyserini.search.lucene import LuceneSearcher
from pathlib import Path

FULL_TEXT_INDEX_DIR = os.getenv("FULL_TEXT_INDEX_DIR", "data/s2orc_index_text")
TITLE_INDEX_DIR = os.getenv("TITLE_INDEX_DIR", "data/s2orc_index_title")
ORIGINAL_DATA_PATH = os.getenv("ORIGINAL_DATA_PATH", "data/s2orc_papers")

app = FastAPI(
    title="Pyserini Search API",
    description="Search academic papers using BM25 and retrieve full metadata",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

searcher_full_text = None
searcher_title = None


@app.on_event("startup")
async def startup_event():
    global searcher_full_text
    global searcher_title

    full_text_index_path = Path(FULL_TEXT_INDEX_DIR)
    title_index_path = Path(TITLE_INDEX_DIR)

    if not full_text_index_path.exists() or not title_index_path.exists():
        raise RuntimeError(f"Index not found at {FULL_TEXT_INDEX_DIR}. Please build the index first.")

    searcher_full_text = LuceneSearcher(str(full_text_index_path))
    searcher_title = LuceneSearcher(str(title_index_path))

    print(f"searcher_full_text initialized with index: {FULL_TEXT_INDEX_DIR}")
    print(f"searcher_title initialized with index: {TITLE_INDEX_DIR}")


def read_json_at_offset(file_path: str, offset: int) -> Optional[Dict[str, Any]]:
    with open(file_path, 'rb') as f:
        f.seek(offset)
        line = f.readline()
        if line:
            line_str = line.decode('utf-8').strip()
            if line_str:
                try:
                    return json.loads(line_str)
                except json.JSONDecodeError as e:
                    print(f"Error decoding JSON at offset {offset}: {e}")
                    return None
    return None


def extract_text_from_annotations(text: str, annotations: Dict[str, Any], field_name: str) -> List:
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
    all_fields = {"text", "doi", "authors", "title", "extra"}

    if requested_fields is None:
        fields_to_include = all_fields
    else:
        fields_to_include = set(requested_fields) & all_fields

    result = {}

    if version == "v1":
        if "title" in fields_to_include:
            result["title"] = json_data.get("title", "")

        if "text" in fields_to_include:
            result["text"] = json_data.get("body", {}).get("text", "")

        if "doi" in fields_to_include:
            result["doi"] = json_data.get("openaccessinfo", {}).get("external_ids", {}).get("DOI", "")

        if "authors" in fields_to_include:
            result["authors"] = json_data.get("authors")

        if "extra" in fields_to_include:
            bib = json_data.get("bibliography", {})
            bib_text = bib.get("text")
            bib_annotations = bib.get("annotations")
            references = [extract_text_from_annotations(bib_text, bib_annotations, key) for key in bib_annotations.keys()] if bib_annotations else []

            result["extra"] = {
                "external_ids": json_data.get("openaccessinfo", {}).get("external_ids"),
                "source": json_data.get("openaccessinfo", {}).get("url", {}),
                "references": references
            }
    else:
        text = json_data.get("content", {}).get("text", "")
        annotations = json_data.get("content", {}).get("annotations", {})
        external_ids = json_data.get("externalids", {})

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
                "source": json_data.get("content", {}).get("source", {}),
                "references": references
            }

    return result


def get_version(json_data: Dict[str, Any]) -> str:
    version = "v1" if json_data.get("body", {}).get("text", "") else "v2"
    return version


def retrieve_original_document(doc_id: str, requested_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    parts = doc_id.split('___')
    if len(parts) != 2:
        raise ValueError(f"Invalid document ID format: {doc_id}")

    filename = parts[0]
    offset_version = parts[1].split('__')
    if len(offset_version) != 2:
        raise ValueError(f"Invalid document ID format: {doc_id}")

    offset = int(offset_version[0])
    version = offset_version[1]

    file_path = Path(ORIGINAL_DATA_PATH) / filename

    if not file_path.exists():
        raise FileNotFoundError(f"Original data file not found: {file_path}")

    doc = read_json_at_offset(str(file_path), offset)

    if doc is None:
        raise ValueError(f"Could not read document at offset {offset} in {filename}")

    extracted_doc = extract_document_fields(doc, version, requested_fields)

    return extracted_doc


class SearchRequest(BaseModel):
    query: str = Field(..., description="Search query string", min_length=1)
    k: int = Field(default=10, description="Number of top results to return", ge=1, le=1000)


class SearchResult(BaseModel):
    score: float = Field(..., description="BM25 relevance score")
    doc_id: str = Field(..., description="Pyserini document ID")
    document: Dict[str, Any] = Field(..., description="Full original document metadata")


class SearchResponse(BaseModel):
    query: str = Field(..., description="Original search query")
    total_results: int = Field(..., description="Number of results returned")
    results: List[SearchResult] = Field(..., description="List of search results with scores and documents")


@app.get("/")
async def root():
    return {
        "status": "running",
        "service": "Pyserini Search API",
        "index": FULL_TEXT_INDEX_DIR
    }


@app.get("/search", response_model=SearchResponse)
async def search(
    query: str,
    k: int = 10,
    fields: Optional[str] = Query(None, description="Comma-separated list of fields to include in response (title, text, doi, authors, extra). If not specified, all fields are returned."),
    index_type: str = Query("text", description="Index to search: 'text' for full-text search, 'title' for title-only search")
):
    if searcher_full_text is None or searcher_title is None:
        raise HTTPException(status_code=503, detail="Searchers not initialized")

    if not query or len(query.strip()) == 0:
        raise HTTPException(status_code=400, detail="Query parameter is required and cannot be empty")

    if k < 1 or k > 1000:
        raise HTTPException(status_code=400, detail="k must be between 1 and 1000")

    if index_type not in ["text", "title"]:
        raise HTTPException(status_code=400, detail="index_type must be either 'text' or 'title'")

    searcher = searcher_full_text if index_type == "text" else searcher_title

    requested_fields = None
    if fields:
        requested_fields = [f.strip() for f in fields.split(',') if f.strip()]
        if not requested_fields:
            requested_fields = None

    try:
        hits = searcher.search(query, k=k)

        results = []
        for hit in hits:
            try:
                original_doc = retrieve_original_document(hit.docid, requested_fields=requested_fields)

                result = SearchResult(
                    score=float(hit.score),
                    doc_id=hit.docid,
                    document=original_doc
                )
                results.append(result)

            except Exception as e:
                print(f"Error retrieving document {hit.docid}: {e}")
                results.append(SearchResult(
                    score=float(hit.score),
                    doc_id=hit.docid,
                    document={"error": str(e), "doc_id": hit.docid}
                ))

        return SearchResponse(
            query=query,
            total_results=len(results),
            results=results
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@app.get("/health")
async def health():
    index_exists = Path(FULL_TEXT_INDEX_DIR).exists()
    data_exists = Path(ORIGINAL_DATA_PATH).exists()

    return {
        "status": "healthy" if (searcher_full_text and index_exists and data_exists) else "unhealthy",
        "index_exists": index_exists,
        "index_path": FULL_TEXT_INDEX_DIR,
        "data_exists": data_exists,
        "data_path": ORIGINAL_DATA_PATH,
        "searcher_full_text_initialized": searcher_full_text is not None
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
