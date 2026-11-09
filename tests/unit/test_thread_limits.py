"""One BLAS/OpenMP thread per process, the same in production, the test image and host tests.

Unlimited, numpy's OpenBLAS pool and scikit-learn's OpenMP pool spin against each other on
single-row requests (docs/benchmarks/phase-4-performance.md, step 3).
The OpenMP count also decides how scikit-learn's KNN breaks exact distance ties, so it is part
of the results the characterization and equivalence gates pin: a run with other limits is not
comparable to them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


def env_in_final_stage(dockerfile: Path) -> dict[str, str]:
    """``ENV`` key=value pairs of the last build stage (line continuations joined)."""
    text = dockerfile.read_text().replace("\\\n", " ")
    final_stage = re.split(r"^FROM\s", text, flags=re.MULTILINE)[-1]
    pairs: dict[str, str] = {}
    for line in final_stage.splitlines():
        if line.strip().startswith("ENV "):
            pairs.update(re.findall(r'(\w+)="?([^"\s]*)"?', line.strip()[4:]))
    return pairs


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.test"])
def test_images_limit_thread_pools_to_one(name: str) -> None:
    env = env_in_final_stage(REPO_ROOT / name)

    assert {key: env.get(key) for key in EXPECTED} == EXPECTED


def test_host_tests_use_the_same_limits() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    thread_env = re.search(r"^THREAD_ENV = (.*)$", makefile, flags=re.MULTILINE)

    assert thread_env is not None
    assert dict(pair.split("=") for pair in thread_env.group(1).split()) == EXPECTED
    assert "$(THREAD_ENV) $(UV_RUN) pytest" in makefile
