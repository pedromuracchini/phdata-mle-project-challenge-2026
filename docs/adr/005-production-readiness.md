# ADR 005: Strict error contract, request IDs, JSON request log and multiprocess metrics

- **Status:** Accepted
- **Date:** 2026-11-09
- **Phase:** 5 (branch `chore/phase-5-production`)

## Context

- Phase 5 makes the service closer to what would run in production (clear errors,
  observability, a hardened image, CI) without changing a prediction and, ideally, without
  losing Phase 4's performance. Every commit was gated on the characterization test, the
  imputation tests and `make lint typecheck test`. Commits that touch the request path were
  also gated on `make check-equivalence` (bit for bit). Each commit that adds per-request work
  was A/B'd against 7257ac9 (end of Phase 4) with a budget of the 5% noise floor:
  [`docs/benchmarks/phase-5-production.md`](../benchmarks/phase-5-production.md).
- Since Phase 3 every field except `zipcode` is nullable and imputed. So, under v0's
  `extra="ignore"`, a misspelled key (`"bathroom"`) was silently dropped and its field imputed.
- v0's 422 bodies echoed the rejected value (`input`, `ctx`); its 500 was Starlette's plain
  text; its only log was uvicorn's access line; it allowed every CORS origin with
  credentials; its image ran as root with curl installed for a compose healthcheck.
- Hypothesis found a request that returned 500: `sqft_above` and `sqft_basement` near the
  float maximum overflowed the deterministic `sqft_living` fill to `inf`.
- The server runs N uvicorn worker processes (`WEB_CONCURRENCY`), and each has its own memory.

## Decision

**Error contract.**

1. A 422 body is FastAPI's shape, `{"detail": [{"type", "loc", "msg"}, ...]}`, and nothing
   else: no `input`, no `ctx`. Each `loc` part is capped at 64 characters, because for an
   unknown field it is the client's own key. The unknown-zipcode error has the same shape
   (`type: unknown_zipcode`) and does not repeat the zipcode.
2. Unhandled exceptions return `500 {"detail": "Internal Server Error", "request_id": ...}`
   with the `X-Request-ID` header. The traceback is logged with the request ID, never sent.

**Input policy.**

3. Unknown fields are rejected (`extra="forbid"`, `type: extra_forbidden`, one error per
   key). The fixture, benchmark and equivalence payloads carry only schema fields (tested).
   A raw `future_unseen_examples.csv` row (18 columns) is now a 422.
4. Counts and areas are `>= 0`; each upper bound is 10x the field's maximum in
   `kc_house_data.csv` (e.g. `bedrooms` 330, `sqft_living` 135,400, `floors` 35). That
   rejects only clearly impossible values and fixes the overflow 500; every sale in the data
   is accepted (tested).

**Identity and health.**

5. `model_version = <sha256(model.pkl)[:12]>-<sha256(imputer_metadata.json)[:12]>`, computed
   once in the lifespan and returned by `/predict` and `/ready`. The metadata records
   `imputer.joblib`'s hash, so all three artifacts are covered. A byte-identical refit still
   changes it (`fitted_at`); a refit is a deploy.
6. `/health` is liveness (no dependency; the image's HEALTHCHECK). `/ready` is readiness:
   200 with `model_version` when every artifact is on `app.state`, 503 otherwise.

**Observability.** Two pure ASGI middlewares (`api/middleware.py`), outermost first:

7. `RequestIDMiddleware`: keeps an incoming `X-Request-ID` matching `[A-Za-z0-9._-]{1,64}`,
   otherwise generates a UUID4; stores it in the request state and returns it as a header.
8. `RequestLogMiddleware` writes one JSON line per request to stdout: `request_id`, `method`,
   `path`, `status`, `latency_ms`, `imputed_fields`, `model_version`. It never logs the
   payload, the query string or the model's features (tested with sentinel values).
   - The line is formatted where it is logged and put on a queue; a `QueueListener` thread
     writes it, so stdout never blocks the event loop (`core/logging.py`, stdlib only).
   - uvicorn's own logs go through the same JSON handler, and `uvicorn.access` is disabled
     (replaced).
   - The same middleware records the HTTP metrics (decision 9).
9. Prometheus on `GET /metrics` (`core/metrics.py`):
   - `http_requests_total{method, route, status}` and
     `http_request_duration_seconds{method, route}`. `route` is the matched route template,
     or `unmatched`, so clients cannot create label values.
   - `prediction_missing_fields_total{feature}`.
   - `predicted_price_dollars`, the basis for drift monitoring. Its buckets are the training
     prices' 1–99% quantiles, rounded to $10k.
10. **Multiprocess metrics.**
    - The image sets `PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus`. The directory is owned by
      `app`, mode 1777, so `docker run --user` runs (`make check-equivalence`) can write too.
    - Every worker writes its values to files there, and any worker answers a scrape with the
      sum.
    - The directory is emptied by the image's `ENTRYPOINT` (a `/bin/sh` one-liner that then
      `exec`s the unchanged CMD) before uvicorn forks, because each worker runs the lifespan.
      Only counters and histograms are used, so a dead worker's files stay correct.
    - Without the variable (tests, local runs) the process registry is served.
    - **Proof** (Docker, 4 workers): 400 `/predict` requests on fresh connections, then 10
      scrapes all reported exactly 400 for the request counter, the missing-field counter and
      the price histogram; there were counter and histogram files for 4 PIDs; after
      `docker restart` the counts were 0. A unit test checks the same with two writer
      processes and one reader.

**Configuration and image.**

