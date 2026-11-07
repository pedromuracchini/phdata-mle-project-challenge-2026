# Phase 2: uv migration (build and runtime numbers)

Phase 2 is a tooling change, not an optimization. The goal of these numbers is to show that
the migration to uv (Step A, Python 3.10.22, same library versions) changed the build and
**did not** change request performance. Methodology and machine are the same as
[`baseline.md`](baseline.md).

## Docker builds

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

## Request performance

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
