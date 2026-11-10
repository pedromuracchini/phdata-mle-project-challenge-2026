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
make test                    # hook tests, unit tests, characterization test (about 30 s)
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
