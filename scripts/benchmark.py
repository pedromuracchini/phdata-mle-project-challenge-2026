"""Closed-loop HTTP benchmark for ``POST /predict`` against a running container.

Load profile (defaults; keep them fixed when comparing before and after a change):

* payloads: the 100 rows of ``src/data/future_unseen_examples.csv``, mapped to the v0 request
  schema (8 fields, ``zipcode`` as a string), sent in CSV order and cycled;
* ``--profile`` decides which fields are sent as ``null`` (default ``complete``: none, the
  same bodies as every earlier benchmark). ``one-null``: row *i* has the house field
  ``i mod 7`` set to null, so each field is null in turn; three of the seven are sqft fields,
  which the service fills from the sqft identity without KNN. ``one-null-knn``: the same with
  only the four fields that always go to KNN (bedrooms, bathrooms, sqft_lot, floors).
  ``zipcode-only``: every house field null;
* ``--warmup 20`` requests, sent sequentially and not measured;
* ``--requests 1000`` measured requests from ``--concurrency 8`` clients. Each client is a
  thread with its own keep-alive ``http.client.HTTPConnection``; it sends the next request as
  soon as the previous response is read (closed loop, no think time);
* ``--runs 3`` repetitions of the measured phase; results are reported per run.

Latency is measured on the client, from just before the request is sent until the response
body has been read. Throughput is measured requests divided by the wall time of the measured
phase. Any non-200 response counts as an error and is excluded from the latency percentiles.

Standard library only, so it runs on the host whatever the service's dependencies are.

    python3 scripts/benchmark.py --base-url http://localhost:8000 --output results.json
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import logging
import math
import platform
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "src" / "data" / "future_unseen_examples.csv"

# v0 request schema (src/api/endpoints.py, HomeFeatures), same mapping as generate_baseline.py.
INT_FIELDS = ("bedrooms",)
FLOAT_FIELDS = ("bathrooms", "sqft_living", "sqft_lot", "floors", "sqft_above", "sqft_basement")
STR_FIELDS = ("zipcode",)

HOUSE_FIELDS = INT_FIELDS + FLOAT_FIELDS
KNN_FIELDS = ("bedrooms", "bathrooms", "sqft_lot", "floors")
PROFILES = ("complete", "one-null", "one-null-knn", "zipcode-only")

logger = logging.getLogger("benchmark")


@dataclass
class RunResult:
    run: int
    requests: int
    concurrency: int
    errors: int
    wall_time_s: float
    throughput_rps: float
    latency_ms: dict[str, float] = field(default_factory=dict)


def null_fields(profile: str, index: int) -> tuple[str, ...]:
    """House fields sent as null for CSV row ``index`` under ``profile``."""
    if profile == "complete":
        return ()
    if profile == "one-null":
        return (HOUSE_FIELDS[index % len(HOUSE_FIELDS)],)
    if profile == "one-null-knn":
        return (KNN_FIELDS[index % len(KNN_FIELDS)],)
    if profile == "zipcode-only":
        return HOUSE_FIELDS
    raise ValueError(f"unknown profile {profile!r}")


def load_payloads(csv_path: Path, profile: str = "complete") -> list[bytes]:
    """Encoded request bodies, one per CSV row."""
    bodies = []
    with csv_path.open(newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            payload: dict[str, Any] = {name: int(row[name]) for name in INT_FIELDS}
            payload.update({name: float(row[name]) for name in FLOAT_FIELDS})
            payload.update({name: row[name].strip() for name in STR_FIELDS})
            payload.update(dict.fromkeys(null_fields(profile, index)))
            bodies.append(json.dumps(payload).encode())
    return bodies


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


class Client:
    """One keep-alive connection; reconnects once if the server closed it."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._args = (host, port)
        self._timeout = timeout
        self._conn = http.client.HTTPConnection(host, port, timeout=timeout)

    def post(self, path: str, body: bytes) -> tuple[int, float]:
        headers = {"Content-Type": "application/json"}
        for attempt in (1, 2):
            start = time.perf_counter()
            try:
                self._conn.request("POST", path, body=body, headers=headers)
                response = self._conn.getresponse()
                response.read()
                return response.status, (time.perf_counter() - start) * 1000
            except (http.client.HTTPException, ConnectionError):
                self._conn.close()
                self._conn = http.client.HTTPConnection(*self._args, timeout=self._timeout)
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._conn.close()


def measured_run(
    run: int,
    host: str,
    port: int,
    bodies: list[bytes],
    requests: int,
    concurrency: int,
    timeout: float,
) -> RunResult:
    counter = iter(range(requests))
    lock = threading.Lock()
    latencies: list[float] = []
    errors = 0

    def worker() -> None:
        nonlocal errors
        client = Client(host, port, timeout)
        try:
            while True:
                with lock:
                    index = next(counter, None)
                if index is None:
                    return
                status, elapsed_ms = client.post("/predict", bodies[index % len(bodies)])
                with lock:
                    if status == 200:
                        latencies.append(elapsed_ms)
                    else:
                        errors += 1
        finally:
            client.close()

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker) for _ in range(concurrency)]
        for future in futures:
            future.result()
    wall_time = time.perf_counter() - start

    latencies.sort()
    latency_ms = {
        "min": latencies[0],
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "max": latencies[-1],
        "mean": sum(latencies) / len(latencies),
    }
    return RunResult(
        run=run,
        requests=requests,
        concurrency=concurrency,
        errors=errors,
        wall_time_s=wall_time,
        throughput_rps=requests / wall_time,
        latency_ms={k: round(v, 3) for k, v in latency_ms.items()},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--profile", choices=PROFILES, default="complete")
    parser.add_argument("--output", type=Path, help="write results as JSON to this file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    url = urllib.parse.urlsplit(args.base_url)
    host, port = url.hostname or "localhost", url.port or 80
    bodies = load_payloads(args.csv, args.profile)

    health = http.client.HTTPConnection(host, port, timeout=args.timeout)
    health.request("GET", "/health")
    if health.getresponse().status != 200:
        logger.error("service at %s is not healthy", args.base_url)
        return 1
    health.close()

    warmup = Client(host, port, args.timeout)
    for index in range(args.warmup):
        warmup.post("/predict", bodies[index % len(bodies)])
    warmup.close()

    results = []
    for run in range(1, args.runs + 1):
        result = measured_run(
            run, host, port, bodies, args.requests, args.concurrency, args.timeout
        )
        results.append(result)
        logger.info(
            "run %d: %.1f req/s, p50 %.1f ms, p95 %.1f ms, p99 %.1f ms, errors %d",
            run,
            result.throughput_rps,
            result.latency_ms["p50"],
            result.latency_ms["p95"],
            result.latency_ms["p99"],
            result.errors,
        )

    if args.output:
        report = {
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "base_url": args.base_url,
            "load": {
                "requests": args.requests,
                "concurrency": args.concurrency,
                "warmup": args.warmup,
                "runs": args.runs,
                "payloads": f"{len(bodies)} rows of {args.csv.name}, cycled",
                "profile": args.profile,
            },
            "client_python": platform.python_version(),
            "runs": [asdict(result) for result in results],
        }
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        logger.info("wrote %s", args.output)
    return 1 if any(result.errors for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
