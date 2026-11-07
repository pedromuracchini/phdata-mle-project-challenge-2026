"""Tests for guard_git.py. Run with: python3 -m unittest discover -s .claude/hooks

Each case feeds a sample command to the hook as JSON, the way Claude Code does.
Nothing here runs the sample commands: they are only strings passed to the hook.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).with_name("guard_git.py")
GOOD = 'git commit -m "chore: add x"'


def init_repo(path: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)


class GuardGitTest(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory[str]
    main_repo: str
    feature_repo: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.main_repo = str(root / "on_main")
        cls.feature_repo = str(root / "on_feature")
        init_repo(Path(cls.main_repo), "main")
        init_repo(Path(cls.feature_repo), "feature")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def returncode(self, command: str, cwd: str) -> int:
        payload = json.dumps({"tool_input": {"command": command}, "cwd": cwd})
        result = subprocess.run(
            ["python3", str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode

    def assert_blocked(self, command: str, cwd: str) -> None:
        self.assertEqual(self.returncode(command, cwd), 2, command)

    def assert_allowed(self, command: str, cwd: str) -> None:
        self.assertEqual(self.returncode(command, cwd), 0, command)

    # Existing protections that must keep working.

    def test_blocks_commit_on_main(self) -> None:
        self.assert_blocked(GOOD, self.main_repo)

    def test_blocks_push_from_main_without_ref(self) -> None:
        self.assert_blocked("git push", self.main_repo)

    def test_blocks_pushes_targeting_main(self) -> None:
        for ref in ("main", "HEAD:main", "feature:refs/heads/main"):
            self.assert_blocked(f"git push origin {ref}", self.feature_repo)

    def test_blocks_bad_commit_subjects(self) -> None:
        for subject in ("added stuff", "chore: " + "x" * 80):
            self.assert_blocked(f'git commit -m "{subject}"', self.feature_repo)

    def test_blocks_bad_message_equals_form(self) -> None:
        self.assert_blocked('git commit --message="nope"', self.feature_repo)

    def test_allows_normal_commands(self) -> None:
        for command in (
            GOOD,
            f'{GOOD} -m "Why it matters."',
            "git push -u origin chore/phase-0-harness",
            "ls -la",
        ):
            self.assert_allowed(command, self.feature_repo)

    def test_allows_tag_push_from_main(self) -> None:
        self.assert_allowed("git push origin v0-original", self.main_repo)

    # Real separators must still split commands.

    def test_finds_commit_after_each_separator(self) -> None:
        for separator in (" && ", " || ", " ; ", " | ", "\n"):
            command = f"git status{separator}{GOOD}"
            self.assert_blocked(command, self.main_repo)

    def test_line_continuation_does_not_split_command(self) -> None:
        self.assert_blocked('git commit \\\n  -m "added stuff"', self.feature_repo)

    # Bug: global options hid the subcommand.

    def test_blocks_commit_with_dash_c_path_on_main(self) -> None:
        self.assert_blocked(f"git -C {self.main_repo} {GOOD[4:]}", self.main_repo)

    def test_dash_c_path_decides_which_branch_is_checked(self) -> None:
        self.assert_blocked(f"git -C {self.main_repo} {GOOD[4:]}", self.feature_repo)
        self.assert_allowed(f"git -C {self.feature_repo} {GOOD[4:]}", self.main_repo)

    def test_blocks_commit_with_config_option_on_main(self) -> None:
        self.assert_blocked(f"git -c user.name=x {GOOD[4:]}", self.main_repo)

    def test_blocks_commit_with_git_dir_option_on_main(self) -> None:
        git_dir = f"{self.main_repo}/.git"
        self.assert_blocked(f"git --git-dir={git_dir} {GOOD[4:]}", self.feature_repo)
        self.assert_blocked(f"git --git-dir {git_dir} {GOOD[4:]}", self.feature_repo)

    def test_blocks_push_with_dash_c_path_from_main(self) -> None:
        self.assert_blocked(f"git -C {self.main_repo} push", self.feature_repo)

    def test_checks_message_behind_global_option(self) -> None:
        command = f'git -C {self.feature_repo} commit -m "added stuff"'
        self.assert_blocked(command, self.feature_repo)

    # Bug: separators inside quotes split the command.

    def test_allows_separators_inside_commit_body(self) -> None:
        for body in ("a; b.", "a | b.", "a && b.", "a || b."):
            self.assert_allowed(f'{GOOD} -m "Why: {body}"', self.feature_repo)

    def test_ignores_git_text_inside_quoted_argument(self) -> None:
        command = "echo 'git status && git commit -m \"bad\"'"
        self.assert_allowed(command, self.feature_repo)

    # Bug: heredoc bodies were read as commands.

    def test_ignores_git_text_inside_heredoc(self) -> None:
        command = (
            "cat > cases.py <<'EOF'\n"
            "case = 'git status && git commit -m \"bad\"'\n"
            "EOF\n"
            "ls"
        )
        self.assert_allowed(command, self.feature_repo)

    def test_still_checks_commands_after_heredoc(self) -> None:
        command = f"cat > notes.txt <<'EOF'\nhello\nEOF\n{GOOD}"
        self.assert_blocked(command, self.main_repo)

    def test_unterminated_heredoc_is_not_skipped(self) -> None:
        command = f"cat <<EOF\n{GOOD}"
        self.assert_blocked(command, self.main_repo)

    # Commands that cannot be parsed must not get through unchecked.

    def test_unbalanced_quotes_still_checked(self) -> None:
        self.assert_blocked('git commit -m "chore: x', self.main_repo)


if __name__ == "__main__":
    unittest.main()
