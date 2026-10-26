# Scholar Search API — Step 1: snippet search endpoint (`s2orc`)

## Context

The team wants one shared, high-throughput scholar search API. **All work happens in the `s2orc` repo** (the pyserini/Lucene repo). The first deliverable is `/search/`: a snippet search that takes a query and returns a list of docs, each with a short passage showing where the query matches, with context before and after. Fulltext search, DOI lookup, bearer auth and gunicorn come later; their design is in `docs/plans/00_scholar_api_full_plan.md`.

Decisions taken with the user:
- The snippet route is **added to the existing `api.py` app** (one app, one JVM, one `start.sh`). The current `GET /search` (full documents) stays untouched until the `/fulltext/` step renames it.
- **PyStemmer is added** (`uv add PyStemmer`) for exact stem-vs-stem matching in the snippet extractor. No orjson (ujson is already used and parsing is not the bottleneck).

### What exists in `s2orc`
- `api.py`: FastAPI app with `GET /search?query=&k=&fields=&index_type=text|title` returning full documents. Reusable helpers: `read_json_at_offset()`, `get_version()`, `extract_document_fields()` (title/doi/authors/extra for v1 and v2), `retrieve_original_document()`. Doc id format `{filename}___{offset}__{version}`; raw data in the directory given by `ORIGINAL_DATA_PATH`.
- Indexes: full text `full_text_indexes/pyserini_index` (27.76M docs, 247 GB, positions stored, raw text not stored), title `indexes/pyserini_index_title` (26.4M docs, 3.5 GB).
- Serving today: `start.sh` runs `uvicorn api:app` as a single foreground process (no systemd, no supervisor, no auth). It is currently running with `--reload` (PID 103749).
- Snippets do not exist; raw text is not in Lucene, so snippets are computed from the JSONL record fetched by byte offset.
- Env: Python 3.13 venv; fastapi/uvicorn/pyserini/ujson/pydantic installed and locked via pyserini's own dependencies in `uv.lock`. Missing: `PyStemmer` (this step), `gunicorn`/`uvicorn-worker` (later step).

Measured: `import pyserini` ~10 s (JVM), cold query 1.3–1.8 s, warm query ~80 ms, ~1.3 GB RSS/process. Lucene stems `branched chain amino acid insulin resistance` → `branch chain amino acid insulin resist`.

## Design

### Files
```
api.py                 # MODIFY: add GET/POST /search/ (snippets) + startup warm-up; keep everything else
snippets.py            # NEW: pure snippet-extraction functions (no JVM, unit-testable)
tests/test_snippets.py # NEW: unit tests for snippet extraction
start.sh               # MODIFY: fix --reload footgun, document --workers cost (see below)
pyproject.toml/uv.lock # uv add PyStemmer
API_README.md          # add /search/ section and start.sh notes
```

### Endpoint: `GET /search/` (also `POST /search/` with the same fields as JSON)
Only `q` is required; everything else is optional with a default:

| param | default | notes |
|---|---|---|
| `q` | required | query string |
| `top_k` | 10 | 1–100 |
| `offset` | 0 | pagination; `offset + top_k ≤ 1000` |
| `index` | `text` | or `title` |
| `snippet_chars` | 300 | total width of the window centred on the best match cluster (context before + matches + context after) |
| `highlight` | false | wrap matched words in `<b>…</b>` |

Response:
```json
{
  "searchParameters": {"q": "...", "top_k": 10, "offset": 0, "index": "text"},
  "docs": [
    {
      "title": "The Relationship between ...",
      "doi": "10.1155/2016/2794591",
      "link": "https://doi.org/10.1155/2016/2794591",
      "content": "... insulin resistance was ... branched-chain amino acid ...",
      "score": 12.19
    }
  ],
  "took_ms": 95
}
```
Each doc has exactly five fields: `title`, `doi`, `link`, `content`, `score` — no `position`, `doc_id` or `corpus_id` (the user does not need internal ids; order in the list is the rank). `content` holds the snippet. `link` is `https://doi.org/<doi>` when a DOI exists, else `openaccessurl` / first `pdfurls` entry / v1 `openaccessinfo.url`, else null.

Route path is `/search/` (trailing slash) and FastAPI's redirect_slashes is left on, so it does not collide with the existing `/search`.

