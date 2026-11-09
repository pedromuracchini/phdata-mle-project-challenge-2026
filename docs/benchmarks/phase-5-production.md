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
