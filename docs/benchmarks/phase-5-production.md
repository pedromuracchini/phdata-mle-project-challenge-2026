# Phase 5: production readiness — benchmarks

Every Phase 5 commit that adds per-request work (middleware, request IDs, logging, metrics) is
measured against **`7257ac9`**, the end of Phase 4, with `make ab-bench`. Budget: within the
noise floor on every profile. Final before/after table and the analysis:
[ADR 005](../adr/005-production-readiness.md) (written at the end of the phase).

## Method

Same as Phase 4 ([`after.md`](after.md)): `scripts/ab_bench.py`, images built from a clean
`git archive` of each ref (or the working tree), arms interleaved A,B / B,A / A,B with a fresh
container per run, 3 rounds, medians reported. N = 500 measured requests per run after 20
warm-up requests, 8 closed-loop keep-alive clients, the 100 rows of
`future_unseen_examples.csv` cycled in three profiles (`complete`, `one-null-knn`,
`zipcode-only`).

Layout: server pinned to CPUs 0–11 (6 physical cores), client to 12–15, so they never share a
core. Two worker counts:

- **1 worker**, the image default (`WEB_CONCURRENCY=1`);
- **6 workers** (`-e WEB_CONCURRENCY=6` on both arms), one per physical core of the server: the
  layout where multiprocess metrics matter.

```
make ab-bench CONTROL=7257ac9 CANDIDATE=worktree AB_SERVER_CPUS=0-11 AB_CLIENT_CPUS=12-15
make ab-bench CONTROL=7257ac9 CANDIDATE=worktree AB_SERVER_CPUS=0-11 AB_CLIENT_CPUS=12-15 \
    AB_CONTROL_ARGS="-e WEB_CONCURRENCY=6" AB_CANDIDATE_ARGS="-e WEB_CONCURRENCY=6"
```

Machine: AMD Ryzen 7 5700U (8 cores / 16 threads), 7.1 GiB RAM, governor `performance`,
kernel 6.12.115, Docker 29.9.3, client Python 3.12.16.

## Control: 7257ac9 against itself (A/A)

Both arms are 7257ac9, so every difference is noise. This gives the control numbers for the
phase and the noise floor of this session. Run 2026-11-09 01:48–01:52 UTC.

### 1 worker