### Flow (`api.py` route → `snippets.py`)
1. **Query stems**: lowercase, word-regex tokenize, drop a small English stopword set (Lucene's default list), stem with PyStemmer (`Stemmer.Stemmer("porter")`, same algorithm family as Lucene's PorterStemFilter). Keep as a set.
2. **Retrieve**: `searcher.search(q, k=offset+top_k)` on the chosen `LuceneSearcher`, slice `[offset:]`. Lucene `IndexSearcher` is thread-safe; pyjnius attaches threads automatically.
3. **Load records** for the page of hits with the existing `retrieve_original_document(doc_id, requested_fields=["title","doi","text","extra"])`, in a small `ThreadPoolExecutor` (file reads release the GIL; records are 50–500 KB each).
4. **Snippet** via `snippets.best_snippet(text, query_stems, stemmer, snippet_chars, highlight)` — a match-centred window so matched words always have context on both sides:
   - skip v2 front matter (authors/affiliations) by starting at the first `abstract`/`paragraph` annotation offset when present, else char 0;
   - **find matches**: scan the text with a word regex, stem all words in one `stemmer.stemWords()` call, record `(char_start, char_end)` for every word whose stem is in the query set;
   - **pick the best cluster**: slide a window of width `snippet_chars` over the match positions and score each by distinct query stems covered (+ small bonus for total matches); earliest wins ties;
   - **centre the window**: cluster spans `[m_start, m_end]`; `pad = (snippet_chars - (m_end - m_start)) / 2`; `start = m_start - pad`, `end = m_end + pad`. Example: "amino acid chain" matched over 40 chars with `snippet_chars=300` → ~130 chars before and ~130 after;
   - clamp to text bounds (give slack to the other side), snap both edges outward to word boundaries, collapse whitespace/newlines, add `…` at any cut edge;
   - `highlight=true` wraps matched words inside the window in `<b>…</b>`;
   - fallback when nothing matches (e.g. `index=title`, or query terms only in the title): first `snippet_chars` of the abstract/first paragraph.
5. The new route is a plain `def` so FastAPI runs it in its threadpool; the existing async `/search` is unchanged. Startup additionally runs a few warm-up queries so the first user request is not the 1–2 s cold one.

### `start.sh` review and fixes
Keep the script as is (repo-relative paths, venv python, Java/package/index/port checks, `exec`). Change:
- **`--reload` footgun**: `watchfiles` is not installed, so uvicorn's reload falls back to stat-polling every `*.py` under the repo every 0.25 s, which walks the data dirs and the 247 GB index dir. The running reload server is burning ~65 % CPU on that, and each reload restarts the 10 s JVM. Fix: if `--reload` is passed, add `--reload-dir "$APP_DIR"` plus `--reload-include '*.py'` and `--reload-exclude` for `data*`, `*index*` dirs; or simply document "don't use --reload here" and strip it. Recommend the exclude approach so dev reload still works.
- Document in the header that `./start.sh --workers N` works but each worker starts its own JVM (~1.3 GB, ~10 s startup).
- Minor: the package-missing hint should say `uv sync`, matching the project.
Serving as a team service (supervisor, gunicorn, auth) remains the later step; `start.sh` stays the single entry point and that step swaps its launch line.

## Implementation order
1. `uv add PyStemmer`
2. `snippets.py` + `tests/test_snippets.py`
3. `api.py`: pydantic models, `/search/` GET+POST route, warm-up in startup
4. `start.sh` fixes
5. `API_README.md` section with `./start.sh` and a curl example

## Verification
- `uv run pytest src/server/tests/test_snippets.py`: window centred on the match (roughly equal context before and after), densest cluster beats a lone early match, stem matching ("resistance" query hits "resistant" text; "studies" hits "study"), front-matter skip, clamping at text start/end, word-boundary snapping and `…`, fallback, highlight, total length ≈ `snippet_chars`.
- Existing `GET /search` still behaves exactly as before (same response for the same query).
- Stop the current `--reload` server, `./start.sh` (and from another directory); log shows warm-up done, `GET /health` healthy, CPU idle at rest. `PORT=8002 ./start.sh` binds elsewhere; Ctrl-C stops it cleanly; `./start.sh --reload` no longer walks the data dirs.
- `GET /search/?q=branched chain amino acid insulin resistance` → 10 docs, `content` contains the query terms with text on both sides, each doc has only title/doi/link/content/score, first title matches the top hit from the timing test; `offset=10` returns the next page with no overlap; `index=title` and `snippet_chars=150` behave as expected; missing `q` → 422.
- Quick concurrency check: 32 concurrent clients on `/search/`; no JVM/thread errors; warm p50 under ~200 ms.

## Status (2026-10-22): implemented

Built as planned, with two additions found during testing:
- Snippets are taken from the paper body only (after the author block, before the first `bibentry`
  annotation); the reference list used to win because query terms cluster densely there.
- Candidate words are prefiltered with a regex built from the query stems before stemming; stemming
  every word of ten papers cost ~260 ms of GIL time per request, the prefilter brings it to ~40 ms.

Measured on one uvicorn worker (`PORT=8001 ./start.sh`): single client p50 75 ms; 32 concurrent
clients 23 req/s, p50 1.3 s. Throughput is GIL-bound per process; the gunicorn/multi-worker step is
what raises it.

## Update (2026-10-22, later): word-based context and multiple passages

Per user feedback the window is no longer a fixed character width:
- `context_words` (default 20) words before the first and after the last matched word of a passage;
  `snippet_chars` was removed. Matches within ~300 chars are grouped into one passage.
- `matches: [{content, score}]` lists up to `max_matches` (default 5) non-overlapping passages per
  paper, best first; `content` duplicates `matches[0].content`; `match_count` is the total number of
  query-term occurrences in the body. Passage score = fraction of query terms covered (0..1).
