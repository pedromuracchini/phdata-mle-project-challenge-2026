# syntax=docker/dockerfile:1
# Training image (create_model.py). Build from the repository root:
#   docker build -f src/model/Dockerfile -t create-model .
# The model is not retrained in this project; this image is kept working, with the same
# locked dependencies as the API, so the training run stays reproducible.

ARG PYTHON_IMAGE=python:3.10.22-slim-trixie@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE}

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy the create_model script and data
COPY create_model.py .
COPY src/data ./data

CMD ["python", "create_model.py"]
