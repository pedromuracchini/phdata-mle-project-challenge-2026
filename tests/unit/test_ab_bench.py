"""Pure helpers of scripts/ab_bench.py: run order, cgroup parsing and the summary."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ab_bench.py"


@pytest.fixture(scope="module")
def ab() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ab_bench", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def test_arms_alternate_order_every_round(ab: ModuleType) -> None:
    order = ab.run_order(3, ("complete", "zipcode-only"))

    assert order == [
        (1, "complete", "control"),
        (1, "complete", "candidate"),
        (1, "zipcode-only", "control"),
        (1, "zipcode-only", "candidate"),
        (2, "complete", "candidate"),
        (2, "complete", "control"),
        (2, "zipcode-only", "candidate"),
        (2, "zipcode-only", "control"),
        (3, "complete", "control"),
        (3, "complete", "candidate"),
        (3, "zipcode-only", "control"),
        (3, "zipcode-only", "candidate"),
    ]


def test_cpu_stat_is_parsed(ab: ModuleType) -> None:
    text = "usage_usec 1500000\nuser_usec 1000000\nsystem_usec 500000\nnr_periods 0\n"

    assert ab.parse_cpu_stat(text)["usage_usec"] == 1_500_000


def test_cpu_summary_per_request_and_cores(ab: ModuleType) -> None:
    # 2 CPU-seconds over 1 s of wall time for 100 requests: 20 ms each, 2 cores busy.
    summary = ab.cpu_summary(1_000_000, 3_000_000, requests=100, wall_s=1.0)

    assert summary == {"cpu_ms_total": 2000.0, "cpu_ms_per_request": 20.0, "cores_busy": 2.0}


def test_cpu_list_syntax(ab: ModuleType) -> None:
    assert ab.parse_cpu_list("0-3,8") == {0, 1, 2, 3, 8}
    assert ab.parse_cpu_list("12-15") == {12, 13, 14, 15}


def run(arm: str, rps: float, p50: float, profile: str = "complete") -> dict[str, Any]:
    return {
        "profile": profile,
        "arm": arm,
        "errors": 0,
        "throughput_rps": rps,
        "latency_ms": {"p50": p50, "p95": p50 * 2, "p99": p50 * 3},
        "server_cpu": {"cpu_ms_per_request": 10.0, "cores_busy": 1.0},
        "client_cpu": {"cores_busy": 0.2},
        "memory": {"current_mib": 300.0},
    }


def test_summary_is_the_median_per_arm_and_the_ratio(ab: ModuleType) -> None:
    runs = [
        run("control", 10, 100),
        run("candidate", 20, 50),
        run("candidate", 40, 25),
        run("control", 12, 90),
        run("control", 50, 80),  # an outlier does not move the median
        run("candidate", 30, 30),
    ]

    summary = ab.summarize(runs)["complete"]

    assert summary["control"]["throughput_rps"] == 12
    assert summary["candidate"]["throughput_rps"] == 30
    assert summary["control"]["p50_ms"] == 90
    assert summary["ratio"]["throughput_rps"] == 2.5
    assert summary["ratio"]["p50_ms"] == pytest.approx(30 / 90, abs=1e-3)
    assert summary["control"]["runs"] == 3


def test_markdown_table_has_a_ratio_row_per_profile(ab: ModuleType) -> None:
    runs = [
        run(arm, 10, 10, profile) for profile in ("complete", "zipcode-only") for arm in ab.ARMS
    ]

    table = ab.markdown_table(ab.summarize(runs))

    assert table.count("candidate / control") == 2
