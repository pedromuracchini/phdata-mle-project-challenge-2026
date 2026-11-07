#!/usr/bin/env python3
"""PreToolUse hook for Bash: enforce the git workflow from CLAUDE.md.

Second line of defence behind the permission rules in settings.json.
Exit code 2 blocks the command and sends stderr back to the agent.

Blocks:
- `git commit` while on main/master (every phase works on its own branch);
- `git push` that targets main/master;
- `git commit -m` messages that do not follow Conventional Commits.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys

PROTECTED = {"main", "master"}
CONVENTIONAL = re.compile(
    r"^(feat|fix|perf|test|build|docs|chore|refactor|ci|style|revert)(\([\w\-./]+\))?!?: \S.{0,70}$"
)


def block(reason: str) -> None:
    print(f"Blocked by .claude/hooks/guard_git.py: {reason}", file=sys.stderr)
    sys.exit(2)


def current_branch(cwd: str | None) -> str:
    try:
        out = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def git_segments(command: str) -> list[list[str]]:
    """Split a shell command on && ; || | and return the git invocations."""
    segments = re.split(r"&&|\|\||;|\|", command)
    result = []
    for seg in segments:
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if tokens and tokens[0] == "git":
            result.append(tokens[1:])
    return result


def commit_subject(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg in ("-m", "--message") and i + 1 < len(args):
            return args[i + 1].splitlines()[0]
        if arg.startswith("--message="):
            return arg.split("=", 1)[1].splitlines()[0]
        if arg.startswith("-m") and len(arg) > 2:
            return arg[2:].splitlines()[0]
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    command = payload.get("tool_input", {}).get("command", "")
    cwd = payload.get("cwd")

    for args in git_segments(command):
        if not args:
            continue
        sub = args[0]

        if sub == "commit":
            branch = current_branch(cwd)
            if branch in PROTECTED:
                block(
                    f"commits on '{branch}' are not allowed. "
                    "Create the phase branch first (see Git workflow in CLAUDE.md)."
                )
            subject = commit_subject(args)
            if subject is not None and not CONVENTIONAL.match(subject):
                block(
                    f"commit subject '{subject}' is not a Conventional Commit. "
                    "Use e.g. 'feat(imputation): add KNN imputer artifact' (max 72 chars)."
                )

        if sub == "push":
            targets = [a for a in args[1:] if not a.startswith("-")]
            refs = targets[1:] if len(targets) > 1 else []
            for ref in refs:
                dest = ref.split(":")[-1].removeprefix("refs/heads/")
                if dest == "HEAD":
                    dest = current_branch(cwd)
                if dest in PROTECTED:
                    block(
                        f"pushing to '{dest}' is not allowed; open a pull request instead."
                    )
            if not refs and current_branch(cwd) in PROTECTED:
                block("pushing from main/master is not allowed; push the phase branch.")

    sys.exit(0)


if __name__ == "__main__":
    main()
