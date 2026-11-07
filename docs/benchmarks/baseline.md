# Baseline benchmark: v0-original

Performance of the original service before any change. Phase 4 compares against these numbers
with the **same load profile on the same machine**.

## System under test

| Item | Value |
| --- | --- |
| Code | tag `v0-original` (commit `998c82f`) |
| Image | `mle-api:v0-original` `sha256:fb4a2c91…` (see [`../environment/baseline-freeze.txt`](../environment/baseline-freeze.txt)) |
| Server | `uvicorn main:app --host 0.0.0.0 --port 8000 --reload`, as in the original `Dockerfile`: 1 worker process, plus the reload supervisor; h11 + asyncio (no uvloop/httptools in the image) |
| Container | `docker run -d --rm -p 8000:8000 --name housing-api-v0 mle-api:v0-original`, **no CPU or memory limits**, restarted fresh before the run |

## Machine

| Item | Value |
| --- | --- |
| CPU | AMD Ryzen 7 5700U, 8 cores / 16 threads, 400–4372 MHz, governor `powersave` |
| RAM | 7.1 GiB total, ~5.8 GiB in use by other processes during the run |
| OS | Debian GNU/Linux 13 (trixie), kernel 6.12.115 x86_64 |
| Docker | Engine 29.9.3 |
| Client | host Python 3.13.5 (standard library only), same machine, through the Docker port mapping |
| Load average before run | 1.53 / 1.03 / 0.80 |

It's a laptop with a power-saving governor and other workloads running, so expect a few percent
of run-to-run noise. The three runs below agree within ~2 %.

## Load profile

Defined in [`scripts/benchmark.py`](../../scripts/benchmark.py); run with `make bench` (defaults).

| Parameter | Value |
| --- | --- |
| Payloads | the 100 rows of `src/data/future_unseen_examples.csv`, 8 schema fields, `zipcode` as string, CSV order, cycled |
| Warm-up | 20 sequential requests, not measured |
| Measured requests (N) | 1000 per run |
| Concurrent clients (M) | 8 threads, one keep-alive connection each, closed loop (no think time) |
| Runs | 3, back to back |
| Latency | client side, request sent → response body read |
| Throughput | N / wall time of the measured phase |

Command used: `make bench BENCH_OUTPUT=bench-results/baseline-v0.json`. Raw output:
[`baseline-results.json`](baseline-results.json).

## Results (N = 1000, M = 8)

| Run | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | min (ms) | max (ms) | mean (ms) | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 41.6 | 191.8 | 204.2 | 235.6 | 25.4 | 244.0 | 191.5 | 0 |
| 2 | 42.4 | 187.9 | 196.2 | 228.4 | 29.5 | 236.3 | 187.8 | 0 |
| 3 | 42.3 | 188.0 | 196.0 | 228.2 | 25.2 | 234.6 | 188.3 | 0 |
| **Median of runs** | **42.3** | **188.0** | **196.2** | **228.4** | | | | **0** |

### Supplementary: single client (not the reference profile)

`python3 scripts/benchmark.py --concurrency 1 --requests 300 --runs 1`
([`baseline-results-c1.json`](baseline-results-c1.json)):

| Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | min (ms) |
| ---: | ---: | ---: | ---: | ---: |
| 14.5 | 68.1 | 75.5 | 76.1 | 24.7 |

## Observations

Facts measured during this phase. Nothing was changed.

1. **Requests are processed one at a time.** With 8 clients, throughput is 42 req/s, i.e. about
   24 ms of server time per request (1000 / 42.3 = 23.6 ms), and p50 latency (188 ms) ≈ 8 × 24 ms:
   each request waits for the 7 ahead of it. Under load, the worker's main thread (the event
   loop) sat at **100 % of one core** (per-thread `/proc` sampling inside the container). This is
   consistent with the `async def` route doing all its blocking work on the event loop
   (see [`../exploration.md`](../exploration.md) §1).
2. **Container CPU is ~260 % under that load**, not ~100 %. The worker process has 38 threads;
   besides the event loop, several threads each used ~24 % of a core. `threadpoolctl` inside the
   image shows a 16-thread OpenMP pool (scikit-learn's `libgomp`) and two 16-thread OpenBLAS
   pools; the KNN uses `_fit_method == "brute"`. The extra threads match the OpenMP pool that
   brute-force distance computation runs on, but the profile is where this is confirmed (see the
   profile section).
3. **Keep-alive connections add ~44 ms per request when the server is idle.** One client on a
   keep-alive connection: p50 68 ms. A new connection per request (`curl`, or `http.client` from
   inside the container): ~23–24 ms. The same 68 ms appears from inside the container (so not the
   Docker port proxy), with client `TCP_NODELAY`, and with the whole request sent in a single
   write, so it comes from the server side. The ~40 ms size and the pattern (first request on a
   connection fast, later ones slow) are typical of Nagle's algorithm interacting with delayed
   ACKs; **root cause not confirmed**. At M = 8 it is hidden behind queueing, but it will set a
   latency floor once per-request work drops in Phase 4.

## Profile

Added in the next commit.
