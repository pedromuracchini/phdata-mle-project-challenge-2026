"""Load profiles of scripts/benchmark.py: which fields each one sends as null."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark.py"


@pytest.fixture(scope="module")
def benchmark() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def nulls(body: bytes) -> list[str]:
    return [name for name, value in json.loads(body).items() if value is None]


def test_complete_profile_sends_no_nulls(benchmark: ModuleType) -> None:
    bodies = benchmark.load_payloads(benchmark.DEFAULT_CSV, "complete")

    assert len(bodies) == 100
    assert all(nulls(body) == [] for body in bodies)


def test_one_null_cycles_through_every_house_field(benchmark: ModuleType) -> None:
    bodies = benchmark.load_payloads(benchmark.DEFAULT_CSV, "one-null")

    assert [nulls(body) for body in bodies[:7]] == [[f] for f in benchmark.HOUSE_FIELDS]


def test_one_null_knn_only_nulls_fields_that_go_to_knn(benchmark: ModuleType) -> None:
    bodies = benchmark.load_payloads(benchmark.DEFAULT_CSV, "one-null-knn")

    assert {tuple(nulls(body)) for body in bodies} == {(f,) for f in benchmark.KNN_FIELDS}


def test_zipcode_only_nulls_every_house_field(benchmark: ModuleType) -> None:
    body = json.loads(benchmark.load_payloads(benchmark.DEFAULT_CSV, "zipcode-only")[0])

    assert body["zipcode"]
    assert nulls(json.dumps(body).encode()) == list(benchmark.HOUSE_FIELDS)
