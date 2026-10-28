# syntax=docker/dockerfile:1
#
# One image for both jobs:
#   - building the index (download, decompress, convert, merge, index, DOI map)
#   - serving the Scholar Search API
#
# Java 21 comes from the Eclipse Temurin base image (Pyserini runs Lucene in a
# JVM); Python and every package come from uv, pinned by uv.lock. Nothing is
# downloaded at run time: Pyserini bundles the Anserini jar inside its wheel.
#
# Build:   docker compose build          (or: docker build -t s2orc-scholar-search .)
# Serve:   docker compose up -d api      (see docker-compose.yml and .env.example)
# Index:   docker compose run --rm index src/pyserini/build_index.sh

FROM eclipse-temurin:21-jdk-noble

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl \
        pigz \
        procps \
        util-linux \
    && rm -rf /var/lib/apt/lists/*
# pigz: parallel decompression of the downloaded archives (decompress_*.sh)
# procps, util-linux: free / nice / ionice used by cache_warm.sh

# uv, pinned. It installs a managed CPython for the project (see .python-version).
COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so that code changes do not invalidate this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install && uv sync --frozen --no-dev

COPY src ./src
COPY README.md SETUP.md start.sh ./

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHON=/opt/venv/bin/python \
    # Default locations inside the container; docker-compose.yml mounts the
    # host directories from .env at these paths.
    DATA_DIR=/data \
    INDEX_DIR=/data/index \
    RAW_SOURCE=s3 \
    RAW_S3_MAP_PATH=/data/raw_s3_map.sqlite \
    ORIGINAL_DATA_PATH=/data/s2orc_papers \
    DOI_MAP_PATH=/data/doi_map.sqlite \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# Serve by default; `docker compose run index ...` overrides the command.
CMD ["./start.sh"]
