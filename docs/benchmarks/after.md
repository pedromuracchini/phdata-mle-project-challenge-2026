# Phase 4: after — main against the final branch

Main (`1697b6a`, the v0 service with Phase 3's imputation) against this branch's final code
(`d1a68a5`), measured in one session with the method used for every Phase 4 step. Step-by-step
numbers, profiles and the reasoning behind each change:
[`phase-4-performance.md`](phase-4-performance.md). The decisions:
[ADR 004](../adr/004-serving-performance.md).

## Method

- `scripts/ab_bench.py` (`make ab-bench`). Images are built from a clean `git archive` of each
  ref. Arms are interleaved (A,B / B,A / A,B), with a fresh container per run.
- 3 rounds; the numbers are medians. N = 500 measured requests per run after 20 warm-up
  requests, 8 closed-loop keep-alive clients.
- The 100 rows of `future_unseen_examples.csv` are cycled, in three profiles:
  - `complete`;
  - `one-null-knn`: one of `bedrooms`, `bathrooms`, `sqft_lot`, `floors` null, always imputed
    by KNN;
  - `zipcode-only`: every house field null.
- Client pinned to CPUs 12–15 (2 physical cores). Server CPU from the container's cgroup
  `cpu.stat`, memory from `memory.current`.
- **Two server layouts**, as agreed for this phase:
  - **pinned**: CPUs 0–11, 6 physical cores, server and client never share a core;
  - **unpinned**: CPUs 0–15, shared with the client.

  Main's unlimited thread pools spin on every core they see, so main runs better unpinned
  (complete 45 vs 19 req/s). The unpinned table is the fairer comparison *for main*.
- The final image runs with **6 workers**, the best measured setting: one per physical core
  available to the server (step 8b). It also runs with **1 worker**, the image default
  (`WEB_CONCURRENCY=1`). Main runs as it shipped: one process under `--reload`.

## Machine

AMD Ryzen 7 5700U (8 cores / 16 threads), 7.1 GiB RAM, governor `performance`, kernel
6.12.115, Docker 29.9.3, client Python 3.12.16. Image ids: main `sha256:7a69e017b8b4…`, final
`sha256:207e667c88ad…`.

## Results

### Pinned (server 0–11), final with 6 workers

[json](phase4-after-pinned-6-workers.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | main | 19.5 | 428.0 | 668.1 | 856.1 | 423.9 | 185 |
| complete | final | 883.6 | 6.3 | 15.7 | 37.0 | 6.2 | 752 |
| complete | **ratio** | **45.3×** | **0.015** | 0.023 | **0.043** | 0.015 | 4.06 |
| one-null-knn | main | 4.8 | 1660.1 | 1740.0 | 1807.9 | 2157.0 | 186 |
| one-null-knn | final | 223.9 | 34.0 | 51.3 | 73.6 | 31.6 | 865 |
| one-null-knn | **ratio** | **46.4×** | **0.021** | 0.029 | **0.041** | 0.015 | 4.65 |
| zipcode-only | main | 4.8 | 1679.8 | 1767.8 | 1804.1 | 2178.4 | 186 |
| zipcode-only | final | 193.4 | 41.6 | 52.7 | 86.8 | 34.1 | 865 |
| zipcode-only | **ratio** | **40.4×** | **0.025** | 0.030 | **0.048** | 0.016 | 4.66 |

### Pinned (server 0–11), final with 1 worker (image default)

[json](phase4-after-pinned-1-worker.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | main | 18.6 | 432.2 | 784.1 | 868.0 | 452.8 | 185 |
| complete | final | 357.7 | 22.1 | 25.8 | 32.9 | 3.8 | 139 |
| complete | **ratio** | **19.2×** | **0.051** | 0.033 | **0.038** | 0.008 | 0.75 |
| one-null-knn | main | 4.8 | 1655.9 | 1739.2 | 1779.9 | 2139.3 | 186 |
| one-null-knn | final | 109.8 | 72.2 | 87.3 | 96.2 | 21.6 | 250 |
| one-null-knn | **ratio** | **22.7×** | **0.044** | 0.050 | **0.054** | 0.010 | 1.34 |
| zipcode-only | main | 4.8 | 1676.0 | 1744.0 | 1804.1 | 2174.4 | 186 |
| zipcode-only | final | 88.0 | 90.3 | 103.6 | 113.3 | 23.8 | 249 |
| zipcode-only | **ratio** | **18.4×** | **0.054** | 0.059 | **0.063** | 0.011 | 1.34 |

### Unpinned (server 0–15), final with 6 workers

[json](phase4-after-unpinned-6-workers.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | main | 45.4 | 175.6 | 180.1 | 230.9 | 72.9 | 178 |
| complete | final | 964.8 | 6.4 | 12.3 | 59.7 | 6.7 | 754 |
| complete | **ratio** | **21.2×** | **0.036** | 0.068 | 0.259 | 0.091 | 4.24 |
| one-null-knn | main | 5.8 | 1376.1 | 1535.8 | 1580.2 | 2491.7 | 178 |
| one-null-knn | final | 229.2 | 30.4 | 50.1 | 66.2 | 30.8 | 865 |
| one-null-knn | **ratio** | **39.3×** | **0.022** | 0.033 | **0.042** | 0.012 | 4.85 |
| zipcode-only | main | 6.0 | 1357.1 | 1504.1 | 1560.3 | 2426.8 | 178 |
| zipcode-only | final | 200.4 | 34.1 | 64.1 | 84.0 | 34.0 | 866 |
| zipcode-only | **ratio** | **33.5×** | **0.025** | 0.043 | **0.054** | 0.014 | 4.86 |

### Summary

- **Throughput: 18–23× with one worker; 21–46× with 6 workers,** depending on the profile and
  the layout. Against main unpinned (its better layout), 6 workers serve complete requests at
  21× and KNN requests at 34–39×.
- **Latency: complete p50 176–432 ms → 6 ms; KNN p50 1.4–1.7 s → 30–42 ms.**
- **CPU per request: complete 73–453 ms → 4–7 ms; KNN 2.1–2.5 s → 22–34 ms** (about 1–2% of
  main's).
- **Memory: one worker uses 139 MiB (complete) to 250 MiB (KNN load), against main's 185 MiB.**
  6 workers use 752–866 MiB, about 130 MiB per extra worker (step 8b).
- **The client caps the 6-worker numbers.** 8 connections keep at most 8 requests in flight,
  so these are lower bounds for that setting, not its ceiling.

## Where the gain came from

Each step against its parent. Steps 2–7 ran with the server unpinned (0–15), steps 8a onwards
pinned (0–11).

| Step | Change | Measured effect (vs parent) |
| --- | --- | --- |
| 4 | Remove the per-request `print(DataFrame)` | complete 3.18×; KNN 1.3× |
| 5 | Load model, features and demographics once (zipcode → row lookup) | complete 1.16×, p99 −33%, CPU −40%; KNN within noise |
| 6 | One BLAS/OpenMP thread per process | KNN **8.05× / 6.96×**, CPU per request −99%; complete CPU −79% (throughput held at the Nagle floor) |
| 7 | `def` route (threadpool) | `/health` under KNN load 95 → 5 ms p50; throughput mixed under the floor (corrected in 8b: `def` beats `async` with 1 worker) |
| 8a | No `--reload` in production; `WEB_CONCURRENCY` | complete **2.73×** (p50 62 → 22 ms); KNN 1.14–1.29× |
| 10 | `TCP_NODELAY` on every connection (uvicorn worker sockets) | with 2 workers: complete **4.21×**, KNN 1.57–1.82×; 1 worker neutral |
| 8b | Measure workers (6 = physical cores) | 6 vs 1 worker: complete **2.70×**, KNN 2.36–2.51×; 12 (hyperthreads) no better |
| 11 | Skip the pandas sqft fill when nothing can be derived | 1 worker: KNN 1.18–1.22×; 6 workers within noise |
| 9 | Re-implement the KNN imputer | dropped at the human's request |

## Correctness

- **Imputed values:** identical to main on all 12,800 calls (100 held-out rows × 128 null
  patterns), at every step (`make check-equivalence`).
- **Predictions:** one change, approved. With one OpenMP thread, scikit-learn's KNN breaks exact
  neighbour-distance ties deterministically. Before, it depended on the host's physical core
  count. 93 of the 12,800 calls are exact ties at the 5th neighbour and changed, one of them
  complete baseline row 99 (564,800 → 573,300, recorded in the fixture's `amendments`). The
  imputation reference was regenerated from main with one thread, and the final code matches it
  bit for bit. The other 99 baseline rows are unchanged.
- **API contract:** unchanged (same request and response schemas, same errors).
