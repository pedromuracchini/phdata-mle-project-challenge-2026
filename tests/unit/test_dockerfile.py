"""The API image runs as a non-root user, checks /health with Python and ships no curl."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text().replace("\\\n", " ")
COMPOSE = (REPO_ROOT / "docker-compose.test.yml").read_text()
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text().split()


def runtime_stage() -> str:
    """The last stage: the image that is shipped."""
    return re.split(r"^FROM ", DOCKERFILE, flags=re.MULTILINE)[-1]


def test_runtime_stage_runs_as_a_non_root_user() -> None:
    users = re.findall(r"^USER (\S+)$", runtime_stage(), flags=re.MULTILINE)
    assert users, "the runtime stage has no USER"
    assert users[-1] not in {"root", "0", "0:0"}


def test_healthcheck_probes_health_with_python() -> None:
    [check] = re.findall(r"^HEALTHCHECK .*?CMD (\[.*\])$", runtime_stage(), flags=re.MULTILINE)
    command = json.loads(check)
    assert command[0] == "python"
    assert "/health" in " ".join(command)


def instructions(text: str) -> str:
    """The text without comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_no_curl_in_the_image_or_the_compose_healthcheck() -> None:
    assert "curl" not in instructions(runtime_stage())
    assert "curl" not in instructions(COMPOSE)


def test_base_images_are_pinned_by_digest() -> None:
    for image in re.findall(r"^(?:FROM|ARG PYTHON_IMAGE=)(\S+)", DOCKERFILE, flags=re.MULTILINE):
        if image.startswith("${") or " AS " in image:
            continue
        assert "@sha256:" in image, image


def test_dockerignore_keeps_repo_only_material_out() -> None:
    for entry in (".git/", ".github/", ".claude/", "docs/", "notebooks/", ".venv/", ".env"):
        assert entry in DOCKERIGNORE, entry
