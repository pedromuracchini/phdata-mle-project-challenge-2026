# Sound Realty house-price API

A FastAPI service that predicts the sale price of a house in the Seattle area (King County).
`POST /predict` takes eight house fields, joins the zipcode's demographics, and returns the
price predicted by the trained model in `src/model/model.pkl` (a scikit-learn
`RobustScaler -> KNeighborsRegressor` pipeline, not retrained in this project). It ships as a
Docker image.

## Overview

What changed vs the service as received (`v0-original` tag):

- **Missing data.** Every field except `zipcode` may be `null` or omitted. One missing sqft field
  is derived exactly from `sqft_living = sqft_above + sqft_basement`; anything else is imputed
  with the KNN imputer from `notebooks/imputation_experiment.ipynb`, fitted once and shipped as
  an artifact. The response lists the `imputed_fields`.
  [ADR 002](docs/adr/002-persisted-imputer-artifact.md), [ADR 003](docs/adr/003-imputation-design.md).
- **Performance.** Per-request file reads, a debug `print` and thread-pool contention are gone;
  the server runs without `--reload`, with worker processes and `TCP_NODELAY`. Measured in one
  session against the service as received: **9.2×** the throughput on complete requests with
  one worker, **19.9×** with six. On requests that need KNN, against the first version that
  could serve them: **18.1–22.3×** with one worker, **39.6–42.2×** with six
  ([`docs/benchmarks/final.md`](docs/benchmarks/final.md)). [ADR 004](docs/adr/004-serving-performance.md).
- **Production readiness.** A strict request contract (unknown fields are rejected: a breaking
  change, see [`docs/api.md`](docs/api.md)), error bodies without request data, `model_version`,
  `X-Request-ID`, one JSON log line per request, Prometheus metrics, `/ready`, a non-root image
  and CI. [ADR 005](docs/adr/005-production-readiness.md).
- **Dependencies.** uv with a committed lockfile, Python 3.12.16, the model's libraries pinned
  exactly. [ADR 001](docs/adr/001-uv-and-python-version.md).
