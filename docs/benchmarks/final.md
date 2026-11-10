# Final: v0 against main, one session

The headline "before vs after" numbers of the project. All six A/Bs below ran **in one
session**, on 2026-11-10 from 01:14 to 01:42 UTC, one after the other, with nothing else
running. Earlier v0 numbers (`baseline.md`, `phase-2-uv.md`, `after.md`) come from other sessions
and are not comparable with these. The README quotes only this file for "before vs after".

## Arms

| Arm | Ref | What it is | Server |
| --- | --- | --- | --- |
| v0 | `v0-original` (`998c82f`) | The service as received: Python 3.10, unpinned `requirements.txt`. | As shipped: one process under `--reload`, unlimited BLAS/OpenMP threads. |
| v0 + imputation | `1697b6a` | Merge of Phase 3: v0's serving code plus null handling. Used **only** for the two null profiles, which v0 cannot serve. | Same as v0. |
| main | `main` (`7c45362`) | End of Phase 5. | 1 worker (the image default, `WEB_CONCURRENCY=1`) or 6 workers (`-e WEB_CONCURRENCY=6`, one per physical core of the server's cpuset). |

**Why two "before" arms.** v0's request schema requires every field, so a request with `null`
gets 422 back: v0 cannot serve `one-null-knn` or `zipcode-only` at all. For those two profiles
the "before" arm is `1697b6a`, the first commit that serves them. It is labelled `1697b6a`
everywhere, never "v0".

## Method

`make ab-bench` (`scripts/ab_bench.py`), as in Phases 4 and 5:

- images built from a clean `git archive` of each ref;
- arms interleaved A,B / B,A / A,B, a fresh container per run, 3 rounds, **medians** reported;
- 500 measured requests per run after 20 warm-up requests, 8 closed-loop keep-alive clients;
- the 100 rows of `future_unseen_examples.csv` (schema fields only), cycled, in three profiles:
  `complete`, `one-null-knn` (one of `bedrooms`, `bathrooms`, `sqft_lot`, `floors` null, always
  imputed by KNN), `zipcode-only` (every house field null);
- **pinned** layout (the headline): server on CPUs 0–11 (6 physical cores, 12 hardware threads),
  client on CPUs 12–15, so they never share a core;
- **unpinned** layout (v0's best case, `complete` only): server on CPUs 0–15, client on 12–15.
  v0's thread pools spin on every CPU they see, so pinning slows v0 down; the unpinned run
  shows how much.

```
make ab-bench CONTROL=v0-original CANDIDATE=main AB_PROFILES=complete \
    AB_SERVER_CPUS=0-11 AB_CLIENT_CPUS=12-15 [AB_CANDIDATE_ARGS="-e WEB_CONCURRENCY=6"]
make ab-bench CONTROL=1697b6a CANDIDATE=main AB_PROFILES="one-null-knn zipcode-only" \
    AB_SERVER_CPUS=0-11 AB_CLIENT_CPUS=12-15 [AB_CANDIDATE_ARGS="-e WEB_CONCURRENCY=6"]
make ab-bench CONTROL=v0-original CANDIDATE=main AB_PROFILES=complete \
    AB_SERVER_CPUS=0-15 AB_CLIENT_CPUS=12-15 [AB_CANDIDATE_ARGS="-e WEB_CONCURRENCY=6"]
```

Machine: AMD Ryzen 7 5700U (8 cores / 16 threads), 7.1 GiB RAM, governor `performance`,
kernel 6.12.115, Docker 29.9.3, client Python 3.12.16. Image ids: v0
`sha256:fdd29c593ecc…`, main `sha256:eeca81cbc94a…` (full ids and every run in the JSON files).
Every run returned 0 errors.

## Headline (pinned layout)

Medians of 3 rounds. "Before" is v0 for `complete` and `1697b6a` for the null profiles.

| Profile | Before | Before req/s | main, 1 worker req/s | ratio | main, 6 workers req/s | ratio |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| complete | v0 | 34.5 / 40.9 | 319.2 | **9.2×** | 815.0 | **19.9×** |
| one-null-knn | 1697b6a | 4.8 / 4.8 | 107.6 | **22.3×** | 204.8 | **42.2×** |
| zipcode-only | 1697b6a | 4.8 / 4.8 | 86.2 | **18.1×** | 191.5 | **39.6×** |

| Profile | Before p50 ms | main, 1 worker p50 ms | main, 6 workers p50 ms | Before p99 ms | main, 1 worker p99 ms | main, 6 workers p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | 188.0 / 184.2 | 24.7 | 6.8 | 775.8 / 436.4 | 36.2 | 77.1 |
| one-null-knn | 1656.0 / 1660.0 | 73.7 | 34.8 | 1783.8 / 1780.2 | 99.3 | 76.8 |
| zipcode-only | 1687.9 / 1660.0 | 92.5 | 39.9 | 1804.0 / 1788.0 | 114.5 | 88.3 |

How to read it:

- **The "before" arm ran twice**, once in the 1-worker A/B and once in the 6-worker A/B (same
  image, same settings). Both medians are shown as "1-worker A/B / 6-worker A/B". Each ratio uses
  the "before" median of its own A/B. v0's two medians differ by 18% (34.5 vs 40.9 req/s; single
  runs ranged from 33.2 to 41.4) and its p99 by 1.8×. `1697b6a`'s four medians were 4.76–4.85 req/s.
- **6 workers vs 1 process is the deployment as configured**, not the same code with more
  processes. v0 has no worker setting (`--reload` runs one process).
- The 6-worker `complete` p99 (77.1 ms) is the median of three runs of about 0.6 s each; two of
  the three runs had a p99 near 80 ms, one 40 ms. Read the tail in this layout with care
  (`phase-5-production.md`, noise floor: tails up to 15% between identical images).

## v0's best case (unpinned layout, `complete`)

| Arm | req/s | p50 ms | p99 ms | server CPU ms/req | ratio (req/s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| v0 (1-worker A/B) | 44.5 | 179.6 | 220.7 | 61.76 | |
| main, 1 worker | 348.8 | 22.7 | 32.0 | 4.42 | **7.8×** |
| v0 (6-worker A/B) | 44.1 | 180.0 | 222.6 | 61.95 | |
| main, 6 workers | 839.2 | 7.0 | 35.5 | 8.63 | **19.0×** |

Unpinned, v0 serves 44 req/s instead of 35–41 and uses about 62 ms of CPU per request instead
of 100–150 ms: its unlimited thread pools waste less CPU on 16 CPUs than on 12. The ratio for one
worker falls from 9.2× to 7.8×; with six workers it stays at 19–20×. The unpinned layout shares
CPUs 12–15 between the server and the client, so it is not used for the headline.

## Full tables

### complete, v0 vs main (1 worker), pinned

[json](phase6-final-v0-1-worker.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores busy | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | v0-original | 34.5 | 188.0 | 444.0 | 775.8 | 150.58 | 5.20 | 136 |
| complete | main (1 worker) | 319.2 | 24.7 | 31.8 | 36.2 | 4.89 | 1.56 | 158 |
| complete | **ratio** | **9.24×** | 0.132 | 0.072 | 0.047 | 0.032 | 0.30 | 1.16 |

### complete, v0 vs main (6 workers), pinned

[json](phase6-final-v0-6-workers.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores busy | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | v0-original | 40.9 | 184.2 | 248.4 | 436.4 | 99.91 | 4.08 | 153 |
| complete | main (6 workers) | 815.0 | 6.8 | 16.0 | 77.1 | 8.46 | 6.89 | 813 |
| complete | **ratio** | **19.95×** | 0.037 | 0.064 | 0.177 | 0.085 | 1.69 | 5.32 |

### Null profiles, 1697b6a vs main (1 worker), pinned

[json](phase6-final-1697b6a-1-worker.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores busy | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| one-null-knn | 1697b6a | 4.8 | 1656.0 | 1748.0 | 1783.8 | 2155.97 | 10.41 | 186 |
| one-null-knn | main (1 worker) | 107.6 | 73.7 | 88.9 | 99.3 | 22.46 | 2.42 | 250 |
| one-null-knn | **ratio** | **22.28×** | 0.044 | 0.051 | 0.056 | 0.010 | 0.23 | 1.35 |
| zipcode-only | 1697b6a | 4.8 | 1687.9 | 1767.7 | 1804.0 | 2189.03 | 10.44 | 186 |
| zipcode-only | main (1 worker) | 86.2 | 92.5 | 105.1 | 114.5 | 24.73 | 2.12 | 248 |
| zipcode-only | **ratio** | **18.11×** | 0.055 | 0.059 | 0.063 | 0.011 | 0.20 | 1.34 |

### Null profiles, 1697b6a vs main (6 workers), pinned

[json](phase6-final-1697b6a-6-workers.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores busy | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| one-null-knn | 1697b6a | 4.8 | 1660.0 | 1751.8 | 1780.2 | 2141.92 | 10.39 | 186 |
| one-null-knn | main (6 workers) | 204.8 | 34.8 | 62.6 | 76.8 | 32.97 | 7.15 | 910 |
| one-null-knn | **ratio** | **42.23×** | 0.021 | 0.036 | 0.043 | 0.015 | 0.69 | 4.89 |
| zipcode-only | 1697b6a | 4.8 | 1660.0 | 1747.9 | 1788.0 | 2152.57 | 10.39 | 237 |
| zipcode-only | main (6 workers) | 191.5 | 39.9 | 63.9 | 88.3 | 37.34 | 7.15 | 899 |
| zipcode-only | **ratio** | **39.57×** | 0.024 | 0.037 | 0.049 | 0.017 | 0.69 | 3.80 |

### complete, v0 vs main, unpinned

[1 worker json](phase6-final-v0-1-worker-unpinned.json),
[6 workers json](phase6-final-v0-6-workers-unpinned.json)

| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | server cores busy | MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| complete | v0-original | 44.5 | 179.6 | 183.9 | 220.7 | 61.76 | 2.74 | 146 |
| complete | main (1 worker) | 348.8 | 22.7 | 27.9 | 32.0 | 4.42 | 1.54 | 138 |
| complete | **ratio** | **7.84×** | 0.126 | 0.152 | 0.145 | 0.072 | 0.56 | 0.95 |
| complete | v0-original | 44.1 | 180.0 | 188.0 | 222.6 | 61.95 | 2.74 | 145 |
| complete | main (6 workers) | 839.2 | 7.0 | 15.3 | 35.5 | 8.63 | 7.15 | 801 |
| complete | **ratio** | **19.04×** | 0.039 | 0.082 | 0.159 | 0.139 | 2.61 | 5.52 |

## Memory

Memory is the cost side of the speed-up. With one worker main uses 138–250 MiB, about the same
as the "before" arms (136–237 MiB). With six workers each process holds its own copy of the
artifacts: 801–910 MiB in total (ADR 004 budgets about 140 MiB plus 130 MiB per extra worker).

## What this does not show

- **Where the gain comes from.** This file measures only the end points. Each Phase 4 step was
  measured on its own against its parent: [`phase-4-performance.md`](phase-4-performance.md),
  decisions in [ADR 004](../adr/004-serving-performance.md). Those step numbers come from other
  sessions and must not be multiplied together to reproduce the ratios above.
- **Other hardware or core counts.** One machine, one cpuset. The 6-worker figure is for 6
  physical cores; [ADR 004](../adr/004-serving-performance.md) says to set `WEB_CONCURRENCY` to
  the physical cores the container gets.
- **Open-loop or production traffic.** 8 closed-loop clients, 500 requests per run, the same
  100 payloads cycled.
