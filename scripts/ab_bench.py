"""Interleaved A/B benchmark of two service images: control vs candidate, same session.

Every Phase 4 number comes from this script (docs/benchmarks/phase-4-performance.md):

* **Images.** Each arm is a git ref, built from a clean ``git archive`` of that commit, or
  ``worktree``: the working tree as it is, so a change can be measured before it is committed.
  Both arms may name the same ref: that run measures the noise floor of the method.
* **Arm options.** ``--control-args`` / ``--candidate-args`` are extra ``docker run`` arguments
  (e.g. ``-e OMP_NUM_THREADS=1``), so two configurations of one image can be compared too.
* **Order.** ``--rounds 3`` rounds. In each round, for each profile, both arms run one after
  the other, A then B in odd rounds and B then A in even rounds (A,B / B,A / A,B), so drift
  does not always penalise the same arm. Every run gets a fresh container.
* **One run.** Start the container, wait for ``/health``, send ``--warmup`` requests
  sequentially (not measured), then one closed-loop measured phase from ``benchmark.py``
  (``--requests`` requests, ``--concurrency`` keep-alive clients), then remove the container.
* **CPU layout.** The server container is pinned to ``--server-cpus`` and this client process
  to ``--client-cpus``. Disjoint sets keep the client from stealing CPU from the server;
  ``--server-cpus`` covering every CPU (the default) leaves the server unpinned. Pinning an
  image whose OpenMP pool is not limited distorts it (phase-4-performance.md), so the layout
  is recorded with every result.
* **Measured besides latency.** Server CPU from the container's cgroup ``cpu.stat`` (delta over
  the measured phase: CPU-ms per request and average cores busy), client CPU from
  ``getrusage`` (to see whether the client is the bottleneck), the container's memory and the
  RSS of each of its processes at the end of the run.
* **Reported.** Per run, and per arm and profile the median of the rounds, plus the
  candidate/control ratio. Machine specs, image IDs and git SHAs are recorded with the result.

Standard library only, like ``benchmark.py`` (which it reuses for the load itself). Needs
Docker and cgroup v2 (``/sys/fs/cgroup``).

    uv run python scripts/ab_bench.py --control main --candidate worktree \\
        --output docs/benchmarks/phase4-step2-ab.json
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
import logging
import os
import platform
import resource
import shlex
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ARMS = ("control", "candidate")
DEFAULT_PROFILES = ("complete", "one-null-knn", "zipcode-only")
WORKTREE = "worktree"
CGROUP_ROOT = Path("/sys/fs/cgroup")

logger = logging.getLogger("ab_bench")


def _load_benchmark() -> ModuleType:
    """``scripts/benchmark.py`` (the load generator), loaded by path: scripts/ is no package."""
    path = Path(__file__).resolve().parent / "benchmark.py"
    spec = importlib.util.spec_from_file_location("benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


benchmark = _load_benchmark()


# --------------------------------------------------------------------------- pure helpers


def run_order(rounds: int, profiles: tuple[str, ...]) -> list[tuple[int, str, str]]:
    """``(round, profile, arm)`` in execution order: A,B in odd rounds, B,A in even rounds."""
    order = []
    for round_number in range(1, rounds + 1):
        arms = ARMS if round_number % 2 == 1 else tuple(reversed(ARMS))
        for profile in profiles:
            for arm in arms:
                order.append((round_number, profile, arm))
    return order


def parse_cpu_stat(text: str) -> dict[str, int]:
    """cgroup v2 ``cpu.stat`` as a dict (``usage_usec``, ``user_usec``, ...)."""
    stats = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            stats[key] = int(value)
    return stats


def cpu_summary(
    before_usec: int, after_usec: int, requests: int, wall_s: float
) -> dict[str, float]:
    """CPU-ms per request and average busy cores over a measured phase."""
    used_ms = (after_usec - before_usec) / 1000
    return {
        "cpu_ms_total": round(used_ms, 1),
        "cpu_ms_per_request": round(used_ms / requests, 3),
        "cores_busy": round(used_ms / 1000 / wall_s, 3),
    }


def parse_cpu_list(spec: str) -> set[int]:
    """``"0-3,8"`` -> ``{0, 1, 2, 3, 8}`` (the cpuset syntax ``docker run --cpuset-cpus`` uses)."""
    cpus: set[int] = set()
    for part in spec.split(","):
        start, _, end = part.strip().partition("-")
        cpus.update(range(int(start), int(end or start) + 1))
    return cpus


METRICS = (
    ("throughput_rps", ("throughput_rps",)),
    ("p50_ms", ("latency_ms", "p50")),
    ("p95_ms", ("latency_ms", "p95")),
    ("p99_ms", ("latency_ms", "p99")),
    ("server_cpu_ms_per_request", ("server_cpu", "cpu_ms_per_request")),
    ("server_cores_busy", ("server_cpu", "cores_busy")),
    ("client_cores_busy", ("client_cpu", "cores_busy")),
    ("memory_mib", ("memory", "current_mib")),
)


def _get(run: dict[str, Any], path: tuple[str, ...]) -> float:
    value: Any = run
    for key in path:
        value = value[key]
    return float(value)


def summarize(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per profile: the median of each metric per arm, and candidate / control."""
    summary: dict[str, dict[str, Any]] = {}
    for profile in dict.fromkeys(run["profile"] for run in runs):
        entry: dict[str, Any] = {}
        for arm in ARMS:
            arm_runs = [r for r in runs if r["profile"] == profile and r["arm"] == arm]
            entry[arm] = {
                name: round(statistics.median(_get(r, path) for r in arm_runs), 3)
                for name, path in METRICS
            }
            entry[arm]["runs"] = len(arm_runs)
            entry[arm]["errors"] = sum(r["errors"] for r in arm_runs)
        entry["ratio"] = {
            name: round(entry["candidate"][name] / entry["control"][name], 3)
            for name, _ in METRICS
            if entry["control"][name]
        }
        summary[profile] = entry
    return summary