11. CORS is off by default: `APP_CORS_ALLOWED_ORIGINS` (a JSON list, empty) adds the
    middleware only when origins are listed. Listed origins get GET/POST, `Content-Type` and
    `X-Request-ID`, `X-Request-ID` exposed, and no credentials.
12. The image runs as `app` (uid 10001) with the code and artifacts owned by root. HEALTHCHECK
    probes `/health` with Python's urllib (every 30 s, every 1 s during a 30 s start period),
    and curl is gone: 684 MB, down from 703 MB (at that commit; 685 MB at the end of the phase,
    with `prometheus-client`). Base images are pinned by digest.
13. CI (GitHub Actions, push and PR) runs two jobs:
    - lint, typecheck, tests (with a coverage summary, not gated) and `check-imputer`;
    - building the production image and running the characterization baseline against it
      over HTTP (`tests/integration/test_characterization_http.py`).

## Measured overhead

Against 7257ac9, final image (4af84ce), medians of 3 rounds, server pinned to 6 cores:

| Layout | Profile | req/s | p50 | server CPU ms/req |
| --- | --- | ---: | ---: | ---: |
| 1 worker | complete | 0.986 | 1.013 | 4.0 → 4.6 |
| 1 worker | one-null-knn / zipcode-only | 0.973 / 0.979 | 1.024 / 1.025 | +1.0 / +1.2 |
| 6 workers | complete | **0.820** | 0.997 | 7.2 → 6.7 |
| 6 workers | one-null-knn / zipcode-only | 0.966 / 0.975 | 1.125 / 0.990 | ≈ |

- **Request ID and contract changes:** within noise.
- **JSON request log:** about +0.3 ms CPU per request, so −6 to −8% throughput on complete
  requests with one worker. That is over budget; the human **accepted** it instead of
  optimizing the formatter.
- **Metrics:** about +0.4 ms more per request with one worker, with throughput still within
  3.5%. With six workers, complete requests lost 10–18% throughput (three 5-round runs plus
  the final run). CPU per request is unchanged; the workers intermittently leave cores idle.
  Neither the HEALTHCHECK nor the directory's filesystem (overlayfs vs tmpfs) explains it; the
  cause is unknown. **Accepted** by the human.

## Alternatives

| Option | Pros | Cons | Why not chosen |
| --- | --- | --- | --- |
| Keep `extra="ignore"` | Raw CSV rows accepted | Misspelled keys silently imputed | Silent wrong inputs are worse than a 422 that names the key |
| Echo `input` in 422s, made JSON-safe | Easier client debugging | Reflects arbitrary client data | The client already has its payload |
| Tight bounds (training min/max) | Rejects outliers | Rejects plausible new houses; predictions for them are the model's job | Only clearly impossible values are rejected |
| `BaseHTTPMiddleware` | Less code | Extra task per request, buffers the body | The phase rule: pure ASGI |
| Write log lines on the event loop (like uvicorn's access log) | Cheapest | A slow stdout blocks every request | Queue + thread; overhead accepted |
| JSON log library (`python-json-logger`) | Maintained formatter | A dependency for ~20 lines | stdlib is enough |
| Per-process `/metrics` (no multiprocess mode) | No shared files, no ENTRYPOINT | A scrape sees one random worker | Totals would be wrong with N workers |
| Clean the metrics dir in the lifespan | No ENTRYPOINT | Each worker would delete the others' files | Must run once, before the fork |
| `--log-config` for uvicorn | JSON from the first line | Changes the Phase 4 CMD | The first two startup lines stay plain text (stderr) |
| Streaming artifact hash | ~70 MiB less memory on the KNN profiles | −5 to −15% on KNN requests (allocator side effect) | Reverted; see `AI_USAGE.md` |
| Profile and optimize the log / metrics overhead | Could recover the 6-worker loss | More code, unknown gain | The human chose to accept the overhead |

## Consequences

- **Clients** must send exactly the schema fields. 422s name the field and the error type,
  never the value. `X-Request-ID` ties a client's report to the log line and, for 500s, to the
  traceback.
- **Operators** get one JSON line per request, readiness on `/ready` and metrics on
  `/metrics` (aggregated across workers). `predicted_price_dollars` and
  `prediction_missing_fields_total` are the signals to alert on for drift and input quality.
  `/metrics` is on the API port; expose it only on the internal network.
- **Known gaps:**
  - The first two uvicorn startup lines are plain text on stderr.
  - A 500 logs two tracebacks: ours with `request_id`, then uvicorn's "Exception in ASGI
    application".
  - Scripts that import the app print some log lines twice (their own handler and ours).
- **Performance:** about 1 ms of CPU per request goes to observability. One worker keeps Phase
  4's throughput within 3%. Six workers lose ~18% on complete requests for a reason not yet
  understood; finding it would need profiling the multiprocess metrics path under load.
  *Update (Phase 6):* a longer run (5,000 requests per run, 5 rounds) measured 0.91× with six
  workers. Part of the loss is the extra CPU per request; most of it came in runs with idle
  cores and less CPU per request, so it is not a CPU cost. The cause is still unknown
  ([`phase-5-production.md`](../benchmarks/phase-5-production.md), "The 6-worker cost").
- **Running the image:** the image needs a writable `/tmp/prometheus` (a read-only root
  filesystem needs a volume or tmpfs there). `docker run --user <uid>` works because the
  directory is mode 1777.
- **Memory:** about +4 MiB per worker from hashing `model.pkl` at startup; +30 to +42 MiB in
  total with six workers.
