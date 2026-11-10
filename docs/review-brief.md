# Review brief: Phase 5 (production readiness)

For a reviewer of branch `chore/phase-5-production` (17 commits on top of `main` at 850c637).
The design and its measured cost are in [ADR 005](adr/005-production-readiness.md); the
benchmarks are in [`benchmarks/phase-5-production.md`](benchmarks/phase-5-production.md).
Project rules are in [`.claude/CLAUDE.md`](../.claude/CLAUDE.md).

## What changed

| Commit | Change |
| --- | --- |
| 8c5a386 | **Bug fix.** Hypothesis found a 500: `sqft_above` + `sqft_basement` near the float max overflowed the `sqft_living` fill. All house fields are now `>= 0` and at most 10x their maximum in `kc_house_data.csv`. |
| dc55b55 | `.claude/hooks/guard_git.py` (agent guardrail, not the service): also catches `cd <repo> && git commit`, `VAR=1 git`, `bash -c "git ..."`, `eval`, `-am` and `-F` messages. |
| 661d17d | Control benchmark: 7257ac9 against itself, the noise floor of the session. |
| f722f83 | 422 bodies keep `type`, `loc` and `msg` only; nothing from the request is echoed. |
| 127e8a4 | Unknown request fields are rejected (422 `extra_forbidden`). |
| 153e490 | `model_version` in every `/predict` response. |
| 83444e1 | `X-Request-ID`: a safe incoming ID is kept, otherwise a UUID4; returned on every response. |
| c3f44da | Unhandled exceptions return a JSON 500 with `request_id`; the traceback goes to the log only. |
| 982edda | One structured JSON log line per request on stdout (no payload); uvicorn's access log replaced. |
| 1caaf67 | `GET /ready` (readiness); `/health` stays liveness. |
| a26455d | Image runs as non-root `app` (uid 10001); Python HEALTHCHECK on `/health`; curl removed. |
| 03df7e4 | Prometheus metrics on `GET /metrics`, aggregated across worker processes (multiprocess mode). New dependency: `prometheus-client`. |
| c50efea | CORS off by default (v0 allowed every origin with credentials); `APP_CORS_ALLOWED_ORIGINS` to enable. |
| b616d67 | GitHub Actions: lint, typecheck, tests, `check-imputer`; production image checked against the baseline over HTTP. |
| 4af84ce | Coverage table in the CI job summary (reported, not gated). |
| e499c68 | ADR 005, benchmark results, `AI_USAGE.md` entries. |

**Unchanged:** `model.pkl`, `imputer.joblib`, the imputation logic, the Phase 4 server
settings (CMD, `--http core.server:NoDelayH11Protocol`, `WEB_CONCURRENCY`, one BLAS/OpenMP
thread). Predictions are unchanged: `tests/test_characterization.py` passes against the
untouched fixture, and `make check-equivalence` (100 rows x 128 null patterns, bit for bit)
passed after the commits that touch the request path.

## Worth a close look

- **Performance cost.** Measured against 7257ac9 (end of Phase 4):
  - with one worker (the image default), throughput is within 3% on every profile, at about
    +0.6 ms CPU per complete request;
  - with six workers, complete requests are **18% slower** in throughput (KNN requests within
    3.5%). The cause is unknown: not the HEALTHCHECK, not the metrics directory's filesystem;
    see ADR 005.

  Both overheads were above the 5% budget and were accepted by the project owner.
- **Contract change for clients.** Unknown fields now return a 422. A raw
  `future_unseen_examples.csv` row (18 columns) is rejected; send only the 8 schema fields.
- **Multiprocess metrics.** The ENTRYPOINT clears `PROMETHEUS_MULTIPROC_DIR` before uvicorn
  forks (`Dockerfile`). The directory is mode 1777 so `docker run --user` works.
- **Known gaps** (ADR 005, Consequences):
  - two plain-text uvicorn startup lines on stderr;
  - a 500 logs two tracebacks;
  - `/metrics` is served on the API port.

## How to run

Prerequisites: uv 0.13.7 and Docker. Python 3.12.16 is installed by uv from `.python-version`.

```bash
make setup                  # uv sync --frozen (runtime + dev dependencies)
uv run pre-commit install   # once per clone: enables the commit hooks (ruff, mypy)
make docker-build           # verifies the imputer artifact, builds the API image
make docker-run             # API on http://localhost:8000 (WORKERS=N for N processes)
```

