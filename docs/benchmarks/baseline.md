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
   pools; the KNN uses `_fit_method == "brute"`. The same container started with only
   `-e OMP_NUM_THREADS=1` (600 requests, M = 8) used **100 %** CPU at the **same 42.4 req/s**
   (p50 187.7 ms): the OpenMP pool burns ~1.6 cores without adding throughput (see the profile
   section, bottleneck 4).
3. **Keep-alive connections add ~44 ms per request when the server is idle.** One client on a
   keep-alive connection: p50 68 ms. A new connection per request (`curl`, or `http.client` from
   inside the container): ~23–24 ms. The same 68 ms appears from inside the container (so not the
   Docker port proxy), with client `TCP_NODELAY`, and with the whole request sent in a single
   write, so it comes from the server side. The ~40 ms size and the pattern (first request on a
   connection fast, later ones slow) are typical of Nagle's algorithm interacting with delayed
   ACKs; **root cause not confirmed**. At M = 8 it is hidden behind queueing, but it will set a
   latency floor once per-request work drops in Phase 4.

## Profile of one `/predict` request

Tool: **cProfile** (standard library, nothing to install, no `SYS_PTRACE` needed as py-spy
would be). Script: [`scripts/profile_predict.py`](../../scripts/profile_predict.py), run inside
`mle-api:v0-original` from `/app` so relative paths resolve as in production:

```sh
docker run --rm -w /app --user "$(id -u):$(id -g)" \
  -v "$PWD/scripts:/scripts:ro" -v "$PWD/docs/benchmarks:/out" \
  mle-api:v0-original python /scripts/profile_predict.py --out-dir /out
```

It calls the real route function `api.endpoints.predict` with the reference payload (zipcode
`98042`): 1 warm-up call, then **1 profiled call**, then an aggregate over 50 calls. It also times
each handler step **without** the profiler (median of 50), because cProfile inflates pure-Python
code such as pandas formatting (handler ≈ 40 ms under the profiler vs ≈ 22 ms without).
Scope: the handler body only; FastAPI validation and uvicorn's HTTP handling are not included.

Full output: [`baseline-profile.txt`](baseline-profile.txt) (pstats by cumulative and internal
time, single call and aggregate, plus step timings). The raw `.prof` files are gitignored;
regenerate them with the command above.

### Where the time goes (no profiler, median of 50, ms)

| # | Step (`src/api/endpoints.py`) | Line | ms | Share |
| --- | --- | --- | ---: | ---: |
| 7 | `print(input_data)`: pandas formats the 1×34 DataFrame to a string | 49 | **15.54** | **71 %** |
| 4 | `pd.read_csv` of `zipcode_demographics.csv` (disk read + parse) | 42 | 1.86 | 9 % |
| 9 | `model.predict` (RobustScaler + brute-force KNN over the training set) | 55 | 1.84 | 8 % |
| 1 | `open` + `pickle.load` of `model.pkl` (disk read + unpickle) | 33-34 | 0.91 | 4 % |
| 5 | zipcode filter + `drop` + `reset_index` on the full demographics table | 43-45 | 0.55 | 3 % |
| 3 | `pd.DataFrame([home_features.dict()])` | 39 | 0.44 | 2 % |
| 8 | `input_data[model_features]` | 52 | 0.31 | 1 % |
| 6 | `pd.concat(axis=1)` | 48 | 0.19 | 1 % |
| 2 | `open` + `json.load` of `model_features.json` | 36-37 | 0.11 | <1 % |
| | **Sum** | | **21.74** | |

The sum (~22 ms) matches the ~24 ms of server time per request implied by the benchmark
(1000 / 42.3 req/s); the remaining ~2 ms is request parsing, validation and HTTP handling.
The cProfile run agrees on the ranking: `builtins.print` → `DataFrame.__repr__` →
`to_string` is 29 of 39 ms cumulative under the profiler.

### Bottlenecks, in order of cost

Not fixed in this phase. For Phase 4, one optimization per commit, measured with `make bench`.

1. **`print(DataFrame)` on every request: ~15.5 ms, 71 % of handler time.** Formatting a
   34-column frame for stdout (including fitting columns to terminal width) costs far more than
   the prediction itself. It is also unstructured logging (Phase 5 replaces it with JSON logs).
2. **Per-request disk reads and parsing: ~2.9 ms, 13 %.** `read_csv` of the demographics
   (1.86 ms), unpickling the model (0.91 ms), loading the feature list (0.11 ms), plus 0.55 ms
   filtering the full demographics table instead of a precomputed `zipcode -> row` lookup.
   CLAUDE.md requires loading these once in `lifespan`.
3. **All work runs on the event loop (`async def` route).** Requests are fully serialized:
   1 core at 100 %, p50 = 8 × service time at M = 8. Moving the CPU-bound handler to a `def` route
   (threadpool) is required by CLAUDE.md; whether it raises throughput depends on how much of
   the work releases the GIL, so it must be measured, not assumed.
4. **OpenMP thread pool sized to all 16 CPUs for a 1-row prediction.** `model.predict` takes
   1.84 ms with the default pool and 2.04 ms with `OMP_NUM_THREADS=1` (no latency benefit), while
   the default pool raises container CPU from 100 % to ~260 % at the same throughput. That is
   wasted CPU, and it would compete with request threads once the route uses a threadpool.
5. **Keep-alive latency floor of ~44 ms** (observation 3 above): not CPU, not visible in the
   profile, but it will dominate single-client latency once items 1–2 are gone.
6. **Smaller pandas overheads (~1 ms):** building a DataFrame from a dict, `concat` and column
   selection for a single row. Worth revisiting only after items 1–4.
