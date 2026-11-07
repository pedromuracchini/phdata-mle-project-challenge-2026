# syntax=docker/dockerfile:1
# API image. Dependencies come only from uv.lock (uv sync --frozen), so a rebuild without the
# layer cache installs exactly the same versions. Keep the base image and uv in sync with
# Dockerfile.test, .python-version and [tool.uv] required-version in pyproject.toml.

# python:3.12.16-slim-trixie, pinned by digest: the tag alone can be re-pointed.
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# --- builder: resolve nothing, install the locked runtime dependencies into /app/.venv -------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv
# Use the image's Python (never download one), compile .pyc like pip did, copy out of the cache
# mount instead of hard-linking to it.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Only the lock inputs are mounted, so this layer is rebuilt only when dependencies change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-dev --no-install-project

# --- runtime: same base image, the venv and the application ----------------------------------
FROM ${PYTHON_IMAGE}

# Install curl for health checks (docker-compose.test.yml)
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Same path as in the builder: the venv's scripts hard-code it in their shebangs.
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY src .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
