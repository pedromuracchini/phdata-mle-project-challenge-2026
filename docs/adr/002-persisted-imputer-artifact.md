# ADR 002: Persist the fitted imputer as a committed artifact, loaded at startup

- **Status:** Accepted
- **Date:** 2026-11-08
- **Phase:** 3 (branch `feat/phase-3-imputation`)

## Context

- Phase 3 makes every field except `zipcode` nullable and fills the gaps with the KNN imputer from
  `notebooks/imputation_experiment.ipynb` (`KNNImputer(n_neighbors=5, weights="distance")`).
  The notebook fits it in memory and never saves it.
- `.claude/CLAUDE.md` hard rules: never fit the imputer inside a request; load artifacts once in
  the `lifespan` and read them from `app.state`.
- A `KNNImputer` "fit" just stores its donor rows. Which rows those are decides every imputed
  value, so the fit data has to be fixed and recorded. We use the model's own train split
  (16,209 rows), rebuilt exactly as `create_model.py` does it and checked against the model's
  stored training data.
- `model.pkl` is already a committed artifact, tied to the pinned scikit-learn / numpy / pandas
  versions ([ADR 001](001-uv-and-python-version.md)).
- With the same environment, `joblib.dump` of the fitted imputer is byte-for-byte reproducible.

## Decision

1. **`scripts/fit_imputer.py` (`make fit-imputer`) fits the imputer once** in the locked
   environment and writes:
   - `src/model/imputer.joblib` (4.8 MB): the fitted `KNNImputer`.
   - `src/model/imputer_metadata.json`: the artifact's sha256, the estimator and its params,
     `scaling: null`, the 33 columns in model order, the fit data (source files with sha256, the
     selection rule, the row count), the library versions, the generator and `fitted_at`.
2. **Both files are committed**, like `model.pkl`, and baked into the image.
3. **`make check-imputer` (`fit_imputer.py --check`) refits in memory and fails on any
   difference** from the committed artifact: the file hash, the fitted state, the bytes of the
   refit's dump, and the metadata except `fitted_at`. `docker-build`, `test-integration` and
   `test-all` depend on it, and a unit test does the same refit-and-compare.
4. **The API loads the imputer in the `lifespan`** (`core/artifacts.py`, `load_imputer`) into
   `app.state.imputer`. Startup fails with `ArtifactError` if a file is missing, the sha256
   doesn't match the metadata, the installed scikit-learn / joblib / numpy differ from the
   recorded versions, or the columns differ (name or order) from the model's features.
5. **Paths come from `core/config.py`** (pydantic-settings, `APP_` prefix) and can be overridden
   by environment variables.

## Alternatives

| Option | Pros | Cons | Why not chosen |
| --- | --- | --- | --- |
| **Fit at startup** from the CSVs | No binary artifact; nothing to keep in sync | Ships the sales CSV in the image; every start re-reads and re-splits data, so a changed CSV silently changes imputations; nothing to review or hash | The fitted state wouldn't be versioned or reviewable |
| **Fit on the first request** (lazy) | Same as above, plus a fast start | Breaks the hard rule; the first request pays the cost; concurrency issues | Ruled out by `.claude/CLAUDE.md` |
| **Plain `pickle`** instead of joblib | No extra direct dependency | Less efficient for large numpy arrays; same security model | joblib is scikit-learn's documented way to persist estimators, and it was already installed as a scikit-learn dependency (now declared directly, pinned) |
| **skops / ONNX** | skops loads without arbitrary code execution; ONNX is portable | A new dependency or converter, not evaluated against `KNNImputer`'s exact behaviour; `model.pkl` is a pickle anyway | No gain while the model itself is a pickle. Revisit together with the model |
| **Git LFS or an artifact store** | Keeps binaries out of git history | More tooling for a 4.8 MB file | Not needed at this size; reconsider if artifacts grow or are refit often |

## Consequences

- The imputations served are exactly the ones reviewed: the artifact is hashed, reproducible and
  checked before every image build.
- A dependency change that alters the fitted state or the dump fails `check-imputer` and startup
  fails if the installed versions differ. Refitting is then a deliberate `make fit-imputer` plus a
  reviewed diff of the metadata.
- **The sha256 in the metadata is a consistency check, not tamper protection:** it lives next to
  the artifact. `joblib.load` runs pickle code, so the artifact must come only from this
  repository, like `model.pkl`.
- Startup now reads 4.8 MB and validates it; a broken artifact stops the service from starting
  instead of serving wrong prices.
- Phase 4 moves the model and the demographics into the same `lifespan` loader; Phase 5 can add
  `model_version` / the imputer hash to the response and logs.
  *Update (Phase 6):* both done. Phase 4 loads every artifact in `main.load_artifacts`
  ([ADR 004](004-serving-performance.md)); Phase 5 returns `model_version` (which covers the
  imputer's hash through its metadata) in `/predict`, `/ready` and every request log line
  ([ADR 005](005-production-readiness.md)).
