# Phase 4: performance, step by step

Each Phase 4 commit removes one piece of repeated or serial work. This file records how each
one was measured and what it changed. The final before/after summary is
[`after.md`](after.md); the design decisions are in ADR 004.

## Method (used for every number in this file)

`make ab-bench` ([`scripts/ab_bench.py`](../../scripts/ab_bench.py)) compares two images in one
session:

- **Images.** Control is a git ref built from a clean `git archive`. Candidate is the working
  tree, or another ref. Image IDs and git SHAs are stored in each result file.
- **Interleaving.** 3 rounds. Each round runs every profile on both arms back to back: A,B in
  rounds 1 and 3, B,A in round 2, so drift doesn't always penalise the same arm. Every run gets
  a fresh container: start it, wait for `/health`, send 20 sequential warm-up requests, then
  one measured phase.
- **Load.** The closed loop of `scripts/benchmark.py`: 8 keep-alive clients, **N = 500**
  measured requests per run, the 100 rows of `future_unseen_examples.csv` cycled.
- **Profiles.**
  - `complete`: no nulls.
  - `one-null-knn`: one of `bedrooms`, `bathrooms`, `sqft_lot`, `floors` null in turn. Always
    goes to KNN.
  - `zipcode-only`: every house field null.
- **Reported value.** For each arm, the median of the 3 rounds, plus candidate / control. A gain
  counts only if it is candidate vs its parent commit from the same session.
- **Measured besides latency.**
  - Server CPU, from the container's cgroup `cpu.stat` over the measured phase: CPU-ms per
    request and average cores busy.
  - Client CPU, from `getrusage`, to catch the client becoming the bottleneck.
  - Container memory and the RSS of each process in the container.
- **CPU layout.** The client is pinned to CPUs 12–15.
  - **Steps 2–7:** the server is not pinned (all 16 CPUs). The smoke test showed that pinning
    `main` to CPUs 0–11 makes its unrestricted OpenMP pool about 2.2× slower: 16.5 vs
    36.0 req/s and 467 vs 85 CPU-ms per request, complete profile, 60 requests × 2 rounds, a
    quick check only. With `OMP_NUM_THREADS=1` the pinned image ran at 36 req/s again. Pinning
    before the thread limit (step 7) would turn a benchmark artefact into a fake gain.
  - **From step 8 (workers):** the server is pinned to 0–11, so the client can't steal CPU from
    the workers.
  - The final table reports both layouts.

## Correctness gates (every commit)

Each commit must also pass:

- **`make lint typecheck test`.** This includes the characterization test (complete requests
  bit-for-bit against `tests/fixtures/baseline_predictions.json`, untouched), the hand-computed
  KNN reference and the 128 null-combination tests.
- **`make check-imputer`.** The committed imputer must match a refit.
- **`make check-equivalence`** ([`scripts/check_equivalence.py`](../../scripts/check_equivalence.py)).
  This closes a gap the other gates leave: the API doesn't return imputed values, and the
  null-combination tests only check for a 200.
  - It calls the real route in-process inside the service image, for the 100 unseen rows × all
    128 null patterns (12,800 calls).
  - For each call it hashes the exact float64 bytes of the 33 values the model receives (after
    imputation), the predicted price and `imputed_fields`.
  - It compares the result, per pattern, with
    [`tests/fixtures/imputation_reference.json`](../../tests/fixtures/imputation_reference.json),
    generated once from main's image (`1697b6a`, default thread pools).
  - Step 4's commit reproduced the reference exactly in a separate run, which shows the check is
    deterministic.
  - It takes about 32 minutes on main because of the thread contention found in step 3, so it is
    not part of `make test`.

## Machine

| | |
| --- | --- |
| CPU | AMD Ryzen 7 5700U, 8 cores / 16 threads |
| Governor | `performance` (Phase 3 recorded `powersave`) |
| RAM | 7.1 GiB; desktop applications closed for the benchmark sessions (3.9 GiB available) |
| OS | Linux 6.12.115 (Debian 13), Docker 29.9.3, cgroup v2 |
| Client | Python 3.12.16 (host, standard library only) |

