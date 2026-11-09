# AI usage log

Record of cases where an AI agent proposed or did something that turned out wrong, or where
a human rejected a suggestion (see "Decisions and AI log" in `.claude/CLAUDE.md`).
Newest entries first.

## Entry format

```markdown
### YYYY-MM-DD: short title

- **Phase / task:** phase and task the work belonged to
- **Asked:** what the human asked for
- **Produced:** what the agent proposed or did
- **Caught by:** how the problem surfaced (test, hook, review, human)
- **Outcome:** what was done about it, and any rule or habit that changed
```

## Entries

### 2026-11-08: chose py-spy to diagnose the native thread pools

- **Phase / task:** Phase 4, step 3 (profile main's KNN requests).
- **Asked:** profile before optimizing; py-spy was allowed as a throwaway sidecar.
- **Produced:** the agent used py-spy to look into the OpenBLAS/OpenMP contention. py-spy
  samples Python stacks, so it cannot see inside the native pools. Its stop-the-world sampling
  also distorts threads that spin-wait. The sidecar first needed two fixes (a missing `pgrep`,
  then a grep matching its own shell).
- **Caught by:** the agent, when the py-spy output did not account for the measured CPU time.
- **Outcome:** the py-spy result was not used. The diagnosis used per-thread `top -H` and
  cProfile runs with each pool limited in turn (committed in step 3). Lesson: pick the profiler
  for the layer in question; for native thread pools, use per-thread OS views or toggle the
  pools.

### 2026-11-08: ran two long equivalence checks in parallel, which saved no time

- **Phase / task:** Phase 4, building the bit-exact equivalence gate.
- **Asked:** how long the equivalence checks would take.
- **Produced:** the agent ran two ~32-minute checks (main's reference and a candidate) at the
  same time to save wall-clock time. Each already used every core through the unlimited thread
  pools, so together they took as long as running them one after the other (load average 37).
- **Caught by:** the agent, from the timings; reported to the human as such.
- **Outcome:** later long runs went one after the other, and nothing else ran during
  benchmarks. Lesson: two CPU-bound jobs that each fill the machine gain nothing from running
  in parallel.

### 2026-11-08: profiles meant to run with `OMP_NUM_THREADS=1` ran without the limit (zsh)

- **Phase / task:** Phase 4, step 3 (profile main with each thread pool limited).
- **Asked:** profile main's KNN request with the thread pools limited, to find the contention.
- **Produced:** the agent passed the limits through an unquoted shell variable
  (`docker run $ENV_ARGS ...`). zsh does not word-split unquoted variables, so the variable
  went through as one malformed argument and the first `-omp1` profiles ran with no limit at
  all. Nothing failed.
- **Caught by:** the agent, because those profiles' timings matched the unlimited run.
- **Outcome:** the profiles were discarded and rerun with explicit `-e` arguments.
  `scripts/profile_predict.py` now prints `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and
  `MKL_NUM_THREADS` in every report header, so a run shows the limits it actually had.
  Lesson: have experiments record the configuration they ran with, instead of trusting the
  command line.

### 2026-11-08: proposed re-implementing the KNN imputer's transform (rejected)

- **Phase / task:** Phase 4, step 9 (KNN path).
- **Asked:** a faster KNN path that reproduces the persisted `KNNImputer` exactly, with the
  approach brought to the human before implementing it.
- **Produced:** a proposal for an `ExactKNNImputer`. It would replay scikit-learn 1.7.2's
  numpy operations in the same order, precompute the donor-side arrays at startup, skip two
  operations that are exact identities when no donor has a NaN, and run one `argpartition`
  for all columns. That is about 80 lines coupled to scikit-learn internals, for an estimated
  (unmeasured) ~2× on KNN CPU per request.
- **Caught by:** the human, in review of the approach: no need for a new KNN, use
  scikit-learn's.
- **Outcome:** step 9 was dropped; the request path keeps `KNNImputer.transform`. The leftover
  overhead the profile showed outside scikit-learn (the pandas sqft fill) became step 11.
  Lesson: weigh an exact-reproduction rewrite against what it ties the code to, and offer the
  "keep the library" option alongside it, not only the rewrite.

### 2026-11-08: attributed a benchmark regression to the threadpool instead of the Nagle floor

- **Phase / task:** Phase 4, step 7 (`def` route).
- **Asked:** measure the `def` route on its own and explain the result.
- **Produced:** complete requests dropped to 0.78× as a `def` route. The agent attributed
  this to thread handoff and GIL contention in one process, in the benchmark doc and in the
  commit message. That was plausible, and it was never isolated.
- **Caught by:** step 8a found a ~48 ms Nagle/delayed-ACK floor in every child-process server.
  Both step 7 arms ran `--reload`, so both had it. Re-measured without it (step 8b), `def` is
  faster than `async` with 1 worker on every profile.
- **Outcome:** the step 7 section keeps its numbers and has a correction note pointing to
  step 8b. The commit message cannot be changed (no amending), so this entry records it.
  Lesson: when a result is explained by a mechanism, test the mechanism, or present it as a
  hypothesis. A flat latency floor that doesn't move with CPU is a sign the measurement itself
  is bounded by something else.

### 2026-11-08: assumed limiting the thread pools could not change predictions

- **Phase / task:** Phase 4, step 6 (one BLAS/OpenMP thread per process).
- **Asked:** remove the thread-pool contention found in step 3, changing no prediction.
- **Produced:** the agent planned the thread limits as a pure performance change and expected
  the gates to pass unchanged. It also first read main's default as 16 OpenMP threads (the
  logical CPU count). With `OMP_NUM_THREADS` unset, scikit-learn 1.7.2 uses the *physical* core
  count, which is 8 here.
- **Caught by:** the gates. `test_complete_payload_matches_baseline[row-99]` failed
  (573,300 vs 564,800) and `make check-equivalence` reported 93 of 128 patterns different. Value
  dumps under five thread configurations showed:
  - imputed values are bit-identical in every configuration;
  - the OpenBLAS count changes nothing;
  - only the OpenMP count of the model's KNN search changes results: 93 of 12,800 calls, all
    exact ties at the 5th neighbour (checked one by one);
  - `OMP_NUM_THREADS=8` reproduces main's reference bit-for-bit.
- **Outcome:** the human chose one thread for both pools and approved amending baseline row 99
  (recorded in the fixture's `amendments`) and regenerating the imputation reference from main
  with one thread. The old value depended on the recording host's core count. Lessons: thread
  counts can be part of a numeric result, so run the gates on any "pure performance"
  configuration change before measuring it; and check a library's default rather than assume
  it.

### 2026-11-08: route rewrite silently dropped v0's per-request `print`

- **Phase / task:** Phase 3, making every field except `zipcode` nullable (commit 6, the
  `/predict` rewrite). Phase 3 must not change complete requests; performance is Phase 4's job.
- **Asked:** rewrite the request schema and route for optional fields, keeping the v0 code path
  for complete requests and leaving the per-request work for Phase 4 to measure.
- **Produced:** the rewritten route no longer had v0's `print(input_data)`. The agent removed it
  without saying so, probably following CLAUDE.md's "no `print`" convention. The commit message
  and the review summary didn't mention it. Predictions were unchanged, so the
  characterization test and every contract test passed.
- **Caught by:** the Phase 3 benchmark. Complete requests ran at 109.8 req/s against 39.0 for the
  Phase 2 control, about 2.7× faster in a phase that shouldn't change performance. The agent
  flagged the speed-up as suspicious instead of reporting it as a gain, and traced it to the
  missing `print`, which Phase 1 had profiled at 71 % of handler time.
- **Outcome:** the human chose to restore it. A failing test came first
  (`test_route_still_prints_the_input_frame_as_v0_did`), then the fix (`0d3129d`). The benchmarks
  were rerun: complete requests 39.1 vs 39.0 req/s
  ([`docs/benchmarks/phase-3-imputation.md`](docs/benchmarks/phase-3-imputation.md)). Phase 4
  removes the `print` as its own measured optimization. Lessons: when rewriting legacy code,
  diff it line by line against the original and list every removed statement in the review,
  even when a convention seems to justify removing it. The characterization test checks
  outputs, not side effects or cost. An unexpected speed-up is a bug signal until explained.

### 2026-11-08: evaluation report draft overstated some results

- **Phase / task:** Phase 3, `docs/imputation_report.md`.
- **Asked:** write up the evaluation of the faithful imputer and the candidates (scaling, sqft
  fill, rounding).
- **Produced:** the draft summarized several numbers more favourably or more loosely than the
  generated tables supported:
  - scaling was said to improve the p90 shift on every feature (`sqft_basement` got 0.1 pp worse);
  - the size of the NRMSE gain was off;
  - the `sqft_above` MAPE cost was off;
  - the cost of zipcode-only was given without the per-method range (13–17 pp);
  - the scaling gain was given as "≤ 0.3 pp" without saying it was on `sqft_lot` only.
- **Caught by:** a re-check of every claim in the draft against
  `docs/imputation/evaluation_results.json`, before the report was proposed for commit.
- **Outcome:** each sentence was corrected to the measured number before the commit, and the
  decision (sqft fill only) was taken on the corrected text. Lesson: write prose summaries from
  the generated tables, then check each sentence against them. "Better on all" claims are the
  ones most likely to be wrong.

### 2026-11-08: `make check-imputer` failed on a valid artifact (two bugs)

- **Phase / task:** Phase 3, `scripts/fit_imputer.py --check` (ADR 002).
- **Asked:** a check that refits the imputer and fails if the committed artifact differs.
- **Produced:** two bugs made the check fail on a correct artifact:
  1. the fitted-state comparison used `==` on the estimator params, and
     `missing_values=nan` never equals itself;
  2. the metadata recorded the artifact's path instead of its file name, so a verified copy in
     another directory (the unit tests' `tmp_path`) failed the check.
- **Caught by:** running the check and the unit tests; both failed on artifacts that were
  known to be good.
- **Outcome:** a NaN-aware comparison, plus a byte comparison of a refit's dump. The metadata
  now records the file name only, and the artifact was refit. Both were fixed before the
  artifact commit. Lesson: test a verification tool on a known-good input as well as on broken
  ones, and keep machine-specific paths out of recorded metadata.

### 2026-11-08: wrong v0 fallback in the profiling script

- **Phase / task:** Phase 3, keeping `scripts/profile_predict.py` working after `predict` gained
  a `request` argument.
- **Asked:** keep the Phase 1 profiling script usable.
- **Produced:** besides the fix (`startup_request()`, a request whose app has the imputer
  loaded), the agent added a fallback for calling the v0 signature. It was wrong, and it was not
  needed: the script only ever profiles the current route.
- **Caught by:** review of the change in the session, before it was proposed for commit.
- **Outcome:** the fallback was removed before the commit. Lesson: don't add compatibility paths
  for code that no longer exists.

### 2026-11-07: proposed editing the baseline fixture's metadata to pass the version check

- **Phase / task:** Phase 2, Step B (Python 3.10 → 3.12).
- **Asked:** move to Python 3.12 with the same library versions; predictions must match. The
  human had said the version check would fail and to ask before updating the fixture's
  version metadata.
- **Produced:** after confirming all 100 predictions were bit-for-bit identical on 3.12, the
  agent asked to change `meta.versions.python` in `tests/fixtures/baseline_predictions.json`
  from 3.10.22 to 3.12.16, then started the edit after a "yes". That would have made the fixture
  claim it was recorded on 3.12, which is false. The test's own failure message suggested it
  ("or pin the versions in meta.versions").
- **Caught by:** the human, who rejected the edit: the baseline was recorded on 3.10, so that
  is what the fixture must say. Nothing was written.
- **Outcome:** the fixture is untouched. The test was split instead: the model libraries
  (scikit-learn, numpy, pandas) are checked against the recorded versions, Python against
  `.python-version`, and the message no longer suggests editing the fixture (commit
  `33f5e85`, ADR 001). Lesson: recorded data describes where it came from; when a check
  mixes "how it was recorded" with "what we expect now", change the check, not the record.

### 2026-11-07: ad-hoc verification scripts reported false results

- **Phase / task:** Phase 2, Step A (diff against the baseline freeze) and Step B (3.12 wheel
  check).
- **Asked:** explain every difference from `baseline-freeze.txt`; check that wheels exist for
  Python 3.12 for every pinned package before changing anything.
- **Produced:** two one-off checks were wrong. (1) To see whether scikit-learn needs
  `packaging`, the agent imported `packaging` itself before checking `sys.modules`, so the
  check always said "yes". (2) The wheel-check script only accepted `py3-none-any` as an
  interpreter-independent wheel and reported ruff 0.17.4 as missing, although ruff ships
  `py3-none-manylinux` binary wheels. Taken at face value, (2) would have stopped the phase
  ("if any is missing, stop and tell me").
- **Caught by:** the agent, before acting on either: (1) the uv environment, which reported
  `packaging` not imported, contradicted it; (2) ruff, a pure binary, was the only miss, which
  was suspicious, so the agent listed its actual wheel files on PyPI.
- **Outcome:** both were corrected in the report to the human. Neither reached a commit.
  Lesson: when a quick script's result would trigger a decision (stop, approve an exception),
  cross-check it against the raw source (PyPI file list, `pip show`, a clean interpreter)
  before reporting it.

### 2026-11-07: pre-commit reformatted files outside the task's scope

- **Phase / task:** Phase 0, task 6 (pre-commit with ruff and mypy).
- **Asked:** add pre-commit, ruff and mypy config, and verify it. `src/` was not to be touched
  in Phase 0.
- **Produced:** the agent ran `pre-commit run --all-files` to verify the config without
  allowing for the `ruff-format` hook rewriting files in place. It reformatted 8 files,
  including 4 in `src/`, 1 in `test/` and the root `create_model.py`. Whitespace and line
  wrapping only, no behaviour change.
- **Caught by:** the agent, in `git status` right after the run.
- **Outcome:** the agent told the human and restored the 6 out-of-scope files with
  `git restore`, which the human approved through the permission prompt. They matched
  `HEAD` again. The 2 reformatted hook files were in scope and kept. Lesson: verify
  formatter hooks in check-only mode (`ruff format --check`) first, and run pre-commit on
  all files only once the formatter has nothing left to change.
