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
SEPARATOR_CHARS = ";&|\n"
HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*['\"]?(?P<delimiter>[\w.-]+)['\"]?")
GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
}


def block(reason: str) -> None:
    print(f"Blocked by .claude/hooks/guard_git.py: {reason}", file=sys.stderr)
    sys.exit(2)


def current_branch(cwd: str | None, global_options: list[str]) -> str:
    """Return the branch the command would act on, honouring e.g. `git -C <path>`."""
    try:
        out = subprocess.run(
            ["git", *global_options, "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def strip_heredocs(command: str) -> str:
    """Remove heredoc bodies, which are data rather than commands.

    A heredoc with no closing delimiter is left in place, so its text is still checked.
    """
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        match = HEREDOC.search(line)
        if match is None:
            continue
        delimiter = match.group("delimiter")
        for j in range(i, len(lines)):
            if lines[j].strip() == delimiter:
                i = j + 1
                break
    return "\n".join(kept)


def split_commands(command: str) -> list[list[str]]:
    """Tokenize a shell command, splitting on separators outside quotes only."""
    text = strip_heredocs(command).replace("\\\n", " ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=SEPARATOR_CHARS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        # Unparseable, e.g. unbalanced quotes: split naively so it is still checked.
        return [seg.split() for seg in re.split(r"&&|\|\||[;|\n]", text)]

    commands: list[list[str]] = [[]]
    for token in tokens:
        if set(token) <= set(SEPARATOR_CHARS):
            commands.append([])
        else:
            commands[-1].append(token)
    return commands


def git_invocations(command: str) -> list[tuple[list[str], list[str]]]:
    """Return (global options, subcommand and its arguments) for each git command."""
    result = []
    for tokens in split_commands(command):
        if not tokens or tokens[0] != "git":
            continue
        i = 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i] in GLOBAL_OPTIONS_WITH_VALUE else 1
        result.append((tokens[1:i], tokens[i:]))
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

    for global_options, args in git_invocations(command):
        if not args:
            continue
        sub = args[0]

        if sub == "commit":
            branch = current_branch(cwd, global_options)
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
                    dest = current_branch(cwd, global_options)
                if dest in PROTECTED:
                    block(f"pushing to '{dest}' is not allowed; open a pull request instead.")
            if not refs and current_branch(cwd, global_options) in PROTECTED:
                block("pushing from main/master is not allowed; push the phase branch.")

    sys.exit(0)


if __name__ == "__main__":
    main()