[json](phase5-control-aa-1-worker.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | A | 352.4 | 22.2 | 26.5 | 29.1 | 3.9 | 139 |
| complete | B | 354.6 | 22.5 | 26.5 | 29.8 | 3.9 | 139 |
| complete | B / A | 1.006 | 1.01 | 1.00 | 1.02 | 1.01 | |
| one-null-knn | A | 109.2 | 72.3 | 89.7 | 97.7 | 21.7 | 250 |
| one-null-knn | B | 110.1 | 72.0 | 86.2 | 92.3 | 21.6 | 250 |
| one-null-knn | B / A | 1.008 | 0.996 | 0.961 | 0.945 | 0.993 | |
| zipcode-only | A | 86.6 | 92.0 | 106.0 | 117.5 | 24.2 | 248 |
| zipcode-only | B | 87.9 | 90.5 | 103.3 | 110.9 | 23.8 | 248 |
| zipcode-only | B / A | 1.015 | 0.983 | 0.975 | 0.943 | 0.984 | |

### 6 workers

[json](phase5-control-aa-6-workers.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | A | 995.0 | 6.0 | 14.1 | 37.1 | 6.2 | 752 |
| complete | B | 960.9 | 6.1 | 14.6 | 36.3 | 6.4 | 753 |
| complete | B / A | 0.966 | 1.02 | 1.03 | 0.978 | 1.03 | |
| one-null-knn | A | 221.3 | 34.8 | 52.3 | 65.5 | 31.9 | 863 |
| one-null-knn | B | 216.0 | 34.5 | 54.1 | 71.8 | 33.0 | 863 |
| one-null-knn | B / A | 0.976 | 0.992 | 1.03 | 1.10 | 1.03 | |
| zipcode-only | A | 195.2 | 40.2 | 55.9 | 72.8 | 36.3 | 866 |
| zipcode-only | B | 195.4 | 37.1 | 64.5 | 77.1 | 34.4 | 866 |
| zipcode-only | B / A | 1.001 | 0.922 | 1.15 | 1.06 | 0.947 | |

### Noise floor

| Layout | req/s (max \|B/A − 1\|) | p50 | p95 / p99 |
| --- | ---: | ---: | ---: |
| 1 worker | 1.5% | 1.7% | up to 5.7% |
| 6 workers | 3.4% | 7.8% | up to 15% |

- **1 worker** is quiet: throughput and p50 move by less than 2%, well inside the 5% budget.
  Overhead is judged primarily here. `complete` is the tightest case: 3.9 ms of server CPU per
  request, so 5% is about 0.2 ms for everything Phase 5 adds per request.
- **6 workers** is noisier: throughput stays within 3.4%, but p50 moved by 7.8% between two
  identical images (`zipcode-only`), and the tails by up to 15%. In this layout req/s and
  server CPU ms/req are the reliable signals; a p50 difference under ~8% is not evidence of
  a regression. If a result is borderline, the run is repeated rather than read.
- Tails (p95/p99) are reported but not used for the budget in either layout.

## Per-commit measurements

Every row is candidate / control against 7257ac9, medians of 3 rounds (5 for the repeats), as
**req/s / p50 / server CPU ms per request**. Each candidate includes everything committed
before it, so the overheads add up down the table. Commits without per-request work (C1, C2,
C8, C10, C11, C13–C15) were not A/B'd; C11's HEALTHCHECK and C13 are inside the final run.

| Step | Profile | 1 worker | 6 workers |
| --- | --- | --- | --- |
| C3–C6 contract ([1w](phase5-contract-1-worker.json), [6w](phase5-contract-6-workers.json)) | complete | 1.004 / 0.999 / 0.99 | 1.145 / 0.822 / 1.03 |
| | one-null-knn | 1.008 / 0.985 / 1.00 | 1.006 / 0.994 / 1.00 |
| | zipcode-only | 1.002 / 0.999 / 1.00 | 1.013 / 0.979 / 1.00 |
| C7 request ID ([1w](phase5-c7-request-id-1-worker.json), [6w](phase5-c7-request-id-6-workers.json)) | complete | 0.944 / 1.023 / 1.05 | 1.016 / 1.145 / 1.02 |
| | one-null-knn | 1.001 / 0.992 / 0.99 | 0.982 / 1.006 / 0.99 |
| | zipcode-only | 0.993 / 1.008 / 1.00 | 1.002 / 1.023 / 0.98 |
| C7 repeat ([json](phase5-c7-request-id-1-worker-repeat.json)) | complete | 1.047 / 0.950 / 0.95 | |
| C9 request log ([1w](phase5-c9-request-log-1-worker.json), [6w](phase5-c9-request-log-6-workers.json)) | complete | 0.936 / 1.047 / 1.06 | 1.162 / 0.952 / 1.13 |
| | one-null-knn | 0.984 / 1.016 / 1.01 | 0.976 / 0.944 / 0.96 |
| | zipcode-only | 1.001 / 0.998 / 1.00 | 0.993 / 1.020 / 1.02 |
| C9 repeat ([json](phase5-c9-request-log-1-worker-repeat.json)) | complete | **0.922 / 1.067 / 1.07** | |
| C12 metrics ([1w](phase5-c12-metrics-1-worker.json), [6w](phase5-c12-metrics-6-workers.json)) | complete | 0.967 / 1.023 / 1.17 | 0.896 / 0.981 / 1.00 |
| | one-null-knn | 0.972 / 1.029 / 1.04 | 0.985 / 1.069 / 0.99 |
| | zipcode-only | 0.971 / 1.027 / 1.04 | 0.975 / 0.973 / 0.96 |
| C12 repeat ([json](phase5-c12-metrics-6-workers-repeat.json)) | complete | | **0.856 / 1.027 / 1.07** |
| C12 without HEALTHCHECK ([json](phase5-c12-metrics-6-workers-nohealthcheck.json)) | complete | | 0.896 / 0.965 / 0.98 |
| C12 with a tmpfs metrics dir ([json](phase5-c12-metrics-6-workers-tmpfs.json)) | complete | | 0.852 / 1.124 / 0.99 |

Reading it:

- **C3–C6** (no echo, unknown fields rejected, bounds, `model_version`): within noise. The
  6-worker `complete` +14.5% is the control arm running short of cores (5.4 busy vs 6.4).
- **C7** (request ID): the first 1-worker `complete` result was borderline (0.944); the
  5-round repeat came out at 1.047. Within noise.
- **C9** (JSON request log): about **+0.3 ms of server CPU per request**. Throughput for
  `complete` with one worker fell by 6–8% (confirmed by the repeat), over the 5% budget.
  KNN requests (22–25 ms) show nothing. **Accepted by the human.**
- **C12** (Prometheus, multiprocess): about **+0.4 ms more** per request with one worker
  (4.1 → 4.8 ms for `complete`), throughput still within 3.5%. With six workers, `complete`
  lost **10–15%** throughput in three separate 5-round runs, with CPU per request unchanged:
  some runs match the control and some fall to 670–890 req/s with fewer cores busy. Neither
  the HEALTHCHECK nor the directory's filesystem (overlayfs vs tmpfs) explains it; the cause
  is not known. **Accepted by the human.**
- **Streamed hashing (rejected).** A variant of C6 that hashed the artifacts in chunks
  ([1w](phase5-contract-1-worker-streamed.json), [6w](phase5-contract-6-workers-streamed.json))
  used ~70 MiB less memory on the KNN profiles but cost 5–15% on KNN requests. It was
  reverted (`AI_USAGE.md`).

## Final: 7257ac9 against the end of Phase 5 (4af84ce)

Both images built from a clean `git archive`, same method and session. The final image
includes every Phase 5 change: request ID, request log, metrics in multiprocess mode, non-root
user, HEALTHCHECK, CORS off.

### 1 worker

[json](phase5-final-1-worker.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | 7257ac9 | 342.2 | 23.0 | 29.8 | 32.8 | 4.0 | 181 |
| complete | 4af84ce | 337.3 | 23.3 | 28.9 | 31.6 | 4.6 | 161 |
| complete | **ratio** | **0.986** | **1.013** | 0.969 | 0.963 | 1.14 | 0.89 |
| one-null-knn | 7257ac9 | 106.4 | 74.7 | 90.8 | 101.4 | 22.6 | 246 |
| one-null-knn | 4af84ce | 103.5 | 76.5 | 92.3 | 99.9 | 23.6 | 260 |
| one-null-knn | **ratio** | **0.973** | **1.024** | 1.016 | 0.984 | 1.04 | 1.06 |
| zipcode-only | 7257ac9 | 85.1 | 93.3 | 107.2 | 117.7 | 24.7 | 279 |
| zipcode-only | 4af84ce | 83.3 | 95.5 | 110.2 | 120.3 | 25.9 | 279 |
| zipcode-only | **ratio** | **0.979** | **1.025** | 1.028 | 1.023 | 1.05 | 1.00 |

### 6 workers

[json](phase5-final-6-workers.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | 7257ac9 | 897.5 | 7.4 | 15.9 | 38.3 | 7.2 | 753 |
| complete | 4af84ce | 736.0 | 7.4 | 21.9 | 30.5 | 6.7 | 795 |
| complete | **ratio** | **0.820** | **0.997** | 1.379 | 0.796 | 0.93 | 1.06 |
| one-null-knn | 7257ac9 | 203.3 | 34.5 | 63.9 | 81.3 | 33.6 | 874 |
| one-null-knn | 4af84ce | 196.4 | 38.8 | 59.7 | 85.6 | 33.3 | 904 |
| one-null-knn | **ratio** | **0.966** | **1.125** | 0.933 | 1.053 | 0.99 | 1.03 |
| zipcode-only | 7257ac9 | 186.3 | 41.0 | 67.6 | 82.2 | 37.7 | 863 |
| zipcode-only | 4af84ce | 181.8 | 40.6 | 64.3 | 76.9 | 39.9 | 905 |
| zipcode-only | **ratio** | **0.975** | **0.990** | 0.951 | 0.936 | 1.06 | 1.05 |

- **1 worker** (the image default): throughput within 3% of Phase 4 on every profile; CPU per
  request +0.6 ms for `complete` (logging and metrics), +1 ms for KNN.
- **6 workers**: KNN requests within 3.5% on throughput (the one-null-knn p50 +12.5% is inside
  this layout's ~8% p50 noise plus the C12 effect). `complete` requests are 18% below Phase 4,
  the accepted C12 regression.
- **Against v0** (Phase 4's [`after.md`](after.md), same machine and method, another session):
  with one worker, `complete` 337 vs 18.6 req/s (18×) and KNN 103.5 vs 4.8 req/s (22×); with
  six workers, `complete` 736 vs 19.5 req/s (38×) and KNN 196 vs 4.8 req/s (41×).
- Memory is reported, not budgeted: within ±11% with one worker; +30 to +42 MiB with six
  workers (5–7 MiB per worker). About 4 MiB per worker was measured at C6 (hashing
  `model.pkl` at startup); the rest was not broken down.
