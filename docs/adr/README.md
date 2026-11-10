# Architecture decision records

One file per non-trivial decision: context, decision, alternatives, consequences. New ADRs
start from [`000-template.md`](000-template.md) and take the next number. An ADR is not
rewritten when a later one changes the decision; the later one supersedes it.

| ADR | Decision | Phase | Status |
| --- | --- | --- | --- |
| [001](001-uv-and-python-version.md) | Manage dependencies with uv (`uv.lock`), pin the libraries behind `model.pkl` exactly, and run on Python 3.12.16. | 2 | Accepted |
| [002](002-persisted-imputer-artifact.md) | Fit the KNN imputer once with `scripts/fit_imputer.py`, commit it as `src/model/imputer.joblib` with its metadata, verify it with `make check-imputer` and load it at startup. | 3 | Accepted |
| [003](003-imputation-design.md) | Impute missing house fields with the notebook's KNN (raw values, no scaling, no rounding), after an exact fill of one missing sqft field from `sqft_living = sqft_above + sqft_basement`. | 3 | Accepted |
| [004](004-serving-performance.md) | Load artifacts once in the lifespan, one BLAS/OpenMP thread per process, a `def` route, workers from `WEB_CONCURRENCY`, no `--reload`, and `TCP_NODELAY` on every connection. | 4 | Accepted |
| [005](005-production-readiness.md) | Strict error contract (unknown fields rejected, no input echoed), `model_version`, request IDs, a JSON request log, multiprocess Prometheus metrics, CORS off by default, non-root image, CI. | 5 | Accepted |
