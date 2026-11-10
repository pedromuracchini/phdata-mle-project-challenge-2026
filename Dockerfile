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

# Unprivileged user for the server. The application files stay owned by root, so the process
# cannot modify its own code or artifacts.
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

# Prometheus multiprocess mode (core/metrics.py): every worker writes its metrics to files here
# and a scrape aggregates them. Owned by the app user; mode 1777 (like /tmp) so `docker run
# --user <uid>` runs, e.g. `make check-equivalence`, can write their own files too.
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
RUN install -d -m 1777 -o app -g app /tmp/prometheus

WORKDIR /app

# Same path as in the builder: the venv's scripts hard-code it in their shebangs.
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# One BLAS (OpenBLAS, numpy) and one OpenMP (scikit-learn) thread per process. A request is one
# row: the pools only add threads that spin against each other. Unlimited, a KNN request took
# 180 ms of handler time instead of 32 ms (docs/benchmarks/phase-4-performance.md, step 3).
# Concurrency comes from uvicorn workers instead; keep workers x threads <= cores.
# The OpenMP count also fixes results: scikit-learn's KNN breaks exact neighbour-distance ties
# by how it chunks the training set across threads, so one thread makes predictions
# reproducible on any host (tests/fixtures/baseline_predictions.json, "amendments").
# Keep in sync with Dockerfile.test and the Makefile's THREAD_ENV.
ENV OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

COPY src .

# Worker processes: uvicorn reads WEB_CONCURRENCY when --workers is not given, so set it at
# deploy time to the physical cores the container gets (threads per worker are 1, above):
# hyperthreads added no throughput (docs/benchmarks/phase-4-performance.md, step 8b). Every
# worker loads its own copy of the artifacts: budget ~140 MiB + ~130 MiB per extra worker. No --reload here: it is a development tool (it polled
# every *.py under /app, .venv included, 4 times a second); `make docker-dev` runs with it.
# --http: uvicorn's h11 protocol plus TCP_NODELAY. Serving from worker processes, uvicorn
# leaves Nagle on and every response waited ~44 ms for the client's ACK (core/server.py).
ENV WEB_CONCURRENCY=1

USER app

# Liveness only (/health checks no dependency; /ready is readiness). Python's urllib instead of
# curl, so the image needs no extra package; urlopen fails on any non-2xx status. Checked every
# second during the start period, so `docker compose` sees the API healthy as soon as it is.
HEALTHCHECK --interval=30s --timeout=3s --retries=3 --start-period=30s --start-interval=1s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# Empty the metrics directory before uvicorn starts its workers: files left by a previous run
# of this container would be added to the new counters. It cannot be done in the lifespan,
# which every worker runs. Then exec the CMD (unchanged; signals reach uvicorn directly).
ENTRYPOINT ["/bin/sh", "-c", "d=\"${PROMETHEUS_MULTIPROC_DIR:-}\"; if [ -n \"$d\" ]; then mkdir -p \"$d\" && find \"$d\" -mindepth 1 -delete; fi; exec \"$@\"", "--"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--http", "core.server:NoDelayH11Protocol"]
