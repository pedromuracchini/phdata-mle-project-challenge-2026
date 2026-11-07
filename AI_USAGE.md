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
