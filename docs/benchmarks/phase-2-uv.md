# Phase 2: uv migration and Python 3.12 (build and runtime numbers)

Phase 2 is a tooling change, not an optimization. The goal of these numbers is to show that
the migration to uv (Step A, Python 3.10.22, same library versions) and then the move to
Python 3.12.16 (Step B, same library versions) changed the build and **did not** regress
request performance. Methodology and machine are the same as [`baseline.md`](baseline.md).
Decision record: [`../adr/001-uv-and-python-version.md`](../adr/001-uv-and-python-version.md).

## Summary

| | v0 (pip, 3.10) | Step A (uv, 3.10.22) | Step B (uv, 3.12.16) |
| --- | ---: | ---: | ---: |
| API image cold build (median) | 50.6 s | 36.5 s | 44.0 s (1 run) |
| API image size | 678.8 MB | 667.1 MB | 696.1 MB |
| Test image size | 688.6 MB | 678.0 MB | 710.8 MB |
| Throughput, back-to-back with v0 control | — | 41.9 vs 41.8 req/s | 44.3 vs 42.5 req/s |
| p50, back-to-back with v0 control | — | 188.3 vs 188.5 ms | 180.0 vs 187.8 ms |
| Predictions vs baseline fixture | baseline | 100/100 within rtol 1e-6 | 100/100 bit-for-bit identical |

The sections below cover Step A in detail, then Step B.

## Step A: Docker builds

Cold builds: `docker builder prune -af` (which also empties uv's cache mount), then
`docker build --no-cache`. The base image was pulled beforehand, so timings exclude its
download but include downloading and installing every Python package. "Before" is the pip
Dockerfile at `main` (`423984b`, built with `git archive main | docker build -f <file> -`),
"after" is this branch. Three rounds, before and after alternating, on the same laptop.

| Image | Before (pip), runs | After (uv), runs | Before median | After median | Change |
| --- | --- | --- | ---: | ---: | ---: |
| API (`Dockerfile`) build time | 46.4 / 50.6 / 50.8 s | 39.2 / 36.5 / 34.6 s | 50.6 s | 36.5 s | −28 % |
| Test (`Dockerfile.test`) build time | 48.9 / 47.9 / 44.3 s | 30.0 / 33.9 / 33.9 s | 47.9 s | 33.9 s | −29 % |
| API image size | 678.8 MB | 667.1 MB | | | −11.7 MB (−1.7 %) |
| Test image size | 688.6 MB | 678.0 MB | | | −10.6 MB (−1.5 %) |

Rebuild after a change under `src/` only: **1.2 s** with uv (the dependency layer stays cached
because it only depends on `pyproject.toml`, `uv.lock` and `.python-version`). The pip
Dockerfile also cached `pip install` while `requirements.txt` was unchanged, but could resolve
different versions whenever the cache was gone; uv installs exactly `uv.lock` every time.

Where the size goes: the venv no longer carries pip/setuptools/wheel and the test image no
longer installs lint/type tools (separate `test` dependency group). Both images are still
dominated by the base image, numpy, scipy, pandas and scikit-learn; the multi-stage build
mostly buys reproducibility, not size.

## Step A: request performance

Reference load profile (`make bench` defaults: 1000 requests, 8 clients, 20 warm-up, 3 runs),
fresh container with no limits, `uvicorn ... --reload` as in the original image.

The first run after the migration ([`phase2-uv-py310-run1.json`](phase2-uv-py310-run1.json))
showed 41.3 req/s and a p95/p99 5–6 % above the recorded baseline. To separate machine drift
from a regression, the original `mle-api:v0-original` image was benchmarked again, immediately
followed by the uv image, in the same session:

| Image | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| Recorded baseline (`baseline.md`, earlier session) | 42.3 | 188.0 | 196.2 | 228.4 | 0 |
| `v0-original`, control re-run ([json](phase2-control-v0-original.json)) | 41.8 | 188.5 | 200.4 | 232.2 | 0 |
| uv, Python 3.10.22 ([json](phase2-uv-py310.json)) | 41.9 | 188.3 | 200.2 | 235.7 | 0 |

Medians of 3 runs. Back to back, the two images are within run-to-run noise (≤ 1.5 % on every
metric); the gap to the recorded baseline is the machine (laptop, `powersave` governor, other
workloads), present in the control too. **No regression.**

One difference from the baseline setup: the benchmark client now runs through `uv run` on
Python 3.10.22 instead of the host's Python 3.13.5. The client is standard library only and is
not the bottleneck (~190 ms per request is server time), and the control run used the same
client, so the comparison above is like for like.

## Step B: Python 3.12.16

Same library versions as Step A; only the interpreter (and the base image,
`python:3.12.16-slim-trixie`) changed.

### Request performance

Same profile and method as Step A: the v0 image as a control, immediately followed by the
3.12 image, in one session.

| Image | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| `v0-original`, control ([json](phase2-control-v0-original-py312-session.json)) | 42.5 | 187.8 | 196.0 | 228.1 | 0 |
| uv, Python 3.12.16 ([json](phase2-uv-py312.json)) | 44.3 | 180.0 | 192.7 | 221.5 | 0 |

Medians of 3 runs. No regression; the 3.12 image is about 4 % faster on throughput and p50,
slightly beyond the run-to-run noise. That is plausibly the faster 3.12 interpreter on the
per-request Python overhead (the request path still re-reads the model and CSV, see
`baseline.md`), but it is one session on a laptop: it is reported, not claimed as an
optimization. Phase 4 measures against these numbers.

### Build

| Image | Cold build (1 run) | Size | vs Step A |
| --- | ---: | ---: | ---: |
| API (`Dockerfile`) | 44.0 s | 696.1 MB | +29.0 MB |
| Test (`Dockerfile.test`) | 39.8 s | 710.8 MB | +32.8 MB |

The single cold-build timings sit between the Step A runs and the pip ones; with one run each
on this laptop they are not evidence of a change in either direction. The size increase is
precompiled bytecode (measured on the API image): the venv's `__pycache__` directories hold 91 MB on 3.12 against 63 MB on
3.10 (`UV_COMPILE_BYTECODE=1`, as pip also compiled), while the 3.12 base image is 6 MB smaller
and the installed packages are about the same size.