```bash
curl -s localhost:8000/health
curl -s localhost:8000/ready
curl -s -X POST localhost:8000/predict -H 'content-type: application/json' \
  -H 'X-Request-ID: review-1' \
  -d '{"zipcode": "98042", "bedrooms": 3, "bathrooms": 2.0, "sqft_living": 1500,
       "sqft_lot": 5000, "floors": 1, "sqft_above": 1200, "sqft_basement": null}'
curl -s localhost:8000/metrics | grep -E '^(http_requests_total|prediction_missing)'
```

The JSON request log is on the container's stdout (`docker logs housing-api`).

## How to test

| Command | What it runs | Time |
| --- | --- | --- |
| `make lint typecheck test` | ruff, mypy (strict), hook tests, unit + property tests, characterization (in-process), coverage | ~1 min |
| `make test-integration` | builds the API and test images; runs `tests/integration` against the API container, including the 100-row baseline over HTTP | ~1–2 min |
| `make check-imputer` | refits the imputer in memory; fails if the committed artifact differs | seconds |
| `make check-equivalence` | imputation path bit for bit vs the reference: 100 rows x 128 null patterns, in Docker | ~4 min |
| `make ab-bench CONTROL=7257ac9 CANDIDATE=worktree AB_SERVER_CPUS=0-11 AB_CLIENT_CPUS=12-15` | interleaved A/B benchmark (Docker); see the benchmark doc for the layouts | ~2 min |

At the end of the phase: 633 tests passed and coverage of `src/` was 96%.

## API contract

### `POST /predict`

Request: a JSON object with these fields, and no others.

| Field | Type | Required | Allowed |
| --- | --- | --- | --- |
| `zipcode` | string | yes | 5 digits, must be a known zipcode |
| `bedrooms` | integer or null | no | 0–330 |
| `bathrooms` | number or null | no | 0–80 |
| `sqft_living` | number or null | no | 0–135,400 |
| `sqft_lot` | number or null | no | 0–16,513,590 |
| `floors` | number or null | no | 0–35 |
| `sqft_above` | number or null | no | 0–94,100 |
| `sqft_basement` | number or null | no | 0–48,200 |

Omitted and `null` both mean missing, and a missing field is imputed. If exactly one of the
three sqft fields is missing, it is derived from `sqft_living = sqft_above + sqft_basement`;
KNN imputes the rest (ADR 003). NaN and Infinity are invalid.

200 response:

```json
{"predicted_price": 253980.0, "imputed_fields": [], "model_version": "3ffdefa1129b-e6ee4278080a"}
```

- `imputed_fields`: every field that was missing, in model order; `[]` when none.
- `model_version`: `<sha256(model.pkl)[:12]>-<sha256(imputer_metadata.json)[:12]>`.

### Errors

| Status | When | Body |
| --- | --- | --- |
| 422 | Invalid JSON, wrong type, out of range, missing or malformed zipcode, unknown field | `{"detail": [{"type": "...", "loc": ["body", "<field>"], "msg": "..."}]}` |
| 422 | Valid zipcode with no demographic data | the same shape, `type: "unknown_zipcode"` |
| 500 | Unhandled error | `{"detail": "Internal Server Error", "request_id": "..."}` |

422 bodies never contain the submitted values. Each `loc` part is at most 64 characters.

### Other endpoints

| Endpoint | Response |
| --- | --- |
| `GET /health` | `200 {"status": "healthy"}`: liveness, checks nothing else |
| `GET /ready` | `200 {"status": "ready", "model_version": "..."}`, or `503 {"status": "not ready"}` |
| `GET /metrics` | Prometheus text format (not in the OpenAPI schema) |

### Every response

- `X-Request-ID`: the caller's ID if it matches `[A-Za-z0-9._-]{1,64}`, otherwise a new UUID4.
- One log line, for example:

  ```json
  {"ts": "...", "level": "INFO", "logger": "api.requests", "message": "request",
   "request_id": "review-1", "method": "POST", "path": "/predict", "status": 200,
   "latency_ms": 3.1, "imputed_fields": ["sqft_basement"], "model_version": "..."}
  ```

### Configuration (environment)

| Variable | Default | Effect |
| --- | --- | --- |
| `WEB_CONCURRENCY` | `1` | uvicorn worker processes (one per physical core) |
| `APP_CORS_ALLOWED_ORIGINS` | `[]` | JSON list of browser origins allowed by CORS |
| `PROMETHEUS_MULTIPROC_DIR` | `/tmp/prometheus` (image) | metric files shared by the workers; cleared at start |
| `APP_*_PATH` | under `src/` | artifact and data paths (`core/config.py`) |
