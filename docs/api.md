# API reference

The service listens on port 8000. FastAPI also serves the generated OpenAPI schema at `/docs`
and `/openapi.json` (`/metrics` is left out of it). Design and reasons:
[ADR 003](adr/003-imputation-design.md) (missing data) and
[ADR 005](adr/005-production-readiness.md) (error contract, observability). Every example
response below was captured from the image built from this repository.

## Breaking change vs v0

**A request with fields the model does not use is now rejected with 422.** v0's schema ignored
unknown keys, so clients could post a full row of `future_unseen_examples.csv` (18 columns)
and the extra 10 columns were dropped. Now every field except `zipcode` is optional and
imputed, so an ignored misspelled key (`"bathroom"`) would silently turn into an imputed field.
The schema therefore forbids unknown fields (ADR 005, decision 3). A full CSV row gets one
`extra_forbidden` error per extra column:

```json
{"detail": [
  {"type": "extra_forbidden", "loc": ["body", "waterfront"], "msg": "Extra inputs are not permitted"},
  {"type": "extra_forbidden", "loc": ["body", "view"], "msg": "Extra inputs are not permitted"},
  ...
]}
```

(10 errors: `waterfront`, `view`, `condition`, `grade`, `yr_built`, `yr_renovated`, `lat`,
`long`, `sqft_living15`, `sqft_lot15`.) **Migration:** send only the 8 fields below.

The other changes vs v0 are additive: fields may be `null` or omitted, the response has two
new keys, and there are new endpoints and headers.

## `POST /predict`

### Request

A JSON object with these fields and no others (`src/api/endpoints.py`, `HomeFeatures`):

| Field | Type | Required | Allowed values |
| --- | --- | --- | --- |
| `zipcode` | string | **yes** | exactly 5 digits (`^[0-9]{5}$`), and present in `zipcode_demographics.csv` |
| `bedrooms` | integer or null | no | 0–330 |
| `bathrooms` | number or null | no | 0–80 |
| `sqft_living` | number or null | no | 0–135,400 |
| `sqft_lot` | number or null | no | 0–16,513,590 |
| `floors` | number or null | no | 0–35 |
| `sqft_above` | number or null | no | 0–94,100 |
| `sqft_basement` | number or null | no | 0–48,200 |

- **Missing** means omitted or `null`; both are imputed (README, "How missing data is
  handled"). `NaN` and `Infinity` are invalid, not missing (422 `finite_number`).
- **`zipcode` is a string.** A JSON number (`98118`) is a 422 (`string_type`): leading zeros
  matter for the lookup.
- **Bounds.** Counts and areas cannot be negative. Each upper bound is 10× the largest value of
  that field in `kc_house_data.csv` (`tests/unit/test_input_bounds.py` recomputes them). They
  only reject clearly impossible values, and they keep the sqft derivation from overflowing.

### Response (200)

Complete request (the first row of `future_unseen_examples.csv`, schema fields only):

```bash
curl -X POST localhost:8000/predict -H "Content-Type: application/json" \
  -d '{"bedrooms": 4, "bathrooms": 1.0, "sqft_living": 1680, "sqft_lot": 5043, "floors": 1.5,
       "sqft_above": 1680, "sqft_basement": 0, "zipcode": "98118"}'
```

```json
{"predicted_price": 458520.0, "imputed_fields": [], "model_version": "3ffdefa1129b-e6ee4278080a"}
```

The same house with `bedrooms`, `floors` and `sqft_basement` set to `null`:

```json
{"predicted_price": 458520.0, "imputed_fields": ["bedrooms", "floors", "sqft_basement"],
 "model_version": "3ffdefa1129b-e6ee4278080a"}
```

| Key | Meaning |
| --- | --- |
| `predicted_price` | Predicted sale price in dollars (float). |
| `imputed_fields` | Every field that was missing, however it was filled (exact sqft fill or KNN), in model feature order. `[]` when nothing was missing. |
| `model_version` | `<sha256(model.pkl)[:12]>-<sha256(imputer_metadata.json)[:12]>`, computed once at startup. The metadata records `imputer.joblib`'s hash, so all three artifacts are covered. It changes whenever an artifact changes, including a byte-identical refit (the metadata's `fitted_at` changes). |

The two predictions above are equal, which is common: the model is a uniform 5-NN, so a price
moves only when an imputed value changes the model's neighbour set. Over the evaluation set, one
missing field that goes to KNN moves the price by 7–23% at the 90th percentile; one missing sqft
field, filled exactly, moves it by 0% ([`imputation_report.md`](imputation_report.md)).

## Errors

All error bodies are JSON. **No error body contains the submitted values or a stack trace.**

### 422: invalid input

FastAPI's shape, reduced to `type`, `loc` and `msg` (no `input`, no `ctx`). One entry per
problem. Each `loc` part is capped at 64 characters, because for an unknown field it is the
client's own key.

| Case | `type` | `loc` | `msg` (as returned) |
| --- | --- | --- | --- |
| `zipcode` missing | `missing` | `["body", "zipcode"]` | `Field required` |
| `zipcode` not 5 digits | `string_pattern_mismatch` | `["body", "zipcode"]` | `String should match pattern '^[0-9]{5}$'` |
| `zipcode` a number | `string_type` | `["body", "zipcode"]` | `Input should be a valid string` |
| zipcode not in the demographics | `unknown_zipcode` | `["body", "zipcode"]` | `Unknown zipcode: no demographic data for it.` |
| unknown field | `extra_forbidden` | `["body", "<key>"]` | `Extra inputs are not permitted` |
| out of range | `greater_than_equal` / `less_than_equal` | `["body", "<field>"]` | e.g. `Input should be less than or equal to 35` |
| `NaN` / `Infinity` | `finite_number` | `["body", "<field>"]` | `Input should be a finite number` |
| malformed JSON | `json_invalid` | `["body", <position>]` | `JSON decode error` |

