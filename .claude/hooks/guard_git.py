#!/usr/bin/env python3
"""PreToolUse hook for Bash: enforce the git workflow from CLAUDE.md.

Second line of defence behind the permission rules in settings.json.
Exit code 2 blocks the command and sends stderr back to the agent.

Blocks:
- `git commit` while on main/master (every phase works on its own branch);
- `git push` that targets main/master;
- commit messages (`-m`, `--message`, `-am`, `-F <file>`, `--file`, `-F -` with a heredoc)
  that do not follow Conventional Commits, and messages it cannot read.

git is found behind `VAR=value` assignments and wrappers (`env`, `command`, `exec`, `time`,
`nohup`), inside `bash`/`sh`/`zsh -c "..."` and `eval "..."`, and its branch is checked in the
directory the command runs in: earlier `cd <dir>` in the same command line and `git -C <dir>`
both count.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass

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
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Run their arguments as a command; their own options (`time -p`) are skipped.
WRAPPERS = {"command", "exec", "time", "nohup", "builtin"}
ENV_OPTIONS_WITH_VALUE = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
# Nested `bash -c` / `eval` levels followed before giving up (and blocking).
MAX_DEPTH = 5
# `git commit` short options that take a value: -m/-F give the message, -C/-c/-t do not.
MESSAGE_OPTIONS = "mF"
OTHER_VALUE_OPTIONS = "Cct"


class Unverifiable(Exception):
    """The command may commit with a message the hook cannot read."""


@dataclass(frozen=True)
class Invocation:
    """One git command: where it runs, its global options, subcommand and arguments."""

    cwd: str | None
    global_options: list[str]
    args: list[str]
    # Body of a heredoc fed to this command (for `git commit -F -`), if any.
    heredoc: str | None


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


def strip_heredocs(command: str) -> tuple[str, list[str | None]]:
    """Remove heredoc bodies, which are data rather than commands.

    Returns the remaining text and the bodies in order (None for a heredoc with no closing
    delimiter, which is left in place so its text is still checked).
    """
    lines = command.split("\n")
    kept: list[str] = []
    bodies: list[str | None] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for match in HEREDOC.finditer(line):
            delimiter = match.group("delimiter")
            for j in range(i, len(lines)):
                if lines[j].strip() == delimiter:
                    bodies.append("\n".join(lines[i:j]))
                    i = j + 1
                    break
            else:
                bodies.append(None)
    return "\n".join(kept), bodies


def split_commands(text: str) -> list[list[str]]:
    """Tokenize a shell command, splitting on separators outside quotes only."""
    text = text.replace("\\\n", " ")
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


def strip_prefixes(tokens: list[str]) -> list[str]:
    """Drop leading `VAR=value` assignments and wrappers, returning the command they run."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if ASSIGNMENT.match(token):
            i += 1
        elif token in WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
        elif token == "env":
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or ASSIGNMENT.match(tokens[i])):
                i += 2 if tokens[i] in ENV_OPTIONS_WITH_VALUE else 1
        else:
            break
    return tokens[i:]


def change_dir(cwd: str | None, args: list[str]) -> str | None:
    """The directory after `cd <args>`; unchanged if it cannot be known or does not exist."""
    targets = [arg for arg in args if not arg.startswith("-")]
    target = os.path.expanduser(os.path.expandvars(targets[0] if targets else "~"))
    if "$" in target or "-" in args:
        return cwd
    if not os.path.isabs(target):
        target = os.path.join(cwd or os.getcwd(), target)
    target = os.path.normpath(target)
    return target if os.path.isdir(target) else cwd


def dash_c_script(tokens: list[str]) -> str | None:
    """The script of `bash -c '<script>'` (also `-lc`, `-ec`, ...), or None."""
    for i, token in enumerate(tokens[1:], start=1):
        if token.startswith("-") and not token.startswith("--") and "c" in token:
            rest = [t for t in tokens[i + 1 :] if not t.startswith("-")]
            return rest[0] if rest else None
    return None


