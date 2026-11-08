# CLAUDE.md

Guidance for AI coding agents working in this repository. Keep this file short and current;
update it whenever a convention or command changes.

## Project context

Sound Realty serves house-price predictions for the Seattle area through a FastAPI REST API
(`POST /predict`), packaged with Docker. This engagement adds three things to the existing service:

1. **Missing-data handling** — accept `null` for any field except `zipcode`, impute with KNN
   (configuration taken from `notebooks/imputation_experiment.ipynb`).
2. **Performance** — remove per-request work and prove the gain with benchmarks.
3. **Production readiness** — validation, error handling, logging, metrics, tests, docs.

The trained model (`src/model/model.pkl`) is **not** to be retrained.

## Commands

Always use the Makefile targets; do not invent ad-hoc commands.

| Task | Command |
| --- | --- |
| Install deps | `make setup` |
| Run tests | `make test` |
| Lint + format check | `make lint` |
| Type check | `make typecheck` |
| Benchmark | `make bench` |
| Fit imputer artifact | `make fit-imputer` |
| Verify imputer artifact | `make check-imputer` |
| Evaluate imputation | `make evaluate-imputation` |
| Build image | `make docker-build` (runs `check-imputer` first) |
| Run API in Docker | `make docker-run` (`WORKERS=N` for N worker processes) |
| Run API with auto-reload (dev) | `make docker-dev` |

Run `make lint typecheck test` before declaring any task done. A task is not done while any of
them fails.

## Hard rules (never violate)

- **Never retrain or modify `model.pkl`.**
- **Never fit the imputer (or any scaler) inside a request.** The imputer (and a scaler, if one
  is adopted) is fitted once by `scripts/fit_imputer.py` on the model's train split, saved as an
  artifact, and loaded at startup.
- **Load all artifacts and data once**, in the FastAPI `lifespan`, and read them from `app.state`.
  No disk I/O on the request path.
- **Feature order is fixed** and must match what the model was trained on. Never reorder or
  rename features; read the order from the model's feature metadata.
- **`zipcode` is a 5-digit string**, never an int (leading zeros matter for the lookup).
- **`zipcode` is required.** Missing or unknown zipcode returns 422 with a clear message.
- **No CPU-bound work inside `async def` routes.** Use `def` routes (threadpool) for inference.
- **Do not change behaviour for complete requests.** `tests/test_characterization.py` compares
  predictions against `tests/fixtures/baseline_predictions.json`; it must keep passing.
  Never regenerate that fixture unless explicitly asked.
- Do not add dependencies without saying why.

## Dependencies (uv)

- Dependencies are managed with uv: `pyproject.toml` + `uv.lock`, both committed.
- Add runtime deps with `uv add <pkg>`, dev tools with `uv add --dev <pkg>`. Never `pip install`.
- Run tools through `uv run` (the Makefile already does); CI and Docker use `uv sync --frozen`.
- **Never upgrade scikit-learn, numpy or pandas** without explicit approval: `model.pkl` was
  pickled with the pinned versions, and an upgrade can break loading or silently change predictions.
- After any change to `uv.lock`, run the characterization test.

## Imputation pipeline (order matters)

Phase 3 decision: **faithful to the notebook first**. The KNN configuration comes from
`notebooks/imputation_experiment.ipynb` (`KNNImputer(n_neighbors=5, weights="distance")`, raw
values, no scaling), fitted on the 33 model features (house fields + zipcode demographics) of
the model's train split. Deviations are adopted only on evidence from
`scripts/evaluate_imputation.py`, with the human's approval and an ADR
(`docs/adr/003-imputation-design.md`, `docs/imputation_report.md`).

1. Validate payload (Pydantic v2 schema). Omitted and `null` fields are both "missing";
   NaN/Infinity are invalid (422).
2. Look up zipcode demographics; unknown zipcode → 422.
3. If nothing is missing, skip imputation entirely (complete requests keep the v0 code path).
4. Deterministic fill (**adopted**, ADR 003): if exactly one of `sqft_living`, `sqft_above`,
   `sqft_basement` is missing, compute it from `sqft_living = sqft_above + sqft_basement`.
   A negative result means contradictory input: leave the field to KNN.
5. KNN imputation of the remaining missing fields, using the persisted imputer.
6. Predict; return `predicted_price` and `imputed_fields` (every field that was missing, however
   it was filled; empty list when nothing was).

**Rejected** after evaluation (ADR 003): scaling inside the imputer (worse when only `zipcode`
is given) and rounding/clipping (no measurable effect; if ever revisited, `floors` rounds to
0.5, not to an integer). `model_version` in the response is deferred to Phase 5.

Since Phase 4 the `lifespan` (`main.load_artifacts`) loads the model, the feature order, the
demographics (a zipcode -> model-order row lookup, `core.artifacts.load_demographics`) and the
imputer; `services/prediction.py` builds the request's frame from the lookup.

If the notebook and this file disagree, stop and flag it — do not silently pick one.

