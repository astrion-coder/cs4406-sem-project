"""Run the upstream repos' own test suites and record logs plus a SUMMARY.md (gate G0)."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
TIMEOUT_S = 60 * 60


@dataclass(frozen=True)
class Suite:
    name: str
    cwd: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: int = TIMEOUT_S


class Status(Enum):
    passed = "passed"
    failed = "failed"
    timed_out = "timed_out"
    not_built = "not_built"


@dataclass(frozen=True)
class Result:
    suite: Suite
    status: Status
    exit_code: int | None
    wall_s: float
    log_path: Path


SUITES: tuple[Suite, ...] = (
    Suite("simplepir", "third_party/simplepir/pir", ("go", "test", "-count=1", "-timeout", "60m")),
    # TestReal* (key_test.go) read the SKS PGP key dump the apir-code README downloads separately
    # into data/sks/. They test the Keyd demo application, not the benchmark arms.
    Suite("apir-code", "third_party/apir-code", ("go", "test", "-count=1", "-timeout", "60m", "-skip", "^TestReal", ".")),
    Suite(
        "verisimplepir",
        "third_party/verisimplepir",
        ("./bin/demo/test/pir_test",),
        {"DYLD_LIBRARY_PATH": str(REPO_ROOT / "third_party/verisimplepir/bin/lib")},
    ),
)


def _is_built(suite: Suite, repo_root: Path) -> bool:
    cwd = repo_root / suite.cwd
    if not cwd.is_dir():
        return False
    # `go` and friends resolve through PATH; only a path-like entry point can be missing.
    entry = suite.argv[1] if suite.argv[0] == "bash" else suite.argv[0]
    return "/" not in entry or (cwd / entry).exists()


def _execute(suite: Suite, repo_root: Path, log) -> tuple[Status, int | None]:
    env = {**os.environ, **suite.env}
    try:
        proc = subprocess.Popen(
            suite.argv,
            cwd=repo_root / suite.cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError:
        return Status.not_built, None
    try:
        code = proc.wait(timeout=suite.timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return Status.timed_out, None
    return (Status.passed if code == 0 else Status.failed), code


def _header(suite: Suite) -> str:
    env = "".join(f"  {k}={v}\n" for k, v in suite.env.items()) or "  (none)\n"
    return f"argv: {' '.join(suite.argv)}\ncwd: {suite.cwd}\nenv overrides:\n{env}\n"


def _summary(results: Sequence[Result]) -> str:
    rows = [
        f"| {r.suite.name} | {r.status.value} | {'' if r.exit_code is None else r.exit_code} "
        f"| {r.wall_s:.1f}s | {r.log_path} |"
        for r in results
    ]
    head = "| suite | status | exit code | wall time | log |\n|---|---|---|---|---|\n"
    return "# Upstream test summary\n\n" + head + "\n".join(rows) + "\n"


def run(suites: Sequence[Suite], repo_root: Path, logs_root: Path) -> list[Result]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = logs_root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    for suite in suites:
        log_path = out_dir / f"{suite.name}.log"
        start = time.monotonic()
        with log_path.open("w") as log:
            log.write(_header(suite))
            log.flush()
            if _is_built(suite, repo_root):
                status, code = _execute(suite, repo_root, log)
            else:
                status, code = Status.not_built, None
            if status is Status.not_built:
                log.write("not built: cwd or entry point is missing\n")
        result = Result(suite, status, code, time.monotonic() - start, log_path)
        results.append(result)
        print(f"{suite.name}: {status.value} (exit {code}, {result.wall_s:.1f}s) {log_path}", flush=True)
    (out_dir / "SUMMARY.md").write_text(_summary(results))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", metavar="NAME", help="run only these suites")
    parser.add_argument("--logs-root", type=Path, default=Path("logs/p0"))
    args = parser.parse_args(argv)
    suites = SUITES
    if args.only:
        known = {s.name for s in SUITES}
        unknown = [n for n in args.only if n not in known]
        if unknown:
            parser.error(f"unknown suite(s) {unknown}; known: {sorted(known)}")
        suites = tuple(s for s in SUITES if s.name in args.only)
    logs_root = args.logs_root if args.logs_root.is_absolute() else REPO_ROOT / args.logs_root
    results = run(suites, REPO_ROOT, logs_root)
    return 0 if all(r.status is Status.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
