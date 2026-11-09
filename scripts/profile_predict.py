"""Profile the ``POST /predict`` handler with cProfile.

Runs inside the service image, from the app directory, so the handler's relative paths
(``model/model.pkl``, ``data/zipcode_demographics.csv``) resolve as they do in production. It
calls the route function directly with a validated ``HomeFeatures``: request parsing and HTTP
handling by uvicorn/FastAPI are **not** included, only the handler body.

Sequence: one warm-up call (imports and first-use costs), then one profiled call, then an
aggregate profile over ``--repeat`` calls for steadier numbers. Writes a text report (pstats
sorted by cumulative and by internal time) and the raw ``.prof`` files to ``--out-dir``.

    docker run --rm -w /app --user "$(id -u):$(id -g)" \\
      -v "$PWD/scripts:/scripts:ro" -v "$PWD/docs/benchmarks:/out" \\
      mle-api:v0-original python /scripts/profile_predict.py --out-dir /out

Only the standard library and the service's own modules are used.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import cProfile
import inspect
import io
import os
import platform
import pstats
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Same payload as the reference edge case in docs/exploration.md.
PAYLOAD: dict[str, Any] = {
    "bedrooms": 3,
    "bathrooms": 2.0,
    "sqft_living": 1500.0,
    "sqft_lot": 5000.0,
    "floors": 1.0,
    "sqft_above": 1200.0,
    "sqft_basement": 300.0,
    "zipcode": "98042",
}
# --payload: ``complete`` (no imputation), ``knn`` (one field that always goes to KNN, like the
# one-null-knn benchmark profile) or ``zipcode-only`` (every house field missing).
PAYLOADS: dict[str, dict[str, Any]] = {
    "complete": PAYLOAD,
    "knn": {**PAYLOAD, "bedrooms": None},
    "zipcode-only": {"zipcode": PAYLOAD["zipcode"]},
}
TOP = 30
# Thread-pool sizes of OpenMP (scikit-learn's Cython code) and the BLAS numpy links to.
THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


def thread_env() -> str:
    return "  ".join(f"{name}={os.environ.get(name, '(unset)')}" for name in THREAD_VARS)


def stats_text(profile: cProfile.Profile, sort: str, title: str) -> str:
    buffer = io.StringIO()
    stats = pstats.Stats(profile, stream=buffer)
    stats.strip_dirs().sort_stats(sort).print_stats(TOP)
    return f"\n{'=' * 100}\n{title} (sorted by {sort}, top {TOP})\n{'=' * 100}\n{buffer.getvalue()}"


def step_timings(repeat: int, payload: dict[str, Any], imputer: Any) -> str:
    """Median wall time of each handler step, without the profiler.

    Repeats the steps of ``api.endpoints.predict`` one by one (same calls, same order) so their
    cost can be read without cProfile overhead, which inflates pure-Python code such as pandas
    formatting. Measurement only: the service's code is not changed or reused here.
    """
    import json
    import pickle
    import statistics

    import pandas as pd

    from api.endpoints import HomeFeatures
    from services.imputation import impute_missing

    features = HomeFeatures(**payload)
    samples: dict[str, list[float]] = {}

    def timed(label: str, fn: Any) -> Any:
        start = time.perf_counter()
        value = fn()
        samples.setdefault(label, []).append((time.perf_counter() - start) * 1000)
        return value

    def read_pickle() -> Any:
        with open("model/model.pkl", "rb") as handle:
            return pickle.load(handle)

    def read_features() -> Any:
        with open("model/model_features.json") as handle:
            return json.load(handle)

    def one_iteration() -> None:
        model = timed("1 open + pickle.load model.pkl", read_pickle)
        model_features = timed("2 open + json.load model_features.json", read_features)
        frame = timed(
            "3 pd.DataFrame([features.model_dump()])",
            lambda: pd.DataFrame([features.model_dump()]),
        )
        demographics = timed(
            "4 pd.read_csv zipcode_demographics.csv",
            lambda: pd.read_csv("data/zipcode_demographics.csv", dtype={"zipcode": str}),
        )
        info = timed(
            "5 filter zipcode + drop + reset_index",
            lambda: (
                demographics[demographics["zipcode"] == features.zipcode]
                .drop(columns="zipcode")
                .reset_index(drop=True)
            ),
        )
        merged = timed("6 pd.concat(axis=1)", lambda: pd.concat([frame, info], axis=1))
        with contextlib.redirect_stdout(io.StringIO()):
            timed("7 print(DataFrame) (formatting; stdout redirected)", lambda: print(merged))
        selected = timed("8 select model_features", lambda: merged[model_features])
        imputed = timed(
            "9 impute_missing (sqft fill + KNN, if anything is missing)",
            lambda: impute_missing(selected, imputer.imputer, imputer.columns),
        )
        timed("10 model.predict", lambda: model.predict(imputed.features))

    for _ in range(repeat + 1):  # first iteration discarded below (warm-up)
        one_iteration()

    lines = [
        f"\n{'=' * 100}",
        f"STEP TIMINGS WITHOUT PROFILER (median of {repeat} iterations after 1 warm-up, ms)",
        thread_env(),
        "=" * 100,
    ]
    total = 0.0
    for label, values in samples.items():
        median = statistics.median(values[1:])
        total += median
        lines.append(f"{label:58s} {median:8.2f}")
    lines.append(f"{'sum of step medians':58s} {total:8.2f}")
    return "\n".join(lines) + "\n"


def startup_request() -> Any:
    """A request whose app holds what the lifespan loads (``main.load_artifacts``).

    Startup work is done once here, outside the profile, as it is in the service. (The v0
    route took no request; its profile is already recorded in docs/benchmarks/.)
    """
    from starlette.requests import Request

    from core.config import get_settings
    from main import app

    try:
        from main import load_artifacts
    except ImportError:  # Phase 3 images: only the imputer is loaded at startup
        from core.artifacts import load_imputer

        app.state.imputer = load_imputer(get_settings())
    else:
        load_artifacts(app.state, get_settings())
    return Request({"type": "http", "app": app})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=50)
    parser.add_argument("--name", default="baseline-profile")
    parser.add_argument("--payload", choices=sorted(PAYLOADS), default="complete")
    args = parser.parse_args(argv)

    sys.path.insert(0, os.getcwd())
    from api.endpoints import HomeFeatures, predict  # the service's own route

    payload = PAYLOADS[args.payload]
    features = HomeFeatures(**payload)
    request = startup_request()

    def call() -> Any:
        # The handler prints the DataFrame (v0); keep that cost but not the noise.
        with contextlib.redirect_stdout(io.StringIO()):
            result: Any = predict(features, request)
            # `async def` before Phase 4 step 7, so older images can still be profiled.
            return asyncio.run(result) if inspect.iscoroutine(result) else result

    warmup_result = call()

    single = cProfile.Profile()
    start = time.perf_counter()
    single.enable()
    result = call()
    single.disable()
    single_ms = (time.perf_counter() - start) * 1000

    aggregate = cProfile.Profile()
    start = time.perf_counter()
    aggregate.enable()
    for _ in range(args.repeat):
        call()
    aggregate.disable()
    aggregate_ms = (time.perf_counter() - start) * 1000 / args.repeat

    args.out_dir.mkdir(parents=True, exist_ok=True)
    single.dump_stats(args.out_dir / f"{args.name}-single.prof")
    aggregate.dump_stats(args.out_dir / f"{args.name}-x{args.repeat}.prof")

    header = "\n".join(
        [
            f"cProfile of the POST /predict handler ({__file__})",
            f"generated_at: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"python: {platform.python_version()}  cwd: {os.getcwd()}",
            f"{thread_env()}  cpu_count={os.cpu_count()}",
            f"payload ({args.payload}): {payload}",
            f"warm-up result: {warmup_result}  profiled result: {result}",
            f"single profiled call, wall time under profiler: {single_ms:.1f} ms",
            f"aggregate: {args.repeat} calls, mean wall time under profiler: {aggregate_ms:.1f} ms",
            "Scope: handler body only (no HTTP parsing, no uvicorn). Times include cProfile "
            "overhead.",
        ]
    )
    report = (
        header
        + stats_text(single, "cumulative", "SINGLE CALL")
        + stats_text(single, "tottime", "SINGLE CALL")
        + stats_text(aggregate, "cumulative", f"AGGREGATE OF {args.repeat} CALLS")
        + stats_text(aggregate, "tottime", f"AGGREGATE OF {args.repeat} CALLS")
        + step_timings(args.repeat, payload, request.app.state.imputer)
    )
    (args.out_dir / f"{args.name}.txt").write_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
