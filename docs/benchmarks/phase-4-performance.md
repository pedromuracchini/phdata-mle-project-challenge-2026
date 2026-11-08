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
