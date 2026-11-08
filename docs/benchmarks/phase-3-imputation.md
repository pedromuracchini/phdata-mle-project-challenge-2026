# Phase 3: cost of imputation (measured, not optimized)

Phase 3 adds null handling. It must not change performance for complete requests; performance
is Phase 4's job. These numbers show that it doesn't, and measure what a request with missing
fields costs. Methodology and machine are the same as [`baseline.md`](baseline.md) and
[`phase-2-uv.md`](phase-2-uv.md).

## Method

- **Load profile:** `make bench` defaults (1,000 measured requests, 8 closed-loop clients,
  20 warm-up requests, 3 runs). The 100 rows of `future_unseen_examples.csv` are cycled. The
  numbers are medians of the 3 runs.
- **`BENCH_PROFILE` (new):** decides which fields are sent as `null`.
  - `complete`: no nulls, the same bodies as every earlier benchmark.
  - `one-null`: field *i* mod 7 is null on row *i*. 3 of the 7 are sqft fields, which the
    identity fill handles without KNN.
  - `one-null-knn`: like `one-null`, but cycling only `bedrooms`, `bathrooms`, `sqft_lot` and
    `floors`, which always go to KNN.
  - `zipcode-only`: every house field is null.
- **Images:**
  - Control: the Phase 2 image built from `main` at `cc02ca4` (`mle-api:phase2-control`,
    `sha256:5aad9f70…`).
  - Phase 3: this branch at `0d3129d` (`sha256:ee8ab1f4…`).
- **Run order:** one fresh container per run, no resource limits, `uvicorn --reload` as in
  every earlier phase. After a 3-minute idle cool-down, the runs went back to back:
  control → complete → one-null → one-null-knn → zipcode-only → control.

## Results

| Image / profile | req/s | p50 | p95 | p99 | Errors | JSON |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Phase 2 control, complete (start) | 39.0 | 200.0 ms | 251.9 ms | 275.9 ms | 0 | [json](phase3-control-start.json) |
| **Phase 3, complete** | **39.1** | **200.1 ms** | **227.9 ms** | **259.8 ms** | 0 | [json](phase3-complete.json) |
| Phase 3, one-null | 8.0 | 1000.0 ms | 1156.4 ms | 1288.0 ms | 0 | [json](phase3-one-null.json) |
| Phase 3, one-null-knn | 4.9 | 1644.1 ms | 1788.1 ms | 1840.7 ms | 0 | [json](phase3-one-null-knn.json) |
| Phase 3, zipcode-only | 5.8 | 1383.7 ms | 1576.6 ms | 1710.8 ms | 0 | [json](phase3-zipcode-only.json) |
| Phase 2 control, complete (end) | 46.0 | 172.0 ms | 176.0 ms | 220.5 ms | 0 | [json](phase3-control-end.json) |
| *Phase 2, recorded ([phase-2-uv.md](phase-2-uv.md), earlier session)* | *44.3* | *180.0 ms* | *192.7 ms* | *221.5 ms* | *0* | |

**Machine drift:** the same control image ran at 39.0 req/s at the start and 46.0 req/s at the
end, an 18 % change within one session. This laptop runs the `powersave` governor alongside other
workloads. So only adjacent runs can be compared, and differences under about 20 % between
non-adjacent runs aren't meaningful.

## What the numbers say

1. **Complete requests: no change.** Phase 3 ran at 39.1 req/s with a p50 of 200.1 ms. The
   control run right before it gave 39.0 req/s and 200.0 ms. The gap to Phase 2's recorded
   44.3 req/s is drift; the control lands at either end of it.
2. **KNN imputation is expensive: about 180 ms per request.**
   - The route is `async def`, so the event loop handles one request at a time, and
     throughput is roughly 1 / service time.
   - Complete requests take about 26 ms each (39 req/s); requests that go through KNN take
     about 205 ms each (4.9 req/s).
   - Under the 8-client load that becomes a p50 of 1.6 s, because requests queue behind each
     other.
   - The cost is per KNN call, not per missing field: `zipcode-only` (seven fields) costs
     about the same as `one-null-knn` (one field). The 18 % gap between those two is within
     the drift.
3. **The sqft fill helps latency too.** `one-null` (3 of 7 rows skip KNN) runs at 8.0 req/s,
   against 4.9 req/s for `one-null-knn`.

## The `print` episode

The first benchmark of this branch measured complete requests at 109.8 req/s with a p50 of
72.0 ms, about 2.7× faster than the control
([json](phase3-complete-without-print.json), different session).
- **Cause:** the route rewrite had dropped v0's per-request `print(input_data)`. Phase 1
  profiled it at 71 % of handler time.
- **Fix:** it was restored in `0d3129d`, so that Phase 4 removes it as its own measured
  optimization. The table above is from after the fix.
- **Logged in:** [`AI_USAGE.md`](../../AI_USAGE.md).
- **For Phase 4:** this run hints at the gain from removing the `print`. It was measured in a
  different session, so it is not a before/after measurement.

## Inputs for Phase 4 (not done here)

- **Profile the KNN path before changing it.** Likely suspects (unconfirmed):
  - `nan_euclidean_distances` from one row to all 16,209 donors, plus `KNNImputer`'s
    per-column bookkeeping;
  - BLAS/OpenMP threads oversubscribing 16 cores for a single-row operation.
- **The `async def` route serializes everything,** and that is what turns 180 ms of CPU work
  into 1.6 s of p50 latency under load. A `def` route on the threadpool is the expected fix.
  Measure it on its own.
- **The remaining per-request work:** `print`, model unpickling, the features JSON and the
  demographics CSV read. Phase 1 measured it; Phase 3 left it in place on purpose.