def git_invocations(command: str, cwd: str | None, depth: int = 0) -> list[Invocation]:
    """Every git command in a command line, with the directory each one runs in."""
    if depth > MAX_DEPTH:
        raise Unverifiable("too many nested `bash -c` / `eval` levels to check")
    text, bodies = strip_heredocs(command)
    heredocs_seen = 0
    result: list[Invocation] = []
    for raw_tokens in split_commands(text):
        # Heredocs are numbered in order; this command's (if any) is the next one.
        operators = len(HEREDOC.findall(" ".join(raw_tokens)))
        heredoc = bodies[heredocs_seen] if operators and heredocs_seen < len(bodies) else None
        heredocs_seen += operators

        tokens = strip_prefixes(raw_tokens)
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        if name == "cd":
            cwd = change_dir(cwd, tokens[1:])
        elif name in SHELLS:
            script = dash_c_script(tokens)
            if script is not None:
                result.extend(git_invocations(script, cwd, depth + 1))
        elif name == "eval":
            result.extend(git_invocations(" ".join(tokens[1:]), cwd, depth + 1))
        elif name == "git":
            i = 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in GLOBAL_OPTIONS_WITH_VALUE else 1
            result.append(Invocation(cwd, tokens[1:i], tokens[i:], heredoc))
    return result


def work_dir(cwd: str | None, global_options: list[str]) -> str:
    """The directory git runs in: the command's cwd moved by each `-C <path>`."""
    directory = cwd or os.getcwd()
    for option, value in zip(global_options, global_options[1:], strict=False):
        if option == "-C":
            directory = os.path.join(directory, os.path.expanduser(value))
    return directory


def first_line(message: str) -> str:
    """The subject git will record: the first non-blank line."""
    return next((line for line in message.splitlines() if line.strip()), "")


def read_message_file(path: str | None, directory: str, heredoc: str | None) -> str:
    if path is None:
        raise Unverifiable("`-F` without a file")
    if path == "-":
        if heredoc is None:
            raise Unverifiable("message read from stdin; use -m, a file or a heredoc")
        return heredoc
    try:
        with open(os.path.join(directory, os.path.expanduser(path))) as handle:
            return handle.read()
    except OSError as error:
        raise Unverifiable(f"cannot read message file {path!r}: {error.strerror}") from error


def commit_subject(args: list[str], directory: str, heredoc: str | None) -> str | None:
    """Subject of the message given on the command line, or None if none is given."""
    i = 1
    while i < len(args):
        arg = args[i]
        following = args[i + 1] if i + 1 < len(args) else None
        if arg == "--":
            break
        if arg in ("-m", "--message"):
            return None if following is None else first_line(following)
        if arg.startswith("--message="):
            return first_line(arg.split("=", 1)[1])
        if arg == "--file":
            return first_line(read_message_file(following, directory, heredoc))
        if arg.startswith("--file="):
            return first_line(read_message_file(arg.split("=", 1)[1], directory, heredoc))
        if arg.startswith("-") and not arg.startswith("--"):
            # A cluster of short options, e.g. -am "msg", -amsg, -aF file.
            for j, option in enumerate(arg[1:], start=1):
                attached = arg[j + 1 :] or None
                if option == "m":
                    value = attached if attached is not None else following
                    return None if value is None else first_line(value)
                if option == "F":
                    value = attached if attached is not None else following
                    return first_line(read_message_file(value, directory, heredoc))
                if option in OTHER_VALUE_OPTIONS:
                    if attached is None:
                        i += 1
                    break
        i += 1
    return None


def check(invocation: Invocation) -> None:
    args = invocation.args
    if not args:
        return
    sub = args[0]
    cwd, global_options = invocation.cwd, invocation.global_options

    if sub == "commit":
        branch = current_branch(cwd, global_options)
        if branch in PROTECTED:
            block(
                f"commits on '{branch}' are not allowed. "
                "Create the phase branch first (see Git workflow in CLAUDE.md)."
            )
        subject = commit_subject(args, work_dir(cwd, global_options), invocation.heredoc)
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


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    command = payload.get("tool_input", {}).get("command", "")
    try:
        for invocation in git_invocations(command, payload.get("cwd")):
            check(invocation)
    except Unverifiable as error:
        block(f"cannot check this git command: {error}.")
    sys.exit(0)


if __name__ == "__main__":
    main()
