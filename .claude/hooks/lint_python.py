#!/usr/bin/env python3
"""PostToolUse hook for Write/Edit: format and lint every Python file the agent touches.

- Runs `ruff format` and `ruff check --fix` on the edited file.
- If lint errors remain, exits with code 2 so the agent sees them and fixes them now,
  instead of discovering them later at `make lint`.
- Uses `uv run ruff` once the project is on uv, falls back to a global `ruff`,
  and does nothing if neither is available (e.g. before Phase 0 is done).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


def ruff_cmd(project_dir: str) -> list[str] | None:
    if shutil.which("uv") and os.path.exists(os.path.join(project_dir, "uv.lock")):
        return ["uv", "run", "--frozen", "ruff"]
    if shutil.which("ruff"):
        return ["ruff"]
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    path = payload.get("tool_input", {}).get("file_path", "")
    if not path.endswith(".py") or not os.path.exists(path):
        sys.exit(0)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or "."
    ruff = ruff_cmd(project_dir)
    if ruff is None:
        sys.exit(0)

    subprocess.run(
        [*ruff, "format", path],
        cwd=project_dir,
        capture_output=True,
        timeout=50,
        check=False,
    )
    check = subprocess.run(
        [*ruff, "check", "--fix", path],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )
    if check.returncode != 0:
        print(
            f"ruff found issues in {path}:\n{check.stdout}{check.stderr}",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
