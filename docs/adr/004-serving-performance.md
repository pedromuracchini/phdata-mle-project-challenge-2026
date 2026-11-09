# ADR 004: Serve with one BLAS/OpenMP thread per worker, TCP_NODELAY, and artifacts loaded once

- **Status:** Accepted
- **Date:** 2026-11-08
- **Phase:** 4 (branch `perf/phase-4-performance`)

## Context

- Phase 4 removes per-request work and serial bottlenecks from `POST /predict` without
  changing any imputed value. Predictions may change only with the human's approval.
  Every step was measured with an interleaved A/B against its parent
  (`scripts/ab_bench.py`) and gated on the characterization test, `make check-imputer` and
  `make check-equivalence` (100 held-out rows × 128 null patterns, bit for bit). Full numbers:
  [`docs/benchmarks/phase-4-performance.md`](../benchmarks/phase-4-performance.md), summary in
  [`docs/benchmarks/after.md`](../benchmarks/after.md).
- Profiling main (step 3) found, per request:
  - a `print` of the input frame (70% of a complete request);
  - unpickling the model and reading the demographics CSV;
  - numpy's OpenBLAS pool and scikit-learn's OpenMP pool spinning against each other on
    one-row inputs (a KNN request used 1.9 s of CPU on 15.7 cores).
- Main's production `CMD` ran uvicorn with `--reload`, a development tool, in a single process.
- `.claude/CLAUDE.md` requires `def` routes for CPU-bound work, artifacts loaded once in the
  `lifespan`, and no change to complete-request predictions without approval.

## Decision

1. **Load the model, its feature order, the demographics and the imputer once**, in the
   `lifespan` (`main.load_artifacts`). Demographics become a zipcode → model-order float64 row
   lookup (`core.artifacts.load_demographics`). The request path reads no file; a test fails
   on any read under `src/`.
2. **One BLAS/OpenMP thread per process** (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
   `MKL_NUM_THREADS` = 1 in `Dockerfile`, `Dockerfile.test` and the Makefile's `THREAD_ENV`,
   kept in sync by `tests/unit/test_thread_limits.py`). Concurrency comes from worker
   processes. The environment variables are enough, so threadpoolctl is not a direct
   dependency.
3. **The OpenMP thread count is part of the result.** scikit-learn's brute-force KNN breaks
   exact distance ties by how it chunks the training set across threads. v0 ran with the
   default, the host's physical core count, so its predictions for tied rows depended on the
   host. One thread makes them deterministic. With the human's approval, baseline row 99
   (an exact tie at the 5th neighbour) was amended from 564,800 to 573,300. The amendment is
   recorded in the fixture's `amendments`. The imputation reference was regenerated from main
   with one thread; 93 of 12,800 calls changed, all exact ties, with identical imputed values.
4. **`/predict` is a `def` route** (FastAPI threadpool), so inference never blocks the event
   loop. `/health` stays responsive under load: p50 5 ms instead of 95 ms under KNN load.
5. **No `--reload` in production.** Worker count comes from uvicorn's own `WEB_CONCURRENCY`
   (image default 1; set it at deploy time to the physical cores the container gets).
   Development reload is `make docker-dev`.
6. **Serve with `--http core.server:NoDelayH11Protocol`**: uvicorn's h11 protocol plus
   `TCP_NODELAY` in `connection_made`. With `--workers` or `--reload`, uvicorn binds the socket
   with `proto=0`, asyncio then skips `TCP_NODELAY`, and every response waited ~44 ms for the
   client's delayed ACK. `tests/unit/test_server_protocol.py` reproduces the bug on a
   uvicorn-bound socket, so an upstream fix shows up as a failing test.
7. **The imputer stays scikit-learn's `KNNImputer`.** The only change near it skips the pandas
   sqft fill when nothing can be derived.

## Alternatives

| Option | Pros | Cons | Why not chosen |
| --- | --- | --- | --- |
| Limit only OpenBLAS, keep OpenMP at its default | Bit-exact with the old baseline on this host; removes the contention | Predictions still depend on the host's core count; 8 OpenMP threads per worker break workers × threads ≤ cores | Human chose deterministic one-thread results |
| `OMP_NUM_THREADS=8` set explicitly | Bit-exact with the old baseline on any host | Only one worker fits the core budget | Same |
| threadpoolctl limits in code | Works even if the environment is wrong | New direct dependency, for what three variables already do | Not needed |
| Keep `async def` for `/predict` | With 6 workers, 13% faster on complete requests | Blocks `/health` behind inference; ~10% slower on KNN; violates CLAUDE.md | Rejected (step 8b) |
| `def` + a one-thread limiter per worker | Lower CPU per request | No throughput gain beyond noise; one more setting | No measurable gain |
| `sitecustomize` patch of asyncio's `_set_nodelay` | Smallest change | Global monkeypatch of the standard library | Less explicit than a selected protocol |
| Pre-bind the socket with `proto=IPPROTO_TCP` and pass `--fd` | No uvicorn subclass | Needs a launcher script | More moving parts |
| Upgrade uvicorn | Maybe fixed upstream | Lockfile change, unverified | Not checked; the test will tell |
| Re-implement `KNNImputer.transform` bit for bit | Estimated ~2× less KNN CPU (not measured) | ~80 lines tied to scikit-learn internals | Rejected by the human (AI_USAGE.md) |
| An image default of `WEB_CONCURRENCY` = cores seen by the container | Fast without configuration | Needs an entrypoint script; the image cannot know the deployment's quota | Deploy-time setting is explicit |

## Consequences

- **Measured gains, main → final**, same session, same method: see
  [`after.md`](../benchmarks/after.md). The largest single steps were removing the `print`
  (complete 3.18×), the thread limit (KNN 7–8×), dropping `--reload` (complete 2.73×),
  `TCP_NODELAY` (complete 4.21× with 2 workers) and workers (6 workers 2.4–2.7× of one).
- **Throughput scales with workers up to the physical cores.** Hyperthreads added nothing (12
  workers on 6 cores) and doubled memory. Each worker holds its own artifacts: budget about
  140 MiB plus 130 MiB per extra worker.
- **Changing the OpenMP thread count changes predictions for tied rows.** CLAUDE.md says to ask
  before changing it, and the gates would catch it.
- **`core.server` depends on uvicorn's `H11Protocol` class path**, which is not public API.
  `test_server_protocol.py` fails if it moves, and also if uvicorn fixes the bug (then drop the
  workaround). If `httptools` is ever installed, `--http` still selects h11 explicitly.
- **Follow-up (Phase 5):** structured request logging and metrics will add per-request work.
  Measure them with `make ab-bench` against this branch's final commit.