- **Predictions for complete requests are unchanged**, with one approved exception: baseline
  row 99, an exact tie at the model's 5th neighbour, moved from 564,800 to 573,300 when the
  OpenMP thread count was fixed at one (it used to depend on the host's core count). Recorded in
  `tests/fixtures/baseline_predictions.json` (`amendments`) and
  [ADR 004](docs/adr/004-serving-performance.md), decision 3.

Every design decision has an ADR ([index](docs/adr/README.md)). How the AI agent was used and
checked: [`AI_USAGE.md`](AI_USAGE.md).

## Quick start

Prerequisites: `git`, `make`, `curl`, [uv](https://docs.astral.sh/uv/) **0.13.7** (the version
`pyproject.toml` requires; uv installs Python 3.12.16 itself), and Docker with BuildKit (the
Dockerfiles use `RUN --mount`).

```bash
git clone https://github.com/pedromuracchini/phdata-mle-project-challenge-2026.git
cd phdata-mle-project-challenge-2026

make setup                   # uv sync --frozen: Python 3.12.16 + the locked dependencies in .venv
uv run pre-commit install    # once per clone: ruff and mypy run on every commit
make test                    # hook tests, unit tests, characterization test (about 35 s)
make docker-build            # verifies the imputer artifact (make check-imputer), builds the image
make docker-run              # serves on http://localhost:8000, in the foreground; Ctrl+C stops it
```

In a second terminal:

```bash
curl -s -X POST http://localhost:8000/predict -H "Content-Type: application/json" \
  -d '{"bedrooms": 4, "bathrooms": 1.0, "sqft_living": 1680, "sqft_lot": 5043, "floors": 1.5,
       "sqft_above": 1680, "sqft_basement": 0, "zipcode": "98118"}'
```

```json
{"predicted_price":458520.0,"imputed_fields":[],"model_version":"3ffdefa1129b-e6ee4278080a"}
```

`make demo` walks through the rest of the contract against the running container: nulls and
`imputed_fields`, the 422 cases, `X-Request-ID`, `/ready` and `/metrics`. The interactive
OpenAPI page is at <http://localhost:8000/docs>. `make help` lists every target.

## Configuration

Configuration comes only from environment variables (`src/core/config.py`, the `Dockerfile`).
Pass them with `docker run -e NAME=value`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEB_CONCURRENCY` | `1` (image) | Number of uvicorn worker processes. Set it at deploy time to the **physical** cores the container gets; hyperthreads added no throughput ([ADR 004](docs/adr/004-serving-performance.md)). Each worker loads its own copy of the artifacts: budget about 140 MiB plus 130 MiB per extra worker. `make docker-run WORKERS=N` sets it. |
| `APP_CORS_ALLOWED_ORIGINS` | `[]` | Browser origins allowed by CORS, as a JSON list, e.g. `'["https://app.example.com"]'`. Empty: no CORS middleware at all. Server-to-server clients do not need it. Listed origins get GET/POST, the `Content-Type` and `X-Request-ID` headers, and no credentials. |
| `PROMETHEUS_MULTIPROC_DIR` | `/tmp/prometheus` (image); unset elsewhere | Directory where each worker writes its metric files, so a scrape returns the sum over all workers. The image's `ENTRYPOINT` empties it before uvicorn starts. Unset (tests, local runs), `/metrics` serves the process's own counters. |
| `APP_MODEL_PATH` | `src/model/model.pkl` | The trained model. |
| `APP_MODEL_FEATURES_PATH` | `src/model/model_features.json` | The model's feature order. |
| `APP_DEMOGRAPHICS_PATH` | `src/data/zipcode_demographics.csv` | Zipcode demographics, joined to every request. |
| `APP_IMPUTER_PATH` | `src/model/imputer.joblib` | The fitted KNN imputer. |
| `APP_IMPUTER_METADATA_PATH` | `src/model/imputer_metadata.json` | The imputer's hash, parameters, columns and library versions, checked at startup. |
| `APP_SALES_PATH` | `src/data/kc_house_data.csv` | Training sales. Read only by `make fit-imputer` / `make check-imputer` (rebuilding the train split), never by the server. |

Path defaults are resolved from the code's location, not the working directory: `src/` in the
repository, `/app` in the image. A missing or inconsistent artifact stops the server at startup
instead of serving wrong prices.

**`/tmp/prometheus` must be writable.** With a read-only root filesystem, mount a tmpfs or a
volume there, or the workers exit at startup
(`OSError: [Errno 30] Read-only file system: '/tmp/prometheus/...'`):

```bash
docker run --read-only --tmpfs /tmp/prometheus:mode=1777 -p 8000:8000 mle-project-challenge-2026
```

Fixed by the image, **not** meant to be changed: `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and
`MKL_NUM_THREADS` are `1`. Besides performance, the OpenMP count decides how scikit-learn's KNN
breaks exact distance ties, so changing it changes some predictions
([ADR 004](docs/adr/004-serving-performance.md), decision 3).

`make docker-run` also takes `PORT` (host port, default 8000) and `WORKERS`.

## API

[`docs/api.md`](docs/api.md): request fields and bounds, responses, every 422 case, the 500
body, `model_version`, `X-Request-ID`, `/health`, `/ready`, `/metrics`, the log line and the
drift metric. **Breaking change vs v0:** unknown fields are rejected, so a full CSV row (with
columns the model does not use) now returns 422.

## How missing data is handled

Any field except `zipcode` may be `null` or omitted; both mean "missing". The pipeline
(`src/services/imputation.py`, [ADR 003](docs/adr/003-imputation-design.md)):

1. **Validate** the request (types, bounds, no unknown fields). `NaN` and `Infinity` are
   invalid (422), not missing.
2. **Look up the zipcode's demographics.** A missing or unknown zipcode is a 422: the 26
   demographic features depend on it and cannot be imputed.
3. **Nothing missing:** skip imputation. Complete requests take the same path as before.
4. **Exact sqft fill:** if exactly one of `sqft_living`, `sqft_above`, `sqft_basement` is
   missing, compute it from `sqft_living = sqft_above + sqft_basement`, which holds on every
   row of the training data. A negative result means the given values contradict each other;
   the field is then left to KNN.
5. **KNN** for whatever is still missing: the notebook's
   `KNNImputer(n_neighbors=5, weights="distance")` on raw values, fitted once on the model's
   train split (16,209 rows, 33 features) by `make fit-imputer` and loaded at startup
   ([ADR 002](docs/adr/002-persisted-imputer-artifact.md)). Never fitted in a request.
6. **Predict**, and return `imputed_fields`: every field that was missing, however it was
   filled.

### What it costs in accuracy

From [`docs/imputation_report.md`](docs/imputation_report.md): the model's held-out split,
5,297 rows (repeat sales that also occur in train removed), each house field masked on its own,
then all seven. MAPE is against the true sale price; the p90 shift is the 90th percentile of
the change in predicted price vs the same house with complete data. Values are the served
pipeline (`knn+sqft_fill` in
[`evaluation_tables.md`](docs/imputation/evaluation_tables.md)).

| Missing | Filled by | MAPE % | p90 price shift % |
| --- | --- | ---: | ---: |
| nothing (complete) | | 17.77 | |
| `bedrooms` | KNN | 17.87 | 15.15 |
| `bathrooms` | KNN | 17.91 | 13.50 |
| `sqft_living` | exact fill | 17.77 | 0.00 |
| `sqft_lot` | KNN | 19.15 | 22.84 |
| `floors` | KNN | 17.59 | 6.91 |
| `sqft_above` | exact fill | 17.77 | 0.00 |
| `sqft_basement` | exact fill | 17.77 | 0.00 |
| all seven (zipcode only) | KNN | **35.05** | **61.62** |

- One missing field costs at most 1.4 points of MAPE (`sqft_lot`), but at the 90th percentile a
  single prediction moves by up to 22.8% (`sqft_lot`). Clients see which fields were guessed in
  `imputed_fields`.
- One missing sqft field costs nothing: the fill is exact.
- **Weakness: the zipcode-only case is poor, and worse than a simple median.** With no house
  fields, only the demographics are left to measure distance, and they are identical for every
  house in a zipcode. All same-zipcode donors tie at distance 0, and the imputer takes five of
  them in index order: effectively five arbitrary houses from the zipcode. MAPE nearly
  doubles (17.77 → 35.05%). A median imputer does better here (30.63%) but worse on six of the seven
  single fields; a median fallback for this case was not evaluated as a pipeline and is not
  implemented (Known gaps).
- Limits of the evidence: missingness simulated as missing completely at random, one field or
  all seven; one split, no confidence intervals (differences under about 0.1 point are noise).
  Pairs of missing fields were tested only for "no 500" (property-based tests), not for
  accuracy.

Rejected after the evaluation (ADR 003): scaling inside the imputer (zipcode-only MAPE 35.05 →
43.53%) and rounding imputed values (no measurable effect).

## Performance

### Before vs after

One session, interleaved A/B (`make ab-bench`), 3 rounds, medians; 500 requests per run from 8
closed-loop keep-alive clients; server pinned to 6 physical cores (CPUs 0–11), client on CPUs
12–15. Source, method and caveats: [`docs/benchmarks/final.md`](docs/benchmarks/final.md).
These are the only "before vs after" numbers in this README.

| Profile | Before | Before req/s | main, 1 worker | main, 6 workers | Before p50 ms | main p50 ms (1 / 6 workers) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| complete | v0 | 34.5 / 40.9 | 319.2 req/s (**9.2×**) | 815.0 req/s (**19.9×**) | 188.0 / 184.2 | 24.7 / 6.8 |
| one-null-knn | `1697b6a` | 4.8 / 4.8 | 107.6 req/s (**22.3×**) | 204.8 req/s (**42.2×**) | 1656.0 / 1660.0 | 73.7 / 34.8 |
| zipcode-only | `1697b6a` | 4.8 / 4.8 | 86.2 req/s (**18.1×**) | 191.5 req/s (**39.6×**) | 1687.9 / 1660.0 | 92.5 / 39.9 |

- v0 cannot serve requests with nulls (it returns 422), so for the null profiles "before" is
  `1697b6a`: v0's serving code plus the Phase 3 imputation.
- "Before" ran once in each A/B; its two medians are listed as 1-worker A/B / 6-worker A/B.
  v0's varied by 18% between the two.
- 6 workers is the deployment as configured. v0 has no worker setting (one process under
  `--reload`).
- Pinning penalises v0, whose thread pools are unlimited. Unpinned (v0's best case), v0 serves
  44.5 req/s and the ratios are 7.8× (1 worker) and 19.0× (6 workers) on `complete`.
- Memory is the cost: main with six workers uses 801–910 MiB, against 136–237 MiB for the
  "before" arms (one process).

### What each Phase 4 change contributed

Each step was measured against its parent, in its own session
([`docs/benchmarks/phase-4-performance.md`](docs/benchmarks/phase-4-performance.md), decisions in
[ADR 004](docs/adr/004-serving-performance.md)). The ratios are throughput, candidate / parent.
**They are not comparable across steps and must not be multiplied** to reproduce the table
above: each one depends on what was still in the way at that point.

| Step | Change | complete | KNN profiles |
| --- | --- | ---: | ---: |
| 4 | Remove the per-request `print` of the input frame (70% of a complete request's handler time in the profile) | 3.2× | 1.3× |
| 5 | Load model, features and demographics once at startup | 1.16× | within noise |
| 6 | One BLAS/OpenMP thread per process (the pools spun against each other on one-row inputs) | same throughput, CPU per request −79% | 8.0× / 7.0× |
| 7 | `/predict` as a `def` route (threadpool) | 0.78×, later traced to the Nagle floor (step 8b) | 1.15–1.18× |
| 8a | No `--reload`; workers from `WEB_CONCURRENCY` | 2.73× | 1.14–1.29× |
| 10 | `TCP_NODELAY` on every connection (a ~44–48 ms delayed-ACK floor with child processes) | 4.21× with 2 workers | 1.57–1.82× with 2 workers |
| 8b | 6 workers vs 1 | 2.70× | 2.36× / 2.51× |
| 11 | Skip the pandas sqft fill when nothing can be derived | not on this path | 1.18–1.22× (1 worker) |

Step 9 (a re-implementation of the KNN imputer) was rejected; the imputer stays scikit-learn's
`KNNImputer` (`AI_USAGE.md`).

### Cost of observability (Phase 5)

Logging, metrics and request IDs were each measured against the end of Phase 4 (`7257ac9`)
([`docs/benchmarks/phase-5-production.md`](docs/benchmarks/phase-5-production.md),
[ADR 005](docs/adr/005-production-readiness.md)):

- **1 worker** (the image default): throughput within 3% of Phase 4 on every profile (final
  A/B, 500 requests per run). In a longer run (5,000 requests per run, 5 rounds), complete
  requests cost 0.25 ms more CPU (+6%, 3.94 → 4.19 ms) and served 5% fewer requests
  (0.95×).
- **6 workers:** complete requests at 0.82× in the final A/B and 0.91× in the longer run; KNN
  requests within 3.5%. Testing the explanation "a fixed CPU cost that shows only at
  saturation" **confirmed it only in part**. The extra CPU is real (about +0.5 ms per request
  when the six workers are saturated), but in most of the slow runs the server left cores idle
  (4.7–5.5 busy vs 6.2–6.5) and spent *less* CPU per request, which a CPU cost cannot explain.
  What the workers wait on is not known. The JSON log was over the 5% budget with one worker,
  and the metrics with six; both were accepted.

Reproduce any A/B with `make ab-bench CONTROL=<ref> CANDIDATE=<ref|worktree>` (Docker,
cgroup v2; see the Makefile for the CPU layout variables).

## Dependencies

[ADR 001](docs/adr/001-uv-and-python-version.md) has the reasoning and the migration;
[`docs/benchmarks/phase-2-uv.md`](docs/benchmarks/phase-2-uv.md) the verification.

- **uv.** v0 had unpinned `requirements.txt` / `requirements-test.txt` and a floating
  `python:3.10-slim` base image; its working environment existed only in the Docker layer cache.
  Now `pyproject.toml` declares the dependencies and `uv.lock` (committed) fixes every version
  and hash. Every Makefile target runs through `uv run --frozen`, and the images run
  `uv sync --frozen`, so the host, CI and the images use the same versions. uv itself is
  pinned (0.13.7, `[tool.uv] required-version`, and by digest in the Dockerfiles). Add
  dependencies with `uv add` / `uv add --dev`, never with pip.
- **Python 3.12.16**, in `.python-version`, `requires-python` and the digest-pinned
  `python:3.12.16-slim-trixie` base image. Python 3.10 reached end of life in October 2026. On
  3.12.16 `model.pkl` loads without warnings and all 100 baseline predictions are bit-for-bit
  identical to 3.10 (ADR 001).
- **The libraries behind `model.pkl` are pinned exactly** (`==` in `pyproject.toml`):
  `scikit-learn==1.7.2`, `numpy==2.2.6`, `pandas==2.3.3`, `scipy==1.15.3` (KNN distances run
  through it), and `joblib==1.6.0` (it writes and reads `imputer.joblib`). The model was pickled
  with these versions and must not be retrained; another version can refuse to unpickle, warn,
  or silently change predictions. A routine `uv lock --upgrade` can move FastAPI or pytest, but
  never these. Startup also refuses an imputer artifact produced with other scikit-learn,
  joblib or numpy versions (ADR 002).

Added since v0 (v0's runtime list was `fastapi`, `uvicorn`, `pandas`, `scikit-learn`,
`pydantic`, `numpy`; its test list was `pytest`, `pytest-cov`, `httpx`, `hypothesis`):

| Package | Group | Why | Added in |
| --- | --- | --- | --- |
| `pydantic-settings` | runtime | Typed configuration from environment variables (`src/core/config.py`) | `8b24c40` |
| `scipy` (pinned) | runtime | Was already installed through scikit-learn; declared to pin it, since KNN distances go through it | `5526b72` |
| `joblib` (pinned) | runtime | Was already installed through scikit-learn; declared because the imputer artifact is written and read with it directly | `d8e3de7` |
| `prometheus-client` | runtime | `/metrics`; the standard exposition library, and it aggregates several worker processes (multiprocess mode, ADR 005) | `03df7e4` |
| `ruff` (pinned) | dev | Lint and format; same version as the pre-commit hook | `5526b72` |
| `mypy` (pinned) | dev | Type checking (`make typecheck`, pre-commit) | `5526b72` |
| `pre-commit` | dev | Runs ruff and mypy on every commit | hooks `94f14f0`, locked `5526b72` |
| `pandas-stubs` (pinned) | dev | Type information for pandas, which ships none; on pandas' release line | `05cca56` |

Rejected along the way: `threadpoolctl` as a direct dependency (three environment variables do
the same, ADR 004) and `python-json-logger` (the standard library is enough for one formatter,
ADR 005).

## Testing and CI

Times below were measured on the benchmark machine with a warm Docker build cache; a cold
build takes longer.

| Command | What it covers | Time |
| --- | --- | --- |
| `make lint typecheck test` | ruff (lint + format check) and mypy over `src`, `scripts`, `tests` and the hooks; then `make test` | about 35 s (mypy cache warm) |
| `make test` | On the host, through uv. 38 tests for the Claude Code hooks (`.claude/hooks`); 530 unit tests (`tests/unit`: schema and bounds, imputation, artifacts and startup checks, error bodies, request ID, log, metrics, server settings, the benchmark scripts), including property-based tests (hypothesis) that no subset of null fields returns 500; and the **characterization test**: the 100 rows of `future_unseen_examples.csv` must reproduce `tests/fixtures/baseline_predictions.json` (103 tests). Coverage of `src/` is reported (96%), not gated. | about 35 s |
| `make test-docker` | The characterization test inside the test image, with the image's locked libraries | 10 s |
| `make test-integration` | Builds the API image and the test image (Docker Compose), waits for the API's HEALTHCHECK, runs `tests/integration` against the container over HTTP, including the 100-row baseline (102 tests) | 14 s |
| `make test-unit`, `make test-all` | The unit tests in the test image; unit plus integration | |
| `make check-imputer` | Refits the KNN imputer in memory from the train split and fails if the committed `imputer.joblib` or its metadata differ in any way (hash, fitted state, bytes of the dump). `make docker-build` runs it first. | 1 s |
| `make check-equivalence` | **The imputation path, bit for bit.** Inside the service image, every unseen row × all 128 subsets of null house fields goes through the real route; the model's input after imputation, the prediction and `imputed_fields` must match `tests/fixtures/imputation_reference.json`, generated from `1697b6a`'s image (the first version with imputation). Gate for any change to the request path. | about 3 min |
| `make ab-bench` | Interleaved A/B benchmark of two git refs (or the working tree) in Docker, with CPU pinning; reports throughput, latency, server CPU per request and memory (`scripts/ab_bench.py`). Not a pass/fail test. | minutes, by profile |

**What the characterization test protects.** Complete requests must keep their predictions
(`.claude/CLAUDE.md`). The fixture was recorded from v0; its only amendment is row 99 (see
Overview). The fixture is never regenerated unless explicitly asked.

**CI** (`.github/workflows/ci.yml`, GitHub Actions, on every push and pull request; a newer
push cancels the running one):

1. **Lint, types, tests:** `uv sync --frozen`, `make lint`, `make typecheck`, `make test`, a
   coverage table in the job summary, `make check-imputer`.
2. **Image characterization:** `make test-integration`, which builds the production image and
   runs the 100-row baseline against it over HTTP.

`check-equivalence` and `ab-bench` are not in CI: they were run by hand as gates for every
change to the request path in Phases 4 and 5 ([ADR 004](docs/adr/004-serving-performance.md),
[ADR 005](docs/adr/005-production-readiness.md)).

## Known gaps

Still open at the end of the project, each with its source.

**Accuracy**

- **Zipcode-only requests are poor**: MAPE 35.05%, against 17.77% with complete data and
  30.63% for a plain median imputer. Same-zipcode donors all tie at distance 0, so KNN takes
  five arbitrary houses from the zipcode. A median fallback for this case (and per feature for
  `sqft_lot`, where the median also wins: 18.49 vs 19.15%) looks better but was not evaluated as
  a pipeline and is not implemented. It needs its own evaluation and ADR
  ([ADR 003](docs/adr/003-imputation-design.md), [`imputation_report.md`](docs/imputation_report.md)).
- **Limits of the accuracy evidence**: missingness simulated as missing completely at random;
  one split; pairs of missing fields tested only for "no 500"; the contradictory-sqft fallback
  to KNN is unit-tested but its accuracy was never measured (ADR 003).

**Operations**

- **The first two uvicorn startup lines are plain text** on stderr (`INFO:     Started server
  process [1]`, `INFO:     Waiting for application startup.`); every line after them is JSON.
  Fixing it would mean `--log-config`, which changes the Phase 4 `CMD`
  ([ADR 005](docs/adr/005-production-readiness.md)).
- **A 500 logs two tracebacks**: ours, with `request_id`, then uvicorn's "Exception in ASGI
  application" (ADR 005).
- **Scripts that import the app print some log lines twice** (their own handler and ours)
  (ADR 005).
- **`/metrics` is on the API port, without authentication.** Expose it only on the internal
  network (ADR 005).
- **No alert thresholds** are defined for the drift and input-quality metrics
  (`predicted_price_dollars`, `prediction_missing_fields_total`, [`docs/api.md`](docs/api.md)).
- **`/tmp/prometheus` must be writable**; with a read-only root filesystem, mount a tmpfs there
  (Configuration).
- **Base-image digests are bumped by hand**: they are pinned, so OS security patches do not
  arrive on their own, and CI does not check for new ones (ADR 001).

**Performance**

- **Six workers lose 9–18% throughput on complete requests vs Phase 4**, mostly in runs where
  the server leaves cores idle; the cause is not known. Finding it needs profiling under load
  (off-CPU time per worker) ([`phase-5-production.md`](docs/benchmarks/phase-5-production.md),
  "The 6-worker cost").
- **The benchmarks come from one machine** (AMD Ryzen 7 5700U, 8 cores / 16 threads, one cpuset layout, closed-loop
  load of 8 clients); other hardware was not measured ([`final.md`](docs/benchmarks/final.md)).
- **`check-equivalence` and `ab-bench` are not in CI**; they are manual gates.

**Maintenance**

- **`core.server` subclasses uvicorn's `H11Protocol`**, which is not public API, to set
  `TCP_NODELAY`. `tests/unit/test_server_protocol.py` fails if the class moves or if uvicorn
  fixes the underlying bug, at which point the workaround should go (ADR 004).
- **Artifacts are pickles.** `model.pkl` and `imputer.joblib` run code when loaded, so they must
  come only from this repository; the sha256 in the metadata is a consistency check, not tamper
  protection ([ADR 002](docs/adr/002-persisted-imputer-artifact.md)).
- **`model_version` changes on a byte-identical refit** of the imputer, because the metadata's
  `fitted_at` changes; a refit is treated as a deploy (ADR 005).
