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

## Step 6: one BLAS/OpenMP thread per process

`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` are set to 1 in `Dockerfile`,
in `Dockerfile.test`, and for host tests through the Makefile's `THREAD_ENV`.
`tests/unit/test_thread_limits.py` keeps the three in sync. No dependency was added: the limit
is environment only, so threadpoolctl was not needed.

### The limit changes 93 tie-broken predictions (approved)

The first run of the gates failed: baseline row 99 (564,800 → 573,300) and 93 of 128
equivalence patterns. Dumps of every call's model input and prediction (100 rows × 128
patterns, same image, thread counts set with `-e`) showed:

| OpenMP / OpenBLAS threads | Imputed values vs main | Predictions vs main |
| --- | --- | --- |
| 8 / 1 | identical | identical (all 128 patterns match main's reference) |
| 16 / 16 | identical | 94 differ |
| 16 / 1 | identical to 16 / 16 | identical to 16 / 16 |
| 1 / 16 | identical | 93 differ |
| 1 / 1 | identical | 93 differ |

- **Imputation is unaffected.** The imputer's `nan_euclidean_distances` gives the same values
  with any number of OpenBLAS threads.
- **Only the OpenMP count of the model's KNN search changes predictions.** scikit-learn's
  brute-force search splits the training set into one chunk per thread, and the chunking
  decides which neighbour is kept when two are at exactly the same distance. All 93 changed
  calls are exact ties at the 5th neighbour (checked one by one), across unseen rows 7, 18,
  96 and 99. The changes have a median of 8,500 and a maximum of 54,000 (8.6%).
- **Main runs with 8 OpenMP threads, not 16.** With `OMP_NUM_THREADS` unset,
  scikit-learn 1.7.2 uses `min(omp_get_max_threads(), physical cores)`, which is 8 on this
  machine. So v0's predictions for tied rows depend on the host's physical core count: the
  committed baseline would already fail on a host with another core count.

The human chose one thread and approved amending row 99 of `baseline_predictions.json` to
573,300. The amendment is recorded in the fixture's top-level `amendments`, and `meta` is
unchanged. The human also approved regenerating `imputation_reference.json` from main's image
(`1697b6a4c291`) with one thread per pool. Complete requests on other rows, and all imputed
values, are unchanged.

### A/B

Control: `917584b`. Candidate: the working tree. Server unpinned (0–15), as decided for
steps 2–6. [json](phase4-step6-thread-limits.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | control | 167.6 | 48.0 | 52.1 | 56.5 | 30.3 | 5.1 | 176.9 |
| complete | candidate | 166.9 | 48.0 | 52.1 | 60.2 | 6.4 | 1.1 | 160.7 |
| complete | ratio | 0.996 | 1.00 | 1.00 | 1.06 | **0.21** | | 0.91 |
| one-null-knn | control | 8.2 | 980.1 | 1223.4 | 1267.6 | 1912.3 | 15.7 | 177.9 |
| one-null-knn | candidate | 66.1 | 120.0 | 124.2 | 147.7 | 23.2 | 1.5 | 161.5 |
| one-null-knn | **ratio** | **8.05** | **0.122** | **0.101** | **0.117** | **0.012** | | 0.91 |
| zipcode-only | control | 8.1 | 995.2 | 1203.9 | 1260.5 | 1936.6 | 15.8 | 177.9 |
| zipcode-only | candidate | 56.6 | 140.0 | 144.3 | 172.0 | 27.2 | 1.5 | 161.4 |
| zipcode-only | **ratio** | **6.96** | **0.141** | **0.120** | **0.136** | **0.014** | | 0.91 |

- **KNN requests: 8.0× and 7.0× throughput, p50 −88% and −86%, CPU per request −99%**
  (1.9 s → 23 ms). The server went from 15.7 busy cores to 1.5.
- **Complete requests: same throughput and latency, CPU per request −79%** (30 → 6.4 ms). The
  OpenMP pool was spinning on these too. Throughput did not move because something else bounds
  it: 8 clients at a flat 48 ms p50 give 167 req/s, while the server is 1.1 cores busy. A flat
  ~40–48 ms floor is the signature of the keep-alive latency of step 10; it is checked there
  with a minimal experiment, not assumed here.
- **KNN latency is now serial.** 23 ms of CPU but 120 ms p50 with 8 clients fits the `async`
  route running every request on the event loop, one at a time. That is step 7.
- **Memory: −9%** (177 → 161 MiB), from fewer thread stacks and buffers.
- **`make test`: 49.7 s → 18.6 s.**

## Step 7: `def` route

`predict` is now a `def` route: FastAPI runs it in its threadpool (anyio, 40 threads by
default) instead of on the event loop. CLAUDE.md requires this for CPU-bound work. The
artifacts on `app.state` are only read, and each request gets its own copy of the zipcode's
row. `tests/unit/test_predict_threadpool.py` checks two things: the route is synchronous, and
240 requests sent from 16 threads at once get responses identical to the same requests sent
serially. `make check-equivalence` matches the reference on all 128 patterns.

Control: `d7cd2c1`. Candidate: the working tree. One worker, server unpinned.
[json](phase4-step7-def-route.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | control | 167.1 | 48.0 | 52.0 | 58.3 | 6.4 | 1.1 | 160.8 |
| complete | candidate | 129.5 | 63.3 | 68.3 | 111.0 | 8.5 | 1.1 | 176.7 |
| complete | **ratio** | **0.775** | **1.32** | 1.31 | **1.91** | 1.33 | | 1.10 |
| one-null-knn | control | 63.0 | 127.8 | 128.4 | 155.6 | 24.3 | 1.5 | 161.3 |
| one-null-knn | candidate | 72.5 | 112.2 | 131.5 | 155.5 | 36.5 | 2.7 | 228.2 |
| one-null-knn | ratio | 1.15 | 0.878 | 1.02 | 0.999 | 1.50 | | 1.42 |
| zipcode-only | control | 57.3 | 139.9 | 143.5 | 171.8 | 26.8 | 1.5 | 161.4 |
| zipcode-only | candidate | 67.3 | 120.3 | 144.0 | 163.9 | 38.0 | 2.6 | 241.7 |
| zipcode-only | ratio | 1.18 | 0.860 | 1.00 | 0.954 | 1.42 | | 1.50 |

The direction is the same in every round: complete 128–132 vs 167–168 req/s; KNN 71–73 vs 63
and 64–68 vs 57–59.

- **Complete requests: 0.78× throughput, p99 +91%.** The server is 1.1 cores busy in both
  arms, so the route is not short of CPU. Handing each request to a worker thread and back costs
  CPU (6.4 → 8.5 ms per request) and latency, and with one process all of those threads share
  one GIL.
- **KNN requests: 1.15–1.18× throughput, p50 −12–14%, but CPU per request +42–50%.** The
  numpy and scikit-learn sections release the GIL, so some of the work now overlaps (1.5 → 2.7
  busy cores). The extra CPU goes to GIL contention between the threads.
- **Memory: +10% (complete) to +50% (zipcode only).** Worker threads hold their own buffers
  during the imputer's distance computation.

### `/health` under load (the reason for the rule)

The async route runs inference on the event loop, so every other request waits behind it,
including the health check an orchestrator uses for liveness. Measured with the same two
images: one worker, 8 clients loading `/predict` for 12 s, and one `/health` probe every 50 ms
from a separate connection (client on CPUs 12–15).

| Load | Arm | `/health` p50 ms | p95 ms | max ms |
| --- | --- | ---: | ---: | ---: |
| zipcode-only | control (`async def`) | 94.6 | 103.0 | 239.7 |
| zipcode-only | candidate (`def`) | 4.8 | 13.6 | 21.8 |
| complete | control (`async def`) | 1.9 | 6.6 | 15.2 |
| complete | candidate (`def`) | 2.3 | 12.8 | 50.4 |

- **Under KNN load, the async route delays `/health` by about one KNN batch** (p50 95 ms,
  max 240 ms). The `def` route keeps it at 5 ms. A liveness probe with a short timeout would
  restart a busy but healthy pod.
- **Under complete load, the loop is mostly idle in both arms.** Complete requests use 6 ms of
  CPU but wait about 48 ms (step 10).

Throughput per process is not where this service scales. Step 8 adds workers, and the
`def`-versus-`async` cost is measured again there with several processes.

## Step 8a: serve without `--reload`; worker count from `WEB_CONCURRENCY`

From this step on, the server is pinned to CPUs 0–11 (6 physical cores) and the client to
12–15, as planned.

- **No `--reload` in the production `CMD`.** v0 shipped uvicorn's development reloader. Without
  `watchfiles` installed it uses `StatReload`, which polls every `*.py` under `/app` (3,889
  files, `.venv` included) four times a second. It also serves from a child process.
  `make docker-dev` keeps reload for development: it mounts `src/` read-only and runs
  `--reload --reload-dir /src`. It was checked to reload on a change.
- **Workers via uvicorn's own `WEB_CONCURRENCY`** (image default 1; `make docker-run
  WORKERS=N`). It applies only when `--workers` is not on the command line.
  `tests/unit/test_server_command.py` pins the `CMD` (no `--reload`, no `--workers`) and
  keeps `--reload` in the dev target only.

Control: `facff53` (with `--reload`). Candidate: the working tree, 1 worker.
[json](phase4-step8a-no-reload.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | control | 130.7 | 62.3 | 67.9 | 108.2 | 8.5 | 1.1 | 176.5 |
| complete | candidate | 356.4 | 21.8 | 26.4 | 29.2 | 3.9 | 1.4 | 140.6 |
| complete | **ratio** | **2.73** | **0.351** | 0.389 | **0.270** | **0.459** | | 0.80 |
| one-null-knn | control | 69.8 | 116.2 | 135.3 | 156.3 | 38.3 | 2.7 | 227.7 |
| one-null-knn | candidate | 89.9 | 88.7 | 107.4 | 118.7 | 26.5 | 2.4 | 182.5 |
| one-null-knn | ratio | 1.29 | 0.763 | 0.794 | 0.759 | 0.692 | | 0.80 |
| zipcode-only | control | 66.3 | 124.1 | 145.3 | 167.6 | 39.7 | 2.6 | 217.9 |
| zipcode-only | candidate | 75.6 | 105.2 | 121.6 | 130.3 | 27.2 | 2.1 | 210.4 |
| zipcode-only | ratio | 1.14 | 0.847 | 0.837 | 0.777 | 0.685 | | 0.97 |

- **Complete requests: 2.73× throughput, p50 62 → 22 ms, p99 −73%, CPU per request −54%.**
  KNN requests: 1.14–1.29×, CPU per request −31%. Memory −20% for complete and one-null
  (the reloader process is gone).
- **Most of the latency gain is not the polling: it is `TCP_NODELAY`.** Serving from a child
  process (`--reload`, or `--workers` > 1), uvicorn 0.54 binds the socket itself with
  `socket.socket(family=...)`, so `proto` is 0. asyncio sets `TCP_NODELAY` on accepted
  connections only when `proto == IPPROTO_TCP`, so those connections keep Nagle. h11 writes the
  response head and body in two writes, and the body waits for the client's delayed ACK.
  Minimal experiment: the same image, the benchmark's own client, one keep-alive connection,
  200 sequential complete requests:

  | Server | p50 ms | p90 ms |
  | --- | ---: | ---: |
  | 1 worker (in-process server) | 4.2 | 5.1 |
  | 2 workers | 48.0 | 48.5 |
  | 2 workers, `TCP_NODELAY` forced (4-line `sitecustomize`) | 4.0 | 4.7 |
  | 1 worker, `TCP_NODELAY` forced | 4.3 | 4.9 |

  This is the flat ~48 ms floor seen on complete requests in every earlier step, main
  included, because they all ran `--reload`. It is server-side, not a client artifact. The fix
  is step 10. It comes before the worker measurements (step 8b), because without it any
  `WEB_CONCURRENCY` > 1 brings the floor back: a first sweep of 2, 4, 6 and 12 workers gave
  complete requests 0.41–0.46× of one worker (json: [2](phase4-step8a-sweep-before-nodelay-2.json),
  [4](phase4-step8a-sweep-before-nodelay-4.json), [6](phase4-step8a-sweep-before-nodelay-6.json),
  [12](phase4-step8a-sweep-before-nodelay-12.json)). Memory from that sweep (not affected by the
  floor): each worker is 200–250 MiB RSS under KNN load, the supervisor 30 MiB, and the
  container about 122 MiB more per extra worker (352 MiB with 2 workers, 1,575 MiB with 12).