def markdown_table(summary: dict[str, dict[str, Any]]) -> str:
    """The summary as a Markdown table (medians of the rounds)."""
    lines = [
        "| Profile | Arm | req/s | p50 ms | p95 ms | p99 ms | server CPU ms/req | "
        "server cores | client cores | MiB | errors |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for profile, entry in summary.items():
        for arm in (*ARMS, "ratio"):
            values = entry[arm]
            label = "candidate / control" if arm == "ratio" else arm
            cells = [
                f"{values.get(name, float('nan')):.3g}" if arm == "ratio" else f"{values[name]:.1f}"
                for name, _ in METRICS
            ]
            errors = "" if arm == "ratio" else str(values["errors"])
            lines.append(f"| {profile} | {label} | " + " | ".join(cells) + f" | {errors} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- environment


def _run(cmd: list[str], **kwargs: Any) -> str:
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
    return str(result.stdout).strip()


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return "unknown"


def machine_info() -> dict[str, Any]:
    cpuinfo = _read(Path("/proc/cpuinfo"))
    model = next(
        (line.split(":", 1)[1].strip() for line in cpuinfo.splitlines() if "model name" in line),
        "unknown",
    )
    cores = {
        _read(Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/core_id"))
        for cpu in range(os.cpu_count() or 0)
    }
    meminfo = dict(
        line.split(":", 1) for line in _read(Path("/proc/meminfo")).splitlines() if ":" in line
    )
    return {
        "cpu_model": model,
        "logical_cpus": os.cpu_count(),
        "physical_cores": len(cores),
        "governor": _read(Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")),
        "mem_total": meminfo.get("MemTotal", "").strip(),
        "mem_available_at_start": meminfo.get("MemAvailable", "").strip(),
        "kernel": platform.release(),
        "docker": _run(["docker", "version", "--format", "{{.Server.Version}}"]),
        "client_python": platform.python_version(),
    }


@dataclass
class Arm:
    name: str
    ref: str
    docker_args: list[str]
    image: str = ""
    image_id: str = ""
    git_sha: str = ""
    worktree_diff_sha256: str | None = None


def build_image(arm: Arm) -> None:
    """Build the arm's image: a clean archive of ``ref``, or the working tree."""
    if arm.ref == WORKTREE:
        arm.git_sha = _run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT)
        diff = _run(["git", "diff", "HEAD"], cwd=REPO_ROOT)
        untracked = _run(["git", "status", "--porcelain"], cwd=REPO_ROOT)
        arm.worktree_diff_sha256 = hashlib.sha256((diff + untracked).encode()).hexdigest()
        arm.image = f"mle-ab:worktree-{arm.git_sha[:12]}-{arm.worktree_diff_sha256[:8]}"
        logger.info("building %s from the working tree", arm.image)
        _run(["docker", "build", "-q", "-t", arm.image, "."], cwd=REPO_ROOT)
    else:
        arm.git_sha = _run(["git", "rev-parse", f"{arm.ref}^{{commit}}"], cwd=REPO_ROOT)
        arm.image = f"mle-ab:{arm.git_sha[:12]}"
        logger.info("building %s from git archive %s", arm.image, arm.ref)
        archive = subprocess.Popen(
            ["git", "archive", "--format=tar", arm.git_sha], cwd=REPO_ROOT, stdout=subprocess.PIPE
        )
        subprocess.run(
            ["docker", "build", "-q", "-t", arm.image, "-"],
            stdin=archive.stdout,
            check=True,
            capture_output=True,
        )
        if archive.wait() != 0:
            raise RuntimeError(f"git archive {arm.ref} failed")
    arm.image_id = _run(["docker", "image", "inspect", "-f", "{{.Id}}", arm.image])


class Container:
    """One service container, pinned to the server CPUs, removed on exit."""

    def __init__(self, image: str, name: str, port: int, cpus: str, args: list[str]) -> None:
        self.name = name
        self.port = port
        self.id = _run(
            [
                "docker", "run", "-d", "--rm", "--name", name,
                "--cpuset-cpus", cpus, "-p", f"127.0.0.1:{port}:8000",
                *args, image,
            ]
        )  # fmt: skip
        self.cgroup = CGROUP_ROOT / "system.slice" / f"docker-{self.id}.scope"

    def wait_healthy(self, timeout_s: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                conn.request("GET", "/health")
                if conn.getresponse().status == 200:
                    conn.close()
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.25)
        logs = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True)
        raise RuntimeError(f"{self.name} not healthy after {timeout_s}s:\n{logs.stderr[-2000:]}")

    def cpu_usage_usec(self) -> int:
        return parse_cpu_stat((self.cgroup / "cpu.stat").read_text())["usage_usec"]

    def memory(self) -> dict[str, Any]:
        """Container memory now and at peak, and the RSS of each of its processes."""
        current = int((self.cgroup / "memory.current").read_text())
        peak_path = self.cgroup / "memory.peak"
        peak = int(peak_path.read_text()) if peak_path.exists() else None
        processes = []
        for pid in (self.cgroup / "cgroup.procs").read_text().split():
            status = _read(Path(f"/proc/{pid}/status"))
            fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
            cmdline = _read(Path(f"/proc/{pid}/cmdline")).replace("\0", " ").strip()
            processes.append(
                {
                    "pid": int(pid),
                    "cmdline": cmdline[:200],
                    "rss_kib": int(fields.get("VmRSS", "0 kB").split()[0]),
                }
            )
        return {
            "current_mib": round(current / 2**20, 1),
            "peak_mib": round(peak / 2**20, 1) if peak is not None else None,
            "processes": processes,
        }

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, check=False)


@dataclass
class RunRecord:
    round: int
    profile: str
    arm: str
    image: str
    requests: int
    concurrency: int
    errors: int
    wall_time_s: float
    throughput_rps: float
    latency_ms: dict[str, float]
    server_cpu: dict[str, float]
    client_cpu: dict[str, float]
    memory: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""


def one_run(
    round_number: int, profile: str, arm: Arm, args: argparse.Namespace, bodies: list[bytes]
) -> RunRecord:
    started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    container = Container(
        arm.image, f"ab-{arm.name}-r{round_number}-{profile}", args.port, args.server_cpus,
        arm.docker_args,
    )  # fmt: skip
    try:
        container.wait_healthy()
        warmup = benchmark.Client("127.0.0.1", args.port, args.timeout)
        for index in range(args.warmup):
            warmup.post("/predict", bodies[index % len(bodies)])
        warmup.close()

        cpu_before = container.cpu_usage_usec()
        rusage_before = resource.getrusage(resource.RUSAGE_SELF)
        result = benchmark.measured_run(
            round_number, "127.0.0.1", args.port, bodies, args.requests, args.concurrency,
            args.timeout,
        )  # fmt: skip
        rusage_after = resource.getrusage(resource.RUSAGE_SELF)
        cpu_after = container.cpu_usage_usec()
        memory = container.memory()
    finally:
        container.remove()

    client_s = (rusage_after.ru_utime - rusage_before.ru_utime) + (
        rusage_after.ru_stime - rusage_before.ru_stime
    )
    return RunRecord(
        round=round_number,
        profile=profile,
        arm=arm.name,
        image=arm.image,
        requests=result.requests,
        concurrency=result.concurrency,
        errors=result.errors,
        wall_time_s=round(result.wall_time_s, 3),
        throughput_rps=round(result.throughput_rps, 2),
        latency_ms=result.latency_ms,
        server_cpu=cpu_summary(cpu_before, cpu_after, result.requests, result.wall_time_s),
        client_cpu={"cores_busy": round(client_s / result.wall_time_s, 3)},
        memory=memory,
        started_at=started_at,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--control", default="main", help="git ref, or 'worktree'")
    parser.add_argument("--candidate", default=WORKTREE, help="git ref, or 'worktree'")
    parser.add_argument("--control-args", default="", help="extra docker run args (one string)")
    parser.add_argument("--candidate-args", default="", help="extra docker run args")
    parser.add_argument("--profiles", nargs="+", default=list(DEFAULT_PROFILES))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--server-cpus", default="0-15")
    parser.add_argument("--client-cpus", default="12-15")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--label", default="", help="free text stored with the result")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for profile in args.profiles:
        if profile not in benchmark.PROFILES:
            parser.error(f"unknown profile {profile!r}")
    os.sched_setaffinity(0, parse_cpu_list(args.client_cpus))

    arms = {
        "control": Arm("control", args.control, shlex.split(args.control_args)),
        "candidate": Arm("candidate", args.candidate, shlex.split(args.candidate_args)),
    }
    for arm in arms.values():
        build_image(arm)
    bodies = {p: benchmark.load_payloads(benchmark.DEFAULT_CSV, p) for p in args.profiles}

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "machine": machine_info(),
        "method": {
            "rounds": args.rounds,
            "order": "A,B in odd rounds, B,A in even rounds; fresh container per run",
            "requests": args.requests,
            "concurrency": args.concurrency,
            "warmup": args.warmup,
            "profiles": args.profiles,
            "server_cpus": args.server_cpus,
            "server_pinned": parse_cpu_list(args.server_cpus) != set(range(os.cpu_count() or 0)),
            "client_cpus": args.client_cpus,
            "payloads": f"{benchmark.DEFAULT_CSV.name}, cycled",
        },
        "arms": {name: asdict(arm) for name, arm in arms.items()},
        "runs": [],
    }

    runs: list[dict[str, Any]] = report["runs"]
    for round_number, profile, arm_name in run_order(args.rounds, tuple(args.profiles)):
        record = one_run(round_number, profile, arms[arm_name], args, bodies[profile])
        runs.append(asdict(record))
        logger.info(
            "round %d %-12s %-9s %7.1f req/s  p50 %7.1f  p99 %7.1f ms  "
            "server %.1f cores %.1f ms/req  client %.2f cores  errors %d",
            round_number, profile, arm_name, record.throughput_rps, record.latency_ms["p50"],
            record.latency_ms["p99"], record.server_cpu["cores_busy"],
            record.server_cpu["cpu_ms_per_request"], record.client_cpu["cores_busy"],
            record.errors,
        )  # fmt: skip
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")  # partial results survive

    report["summary"] = summarize(runs)
    report["finished_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    logger.info("wrote %s\n%s", args.output, markdown_table(report["summary"]))
    return 1 if any(run["errors"] for run in runs) else 0


if __name__ == "__main__":
    sys.exit(main())
