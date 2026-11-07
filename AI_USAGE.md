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