## Step 2: control and noise floor (main vs main)

Both arms are `main` at `1697b6a` (the same image), server unpinned.
[json](phase4-step2-control-main-vs-main.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | A | 45.4 | 175.7 | 180.0 | 219.3 | 73.4 | 3.3 | 177.5 |
| complete | B | 44.9 | 176.1 | 180.5 | 220.3 | 73.7 | 3.3 | 177.6 |
| one-null-knn | A | 6.0 | 1348.1 | 1507.8 | 1551.8 | 2442.5 | 14.6 | 178.0 |
| one-null-knn | B | 6.0 | 1340.6 | 1512.2 | 1543.5 | 2440.9 | 14.6 | 178.2 |
| zipcode-only | A | 6.0 | 1336.5 | 1503.8 | 1571.5 | 2428.1 | 14.7 | 178.4 |
| zipcode-only | B | 6.0 | 1347.9 | 1503.8 | 1555.9 | 2433.1 | 14.6 | 177.9 |

- **Noise floor.** The medians of the two arms differ by at most 1.2%. Single runs span 2.5%
  (complete) and 5% (KNN profiles). A change under about 5% is treated as no measurable
  change.
- **Main's numbers are the control for step 3 onwards.** Complete requests: 45 req/s, p50
  176 ms. Requests that go to KNN: 6.0 req/s, p50 1.34 s.
- **The KNN path burns 2.4 CPU-seconds per request on 14.6 cores.** The route is serial, so
  6 req/s means about 165 ms of wall time per request, with nearly every core busy during it.
  That looks like thread oversubscription in the distance computation, not single-threaded
  work. Step 3 profiles it before anything is changed.
- **The client uses 0.01–0.02 cores,** so it is far from being the bottleneck.

## Step 3: where the time goes on main

Two tools:

- **cProfile and per-step timings without the profiler.** `scripts/profile_predict.py`, run
  inside main's image. It calls the handler directly, so HTTP handling is excluded. It now
  takes `--payload complete|knn|zipcode-only` and times the imputation step.
- **Per-thread CPU of the worker under KNN load,** from `top -H`.

### Handler time per step (ms, median of 30 calls after 1 warm-up, no profiler)

| Step | complete | KNN (`bedrooms` null) | KNN, `OMP_NUM_THREADS=1` | KNN, `OPENBLAS_NUM_THREADS=1` | KNN, all thread vars = 1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 `pickle.load` model.pkl | 0.87 | 1.67 | 0.88 | 0.89 | 0.86 |
| 2 `json.load` features | 0.10 | 0.15 | 0.10 | 0.10 | 0.10 |
| 3 `pd.DataFrame` of the request | 0.39 | 0.78 | 0.49 | 0.51 | 0.49 |
| 4 `pd.read_csv` demographics | 1.70 | 5.29 | 1.68 | 1.77 | 1.71 |
| 5 filter zipcode | 0.49 | 0.78 | 0.48 | 0.51 | 0.49 |
| 6 `pd.concat` | 0.16 | 0.23 | 0.15 | 0.15 | 0.15 |
| 7 `print(DataFrame)` | **14.59** | 32.12 | **14.30** | 14.47 | 14.45 |
| 8 select model features | 0.27 | 0.30 | 0.27 | 0.27 | 0.27 |
| 9 `impute_missing` | 0.26 | 30.71 | **12.37** | 12.30 | 12.14 |
| 10 `model.predict` | 1.84 | **108.24** | 1.87 | 1.77 | 1.88 |
| **sum** | **20.68** | **180.26** | **32.58** | 32.74 | 32.53 |

Reports:
[complete](phase4-profile-main-complete.txt), [complete, threads = 1](phase4-profile-main-complete-threads1.txt),
[KNN](phase4-profile-main-knn.txt), [KNN, OMP = 1](phase4-profile-main-knn-omp1.txt),
[KNN, OpenBLAS = 1](phase4-profile-main-knn-openblas1.txt),
[KNN, all = 1](phase4-profile-main-knn-threads1.txt). The `.prof` dumps are gitignored.

### Thread pools under KNN load (main, `top -H` on the worker, 8 clients)

- **Default:** the worker has **38 threads**. About 16 run at 60–80% CPU and 3 more at about
  30%, all during single-row requests. 5.7 req/s, p50 1,424 ms (one informational run).
- **`OMP_NUM_THREADS=1`:** **1 thread.** OpenBLAS also follows `OMP_NUM_THREADS` when
  `OPENBLAS_NUM_THREADS` is unset. 28.2 req/s, p50 280 ms (one informational run, not an A/B;
  step 7 measures this properly).

py-spy was tried and its result isn't used:

- It samples Python threads only. The OpenMP and OpenBLAS workers have no Python frames, so they
  never appear (1 thread sampled).
- `--native` stops the whole process at every sample, which also stops the threads that compete
  with each other. That hides the contention being measured and inflated `print` to 56% of
  samples.

### Findings

1. **A KNN request is dominated by two thread pools fighting, not by KNN.**
   - The imputer's `nan_euclidean_distances` runs on numpy's OpenBLAS pool (pthreads). The
     model's `KNeighborsRegressor` runs on scikit-learn's OpenMP pool. Each pool has 16 threads.
   - After the imputer, OpenBLAS's threads keep busy-waiting, and `model.predict` takes 108 ms
     instead of 1.9 ms. Every other step slows down 2–3× as well.
   - Limiting **either** pool to one thread removes it: 180 → 32.5 ms. That accounts for the
     14.6 busy cores and 2.4 CPU-s per request measured in step 2.
2. **Complete requests: `print` is 70% of the handler** (14.6 of 20.7 ms), as Phase 1 found.
   - Disk reads (pickle, JSON, CSV) cost about 2.7 ms and the pandas glue about 1.3 ms.
     `predict` is 1.8 ms.
   - Thread limits don't change the complete handler's wall time (20.7 vs 20.4 ms). But the
     server burns 73 CPU-ms per complete request against about 22 ms of wall time (3.3 cores
     busy, step 2), which looks like OpenMP threads spinning after `predict`. Step 7 will show
     whether CPU drops.
3. **With thread limits, KNN imputation is the largest remaining cost:** 12.1 ms of the 32.5 ms
   handler. `nan_euclidean_distances` is about 2/3 of the KNN transform under cProfile; the rest
   is scikit-learn's input validation and per-column bookkeeping. That makes step 9 worth doing
   after steps 4–8.
4. **The async route serializes all of this.** Under 8 clients the p50 is about 8× the service
   time (step 2: 176 ms p50 for about 22 ms per complete request).

### Consequence for the order of the steps

Step 6 (`def` route) runs requests in the threadpool, up to 40 at a time. Before the thread limit
(step 7), each of those requests would bring the two 16-thread pools into play concurrently,
which is the contention measured above, multiplied. **Proposed order: step 7 (thread limits)
before step 6 (`def` route)**, so each is measured on its own without the other's artefact.

## Step 4: remove the per-request `print(DataFrame)`

This is a deliberate behaviour change. v0 wrote the 1×34 input frame to stdout on every
request. It is removed with no replacement; Phase 5 adds structured logging. The test that
asserted v0's print is deleted in the same commit.

Control: parent `7eef931`. Candidate: the working tree. Server unpinned.
[json](phase4-step4-remove-print.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | control | 45.8 | 172.1 | 180.2 | 220.1 | 72.8 | 3.3 |
| complete | candidate | 145.7 | 55.9 | 56.5 | 75.1 | 47.8 | 7.0 |
| complete | **ratio** | **3.18** | **0.325** | 0.313 | 0.341 | 0.656 | |
| one-null-knn | control | 6.0 | 1348.2 | 1520.3 | 1564.3 | 2431.4 | 14.7 |
| one-null-knn | candidate | 7.8 | 1047.2 | 1263.5 | 1308.1 | 2028.3 | 15.7 |
| one-null-knn | **ratio** | **1.30** | **0.777** | 0.831 | 0.836 | 0.834 | |
| zipcode-only | control | 6.0 | 1355.8 | 1512.6 | 1563.9 | 2436.1 | 14.6 |
| zipcode-only | candidate | 7.6 | 1063.2 | 1260.7 | 1319.5 | 2075.0 | 15.7 |
| zipcode-only | **ratio** | **1.26** | **0.784** | 0.833 | 0.844 | 0.852 | |

- **Complete requests: 3.2× throughput, p50 172 → 56 ms.** The step 3 profile predicted
  14.6 of 20.7 ms of handler time (70%). The gain in the container is a little larger than that
  share suggests: stdout there goes through Docker's log driver, not a discarded buffer as in the
  profile.
- **KNN profiles: 1.3×.** The print is a smaller share of a request dominated by the thread
  pools (step 3).
- **Server cores went up on complete requests (3.3 → 7.0)** while CPU per request went down
  (73 → 48 ms). With requests arriving 3× faster, the OpenMP pool spends more of its time
  spinning. That is step 6's subject.
- The Phase 3 number "109 req/s without the print" came from a different session and image. It
  is superseded by this A/B, not compared with it.

## Step 5: load the model, features and demographics once at startup

The lifespan (`main.load_artifacts`) now loads:

- the model, checked against `model_features.json`;
- the demographics, as a lookup from zipcode to a read-only float64 row in model feature order,
  with NaN at the house-field positions;
- the imputer, as before.

`services/prediction.py` fills the request's house fields into a copy of the zipcode's row. The
request path no longer reads any file. A test patches `open`, `read_csv`, `pickle.load` and
friends to fail on anything under `src/` and sends complete, one-null and zipcode-only requests.

- **Model input is unchanged.** The frame is now all float64. In v0 it mixed int64 and float64,
  and scikit-learn converted it to float64. `tests/unit/test_prediction.py` compares it with
  v0's construction for every unseen row, 128 null patterns × 5 rows, and `bedrooms` at the
  int64 extremes. `make check-equivalence` matches main's reference on all 128 patterns.
- **`make test`: 75.3 s → 49.7 s** (148 more tests, 505 in total). Before, every test request
  unpickled the model and parsed the CSV.
- **Seen while writing the disk test:** scikit-learn's KNN `predict` calls threadpoolctl on every
  call, and threadpoolctl reads `/proc/self/maps`. That is procfs, not an artifact, but it is
  per-request work. Step 11 (pandas and leftover overhead) will look at it if the profile shows
  it.

Control: `3595962` (same `src/` as the parent `7633fb1`; the commit in between touched only
scripts, tests and the Makefile). Candidate: the working tree. Server unpinned.
[json](phase4-step5-load-once.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | control | 145.0 | 55.9 | 56.4 | 84.0 | 48.0 | 6.9 |
| complete | candidate | 168.0 | 48.0 | 52.0 | 56.0 | 28.7 | 4.8 |
| complete | **ratio** | **1.16** | **0.858** | 0.923 | **0.667** | **0.599** | |
| one-null-knn | control | 8.0 | 1016.0 | 1236.1 | 1287.9 | 1967.4 | 15.7 |
| one-null-knn | candidate | 8.2 | 979.4 | 1187.2 | 1267.9 | 1907.0 | 15.8 |
| one-null-knn | ratio | 1.03 | 0.964 | 0.960 | 0.984 | 0.969 | |
| zipcode-only | control | 7.7 | 1036.6 | 1279.8 | 1338.9 | 2025.9 | 15.7 |
| zipcode-only | candidate | 8.1 | 987.7 | 1236.2 | 1320.0 | 1952.0 | 15.8 |
| zipcode-only | ratio | 1.04 | 0.953 | 0.966 | 0.986 | 0.964 | |

- **Complete requests: 1.16× throughput, p99 −33%, CPU per request −40%** (48 → 29 ms). The
  profile put the removed work at about 4 ms of a request: disk reads 2.7 ms plus the CSV
  filter and concat. With the print gone, that is a sizeable share.
- **KNN profiles: +3–4%, within the noise floor (about 5%).** No measurable change: those
  requests are still dominated by the thread-pool contention, which is step 6.
