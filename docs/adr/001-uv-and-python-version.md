# ADR 001: Manage dependencies with uv and run on Python 3.12

- **Status:** Accepted
- **Date:** 2026-11-07
- **Phase:** 2 (branch `build/phase-2-uv`)

## Context

- `requirements.txt` and `requirements-test.txt` listed package names without versions, and the
  images used the floating `python:3.10-slim` tag. The Phase 1 baseline
  ([`../environment/baseline-freeze.txt`](../environment/baseline-freeze.txt)) only matched
  because of the Docker layer cache: a rebuild on another machine or day could install
  different versions.
- `src/model/model.pkl` (`Pipeline(RobustScaler -> KNeighborsRegressor)`) was pickled with
  scikit-learn 1.7.2 / numpy 2.2.6 / pandas 2.3.3, and KNN distances go through scipy 1.15.3.
  The model must not be retrained. Another version of these libraries can refuse to unpickle,
  warn (`InconsistentVersionWarning`) or silently change predictions.
- The host had none of the runtime dependencies, so the characterization test
  (`tests/test_characterization.py`, 100 recorded predictions) only ran inside Docker, and
  mypy ran with `ignore_missing_imports`.
- Python 3.10 reached end of life in October 2026; it no longer gets security fixes.

## Decision

1. **uv manages dependencies.** `pyproject.toml` declares them (runtime, plus `test` and `dev`
   groups); `uv.lock` (committed) fixes every version and hash. Makefile targets run through
   `uv run --frozen`; images run `uv sync --frozen` in a builder stage and copy the `.venv` into
   the runtime stage. uv itself is pinned (`[tool.uv] required-version`, image tag + digest).
2. **The libraries behind `model.pkl` are pinned with `==` in `pyproject.toml`**:
   scikit-learn, numpy, pandas, scipy. Everything else is fixed only by `uv.lock`. A routine
   `uv lock --upgrade` can therefore move FastAPI or pytest, but never the model's libraries;
   those change only by editing the pin on purpose, followed by the characterization test.
3. **Migrate in two verified steps**:
   - *Step A*: uv with the exact baseline versions on Python 3.10.22. The locked runtime set
     matched the freeze for all 26 packages (`packaging` is absent: it came from the base
     image's `wheel`, not from the app). Pure tooling change, verified with the
     characterization test locally and in Docker and a benchmark against the v0 image.
   - *Step B*: Python 3.12.16 with the same library versions. The lock only dropped
     `exceptiongroup` and `tomli` (needed below 3.11); no version changed.
   Doing both at once would leave any prediction difference with two possible causes.
4. **Run on Python 3.12, exactly 3.12.16**, in `.python-version`, `requires-python`, and
   `python:3.12.16-slim-trixie` pinned by digest in all three Dockerfiles.
   On 3.12.16, `model.pkl` loads with no warnings and all 100 baseline predictions are
   **bit-for-bit identical** (max relative difference 0.0, tolerance is 1e-6).
5. **The fixture stays as recorded.** `meta.versions` in `baseline_predictions.json` still says
   Python 3.10.22, because that is where the baseline came from. The characterization test
   checks the model libraries against the fixture and the interpreter against
   `.python-version`.

Measured results: [`../benchmarks/phase-2-uv.md`](../benchmarks/phase-2-uv.md).

## Alternatives

| Option | Pros | Cons | Why not chosen |
| --- | --- | --- | --- |
| **pip-tools** (`pip-compile` + `pip-sync`) | Small, plain `requirements.txt` output, familiar | Separate tools for Python install, venv, lock and sync; one lock per Python version/platform; slower installs | uv does the same with one tool, one cross-platform lock and faster cold builds (−28 % measured) |
| **Poetry** | Mature, lockfile, dependency groups | Slower resolver and installs; its own `[tool.poetry]` metadata (Poetry 2 supports `[project]` but still differs); does not install Python itself | No advantage over uv for an application that is never published as a package |
| **conda / mamba** | Binary packages beyond Python (MKL, compilers) | Different channel builds of numpy/scipy than the PyPI wheels the baseline used (different BLAS can change floating-point results); large images; a second ecosystem to maintain | Risks the exact predictions we need to keep; nothing here needs non-PyPI binaries |
| **Upgrade the libraries too** (e.g. scikit-learn 1.8) | Newer features and fixes | Changes the environment `model.pkl` was pickled in; predictions could move | Not needed: every pinned version has 3.12 wheels. Out of scope while the model is not retrained |
| **Stay on Python 3.10** | Zero risk now | Past end of life, no security fixes; blocks newer library releases later | The move to 3.12 was verified to change nothing |
| **Python 3.13** | Supported a year longer (to Oct 2029) | Bigger jump from 3.10, less time on these exact library builds | Also possible: every locked package has `cp313` wheels. 3.12 is the more conservative step, supported to Oct 2028. Revisit with the next dependency upgrade |
| **Python 3.14** | Newest | numpy 2.2.6 and scipy 1.15.3 have no `cp314` wheels | Would require upgrading the model's libraries |

## Consequences

- Every install (host, API image, test image, training image) resolves to the same versions;
  a cold rebuild cannot drift. The characterization test runs on the host (`make test`).
- `uv.lock` is the source of truth. Every lock change is reviewed in its diff and followed by
  the characterization test (`.claude/CLAUDE.md`).
- Changing a model library means editing a `==` pin deliberately, and
  `test_model_libraries_match_baseline` then fails: the baseline no longer describes the
  environment. That forces an explicit decision (a new baseline is recorded only when the
  human asks for it, `.claude/CLAUDE.md`) instead of a silent drift.
- Changing Python means editing `.python-version`, `requires-python`, the three Dockerfiles and
  the ruff/mypy targets together, then verifying predictions as in Step B before merging.
- The base image is pinned by digest, so OS security patches do not arrive on their own: bump
  the digest periodically (Phase 5 CI can automate a check).
- On 3.12 the API image is 29 MB larger than on 3.10 (precompiled bytecode is bigger in 3.12);
  request performance did not regress (benchmarks linked above).
- Contributors need uv installed; `make` targets fail with a clear message otherwise.
