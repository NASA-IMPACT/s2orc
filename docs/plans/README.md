# Design notes

Working notes written while the Scholar Search API was designed and built (October 2026), kept
for the reasoning behind the current shape of `src/server/`. They are historical: the measurements
and decisions still apply, but file paths in them refer to the flat development checkout they were
written in, not this repository.

| Note | Covers |
|---|---|
| [00_scholar_api_full_plan.md](00_scholar_api_full_plan.md) | overall design: snippet search, full-document search, DOI lookup, bearer auth, gunicorn; status at the end |
| [01_snippet_search_endpoint.md](01_snippet_search_endpoint.md) | the snippet endpoint in detail: stem matching, passage selection, word-based context, performance work |

Where the notes say… | …look here
---|---
`api.py`, `snippets.py`, `build_doi_map.py`, `start.sh`, `gunicorn.conf.py`, `tests/` | `src/server/`
`convert_data_*.py`, `build_pyserini_index.py` | `src/pyserini/`
`indexes/doi_map.sqlite` | `data/doi_map.sqlite` (`DOI_MAP_PATH`)
`full_text_indexes/pyserini_index_v2` | `data/s2orc_index` (`INDEX_DIR`)
raw data directory | `data/s2orc_papers` (`ORIGINAL_DATA_PATH`)
`SETUP.md`, `API_README.md` | [SETUP.md](../../SETUP.md), [src/server/README.md](../../src/server/README.md)

The current, authoritative description of the endpoints is [src/server/README.md](../../src/server/README.md).