Example (unknown zipcode):

```json
{"detail": [{"type": "unknown_zipcode", "loc": ["body", "zipcode"],
             "msg": "Unknown zipcode: no demographic data for it."}]}
```

The unknown-zipcode error deliberately has the same shape as the validation errors, so
clients parse one format, and it does not repeat the zipcode.

### 500: unexpected error

```json
{"detail": "Internal Server Error", "request_id": "<the request's X-Request-ID>"}
```

The response also carries the `X-Request-ID` header. The traceback goes to the log with the
same `request_id`, never to the client (`tests/unit/test_server_errors.py`).

## Every response: `X-Request-ID`

- If the request has an `X-Request-ID` header matching `[A-Za-z0-9._-]{1,64}`, it is kept and
  returned. Anything else (missing, too long, other characters) is replaced by a new UUID4.
- The ID is in the response header, in the request's log line, and in the body of a 500.

```
$ curl -si -X POST localhost:8000/predict -H "X-Request-ID: demo-123" ... | grep -i x-request-id
x-request-id: demo-123
```

## Other endpoints

| Endpoint | Purpose | Response |
| --- | --- | --- |
| `GET /health` | **Liveness.** The process serves HTTP; checks nothing else. Used by the image's `HEALTHCHECK`. | `200 {"status": "healthy"}` |
| `GET /ready` | **Readiness.** Model, demographics, imputer and `model_version` are loaded. | `200 {"status": "ready", "model_version": "3ffdefa1129b-e6ee4278080a"}`, or `503 {"status": "not ready"}` |
| `GET /metrics` | Prometheus metrics, text format (`text/plain; version=1.0.0`). **Expose it on the internal network only**: it is served on the API port, with no authentication. | see below |

Startup fails (the process exits) if an artifact is missing or doesn't match its metadata, so
a running server with a `503` from `/ready` is short-lived. Use `/ready` for traffic routing and
`/health` for restarts.

## Metrics

Defined in `src/core/metrics.py`. With several workers, every worker writes its values to
files in `PROMETHEUS_MULTIPROC_DIR` and a scrape (answered by any worker) returns the sum over
all workers (ADR 005, decision 10).

| Metric | Type | Labels | What it counts |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `route`, `status` | Every HTTP request. `route` is the matched route template, or `unmatched`, so clients cannot create label values. |
| `http_request_duration_seconds` | histogram | `method`, `route` | Time from receiving a request to sending its response. Buckets 1 ms to 10 s. |
| `prediction_missing_fields_total` | counter | `feature` | Each house field that was missing (and imputed). The input-quality signal. |
| `predicted_price_dollars` | histogram | | Every predicted price. **The drift signal** (below). |

Excerpt after the demo (`make demo`):

```
http_requests_total{method="POST",route="/predict",status="200"} 3.0
http_requests_total{method="POST",route="/predict",status="422"} 3.0
prediction_missing_fields_total{feature="bedrooms"} 1.0
prediction_missing_fields_total{feature="floors"} 1.0
prediction_missing_fields_total{feature="sqft_basement"} 1.0
predicted_price_dollars_count 3.0
```

### `predicted_price_dollars` as the drift signal

Its bucket bounds are the 1, 5, 10, 25, 50, 75, 90, 95 and 99% quantiles of the training
prices (`kc_house_data.csv`), rounded to $10,000: 150k, 210k, 240k, 320k, 450k, 640k, 890k,
1.16M, 1.96M. On traffic that looks like the training data, the share of predictions in each
bucket stays close to those quantiles (for example, about half below 450k). A sustained move
of predictions between buckets means the input population, or the model's behaviour on it,
has shifted. Read it together with `prediction_missing_fields_total`: a rise in imputed
fields lowers accuracy (the zipcode-only case most), even when prices look stable. No alert
thresholds are defined yet (README, "Known gaps").

## Request log

One JSON line per request on stdout (`src/api/middleware.py`, `src/core/logging.py`), written
by a background thread so a slow stdout never blocks the event loop:

```json
{"ts": "2026-11-10T01:57:58.119Z", "level": "INFO", "logger": "api.requests", "message": "request",
 "request_id": "07c4f988-ac70-4fb9-a2ea-020c1b49a329", "method": "POST", "path": "/predict",
 "status": 200, "latency_ms": 18.153, "imputed_fields": ["bedrooms", "floors", "sqft_basement"],
 "model_version": "3ffdefa1129b-e6ee4278080a"}
```

- It never contains the payload, the query string or the model's features (tested with
  sentinel values, `tests/unit/test_request_log.py`).
- `imputed_fields` is `null` for requests that never reached the prediction (for example a
  422, or `/health`).
- uvicorn's own messages use the same JSON format, and uvicorn's access log is turned off (the
  request log replaces it). Two exceptions are listed in the README's "Known gaps": the first
  two startup lines are plain text, and a 500 logs two tracebacks.