## Code layout

```
src/
  api/        # routes, request/response schemas, error handlers
  services/   # prediction and imputation logic (pure, unit-testable)
  core/       # config (pydantic-settings), logging, artifact loading
  model/      # model.pkl, imputer.joblib, metadata (artifacts only, no code)
scripts/      # fit_imputer.py, evaluate_imputation.py, benchmark.py
tests/        # unit, API, characterization, property-based (hypothesis)
docs/         # adr/, benchmarks/, imputation_report.md, exploration.md
```

Keep business logic in `services/`; routes stay thin.

## Conventions

- Python 3.12 (exactly 3.12.16, pinned in `.python-version` and the Docker base images; change
  them together, and only after the characterization test passes on the new version), full
  type hints, ruff formatting, no `print` — use the structured logger.
- Logs are JSON and include `request_id`, latency and `imputed_fields`.
- Errors: 422 for invalid input, 500 without stack traces in the response body.
- Config via environment variables only; no hard-coded paths outside `core/config.py`.
- Every new behaviour gets a test. Bug fix = failing test first, then the fix.

## Git workflow

**Never commit, push, open a PR or merge without asking the human first and getting an explicit
"yes" in the conversation.** Approval covers one commit only; ask again for the next one.

Branches — one per phase, created from an up-to-date `main` (you may create the branch without asking):

| Phase | Branch |
| --- | --- |
| 0 | `chore/phase-0-harness` |
| 1 | `test/phase-1-baseline` |
| 2 | `build/phase-2-uv` |
| 3 | `feat/phase-3-imputation` |
| 4 | `perf/phase-4-performance` |
| 5 | `chore/phase-5-production` |
| 6 | `docs/phase-6-documentation` |

Before asking to commit, always:

1. Run `make lint typecheck test` and report the results. Do not ask to commit while anything fails.
2. Show `git status` and `git diff --stat`, and summarize the change in 2–4 lines.
3. Propose a Conventional Commits message (`feat:`, `fix:`, `perf:`, `test:`, `build:`, `docs:`,
   `chore:`, `refactor:`), imperative mood, subject under 72 characters, body explaining *why*.
4. Stop and wait for approval.

After approval:

- Stage only the files that were reviewed, by path. Never `git add -A` or `git add .`.
- Commit with the approved message (including any edits the human made to it).

One logical change per commit — usually one checklist item from the plan. If a change grows
beyond that, stop and propose splitting it.

Never:

- commit directly to `main`;
- force-push, amend, or rebase commits that were already pushed;
- run `git reset --hard`, `git clean -fd`, `git checkout -- .` or anything else that discards
  work, without asking;
- commit secrets, `.env` files, `.venv/`, profiling dumps or large data files;
- use `--no-verify` to skip pre-commit hooks.

At the end of a phase, ask before pushing and opening the PR. The PR description summarizes the
phase and links its artifacts (ADRs, benchmark results, reports).

These rules are also enforced by `.claude/settings.json` (permissions) and `.claude/hooks/`
(`guard_git.py` blocks commits on `main`, pushes to `main` and non-conventional commit messages;
`lint_python.py` runs ruff on every edited Python file). If a hook or permission rule blocks a
command, fix the cause or ask the human — never look for a way around it.

## Performance work

- Measure before and after with `make bench` using the same load profile; record results in
  `docs/benchmarks/`. Do not claim a speed-up without numbers.
- Profile (py-spy or cProfile) before optimizing; optimize what the profile shows.
- One optimization per commit, with the characterization test passing at each step.
- One BLAS/OpenMP thread per process (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
  `MKL_NUM_THREADS` = 1 in `Dockerfile`, `Dockerfile.test` and the Makefile's `THREAD_ENV`;
  `tests/unit/test_thread_limits.py` keeps them in sync). Scale with workers, not threads.
  The OpenMP count is part of the results: scikit-learn's KNN breaks exact distance ties by
  thread chunking, so changing it changes predictions (baseline row 99). Ask before changing it.
- Worker processes come from `WEB_CONCURRENCY` (uvicorn's own variable; image default in the
  `Dockerfile`). Keep workers x threads <= the cores the container gets. Never put `--reload` or
  `--workers` in the production `CMD` (`tests/unit/test_server_command.py`).
- Serve with `--http core.server:NoDelayH11Protocol` (production `CMD` and `make docker-dev`):
  with workers or reload, uvicorn leaves Nagle on and every response waits ~44 ms for an ACK.

## Decisions and AI log

- Non-trivial design decisions go in `docs/adr/NNN-title.md` (context, decision, alternatives,
  consequences).
- When the agent proposes something that turns out wrong, or the human rejects a suggestion,
  add an entry to `AI_USAGE.md` (what was asked, what was produced, how it was caught, outcome).

## When unsure

Ask instead of guessing on: changing the API contract, adding a dependency, touching model
artifacts, or anything that would alter predictions for complete requests.
