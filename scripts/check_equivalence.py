"""Check that the service imputes and predicts bit-for-bit like a reference build (main).

Phase 4 changes how a request is processed, never its result. The characterization test pins
complete requests; this script pins requests with missing fields. It calls the service's own
``POST /predict`` route in-process (with the app's lifespan, as served), for every row of
``future_unseen_examples.csv`` × every subset of the seven house fields set to null (128
patterns, the empty one included), and records for each call:

* the 33 feature values the model received, after imputation (captured in
  ``Pipeline.predict``; float64 bytes, so the check is about values, not dtypes);
* the predicted price (float64 bytes) and ``imputed_fields``.

They are hashed per null pattern. ``--write`` stores the hashes as a reference (generated from
main's image); ``--check`` recomputes them and fails on any difference, naming the patterns.

Run it inside the service image, where the library versions are the locked ones:

    docker run --rm -w /app -v "$PWD/scripts:/scripts:ro" -v "$PWD/tests/fixtures:/ref" \\
        IMAGE python /scripts/check_equivalence.py --check /ref/imputation_reference.json

(``make check-equivalence`` builds the working tree's image and does this.)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import inspect
import itertools
import json
import logging
import os
import platform
import struct
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("check_equivalence")

HOUSE_FIELDS = (
    "bedrooms",
    "bathrooms",
    "sqft_living",
    "sqft_lot",
    "floors",
    "sqft_above",
    "sqft_basement",
)
THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


def null_patterns() -> list[tuple[str, ...]]:
    """Every subset of the house fields, smallest first (128, the empty one included)."""
    return [
        combo
        for size in range(len(HOUSE_FIELDS) + 1)
        for combo in itertools.combinations(HOUSE_FIELDS, size)
    ]


def pattern_key(pattern: tuple[str, ...]) -> str:
    return ",".join(pattern) or "(none)"


def read_rows(csv_path: Path) -> list[dict[str, Any]]:
    """Request payloads for the unseen examples (v0 schema, ``zipcode`` as a string)."""
    with csv_path.open(newline="") as handle:
        return [
            {
                "bedrooms": int(row["bedrooms"]),
                **{name: float(row[name]) for name in HOUSE_FIELDS[1:]},
                "zipcode": row["zipcode"].strip(),
            }
            for row in csv.DictReader(handle)
        ]


def record_bytes(features: Any, response: dict[str, Any]) -> bytes:
    """What one call is compared on: model input values, prediction and imputed fields."""
    values = np.ascontiguousarray(np.asarray(features, dtype=np.float64))
    return (
        values.tobytes()
        + struct.pack("<d", float(response["predicted_price"]))
        + json.dumps(response["imputed_fields"]).encode()
    )


def capture_model_input() -> list[Any]:
    """Patch ``Pipeline.predict`` (class level, so per-request unpickled models are covered
    too) to record the feature frame the model receives."""
    from sklearn.pipeline import Pipeline

    original = Pipeline.predict
    captured: list[Any] = []

    def predict(self: Any, X: Any, **params: Any) -> Any:  # noqa: N803 (sklearn's name)
        captured.append(X.copy())
        return original(self, X, **params)

    Pipeline.predict = predict
    return captured


async def hashes_per_pattern(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    from starlette.requests import Request

    from api.endpoints import HomeFeatures, predict
    from main import app

    captured = capture_model_input()
    results = []
    async with app.router.lifespan_context(app):
        request = Request({"type": "http", "app": app})
        call: Callable[..., Any] = predict
        for pattern in null_patterns():
            digest = hashlib.sha256()
            for row in rows:
                features = HomeFeatures(**{**row, **dict.fromkeys(pattern)})
                captured.clear()
                response = call(features, request)
                if inspect.isawaitable(response):
                    response = await response
                if len(captured) != 1:
                    raise RuntimeError(f"model.predict called {len(captured)} times, expected 1")
                digest.update(record_bytes(captured[0], response))
            results.append((pattern_key(pattern), digest.hexdigest()))
    return results


def compute(rows: list[dict[str, Any]]) -> dict[str, str]:
    return dict(asyncio.run(hashes_per_pattern(rows)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", type=Path, help="write the reference hashes to this file")
    mode.add_argument("--check", type=Path, help="compare with the reference in this file")
    parser.add_argument("--source", default="", help="what is being run (stored with --write)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    sys.path.insert(0, os.getcwd())
    from core.config import DATA_DIR

    rows = read_rows(DATA_DIR / "future_unseen_examples.csv")
    start = time.perf_counter()
    hashes = compute(rows)
    elapsed = time.perf_counter() - start
    logger.info("%d patterns x %d rows in %.0f s", len(hashes), len(rows), elapsed)

    if args.write:
        reference = {
            "generator": "scripts/check_equivalence.py",
            "source": args.source,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "python": platform.python_version(),
            "thread_env": {name: os.environ.get(name) for name in THREAD_VARS},
            "rows": len(rows),
            "compared": "per pattern, sha256 over every row of: the model's 33 input values "
            "after imputation (float64), predicted_price (float64), imputed_fields",
            "patterns": hashes,
        }
        args.write.write_text(json.dumps(reference, indent=2) + "\n")
        logger.info("wrote %s", args.write)
        return 0

    reference = json.loads(args.check.read_text())
    if reference["rows"] != len(rows):
        logger.error("reference has %d rows, this run has %d", reference["rows"], len(rows))
        return 1
    different = [key for key, value in reference["patterns"].items() if hashes.get(key) != value]
    missing = sorted(set(reference["patterns"]) ^ set(hashes))
    if different or missing:
        logger.error("%d of %d patterns differ: %s", len(different), len(hashes), different)
        if missing:
            logger.error("patterns present on one side only: %s", missing)
        return 1
    logger.info("all %d patterns identical to the reference (%s)", len(hashes), reference["source"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
