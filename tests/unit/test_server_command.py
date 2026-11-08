"""The production image serves without --reload, with its worker count from WEB_CONCURRENCY.

Phase 4, step 8 (docs/benchmarks/phase-4-performance.md): the v0 CMD ran uvicorn with
``--reload``, a development tool, in production. Workers are set at deploy time through
uvicorn's own ``WEB_CONCURRENCY``, which only applies when ``--workers`` is not on the command
line. Reload stays available for development through ``make docker-dev``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text()


def production_cmd() -> list[str]:
    commands = re.findall(r"^CMD (\[.*\])$", DOCKERFILE, flags=re.MULTILINE)
    assert len(commands) == 1, commands
    command: list[str] = json.loads(commands[0])
    return command


def test_production_cmd_does_not_reload() -> None:
    assert production_cmd()[0] == "uvicorn"
    assert not any(arg.startswith("--reload") for arg in production_cmd())


def test_worker_count_comes_from_web_concurrency() -> None:
    assert "--workers" not in production_cmd()
    default = re.search(r"^ENV WEB_CONCURRENCY=(\d+)$", DOCKERFILE, flags=re.MULTILINE)
    assert default is not None
    assert int(default.group(1)) >= 1


def test_reload_is_kept_for_the_dev_target_only() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    dev_target = re.search(r"^docker-dev:.*?(?=^\S|\Z)", makefile, flags=re.MULTILINE | re.DOTALL)

    assert dev_target is not None
    assert "--reload" in dev_target.group(0)
    commands = [line for line in makefile.splitlines() if not line.lstrip().startswith("#")]
    dev_commands = [line for line in dev_target.group(0).splitlines() if "--reload" in line]
    assert [line for line in commands if "--reload" in line] == dev_commands
