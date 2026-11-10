"""Run an adapter test suite (G1 conformance or P7 recovery) in parallel partitions, with logs and a SUMMARY.md."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Partition:
    name: str
    select: str


@dataclass(frozen=True)
class Suite:
    title: str
    test_file: str
    logs_root: str
    partitions: tuple[Partition, ...]  # must cover every collected test exactly once


SUITES = {
    # S4 answers each query in seconds and builds its digest in minutes, so its three 100-query
    # runs get one process each.
    "g1": Suite("conformance", "tests/test_adapters.py", "logs/p1", (
        Partition("s4-honest", "S4 and not flip_answer_bit and not wrong_record"),
        Partition("s4-flip_answer_bit", "S4-flip_answer_bit"),
        Partition("s4-wrong_record", "S4-wrong_record"),
        Partition("vsp", "S0-cpp or S1 or S2"),
        Partition("go-rest", "S0-go or S3 or M0 or M1"),
    )),
    # S1, S2 and S4 each take about an hour alone. The policy test with no arm id runs S1.
    "recovery": Suite("recovery", "tests/test_recovery.py", "logs/p7", (
        Partition("s1", "S1 or proof_arms_accept"),
        Partition("s2", "S2"),
        Partition("s4", "S4"),
        Partition("s0", "S0-go or S0-cpp"),
        Partition("go-rest", "S3 or M0 or M1"),
    )),
}


@dataclass(frozen=True)
class Result:
    partition: Partition
    exit_code: int
    tests: int
    failed: int
    skipped: int
    wall_s: float
    log_path: Path

    @property
    def passed(self) -> int:
        return self.tests - self.failed - self.skipped


def _pytest(test_file: str, *args: str) -> list[str]:
    return [sys.executable, "-m", "pytest", test_file, "-p", "no:cacheprovider", *args]


def _collect(test_file: str, select: str | None) -> list[str]:
    argv = _pytest(test_file, "--collect-only", "-q", *(("-k", select) if select else ()))
    out = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if "::" in line]


def check_partitions(suite: Suite) -> int:
    everything = _collect(suite.test_file, None)
    counts = Counter(t for p in suite.partitions for t in _collect(suite.test_file, p.select))
    missing = sorted(set(everything) - set(counts))
    doubled = sorted(t for t, n in counts.items() if n > 1)
    if missing or doubled:
        raise SystemExit(f"partitions do not cover the suite exactly once: missing {missing}, in several {doubled}")
    return len(everything)


def _junit_counts(xml_path: Path) -> tuple[int, int, int]:
    if not xml_path.is_file():
        return 0, 0, 0
    suite = ET.parse(xml_path).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    attr = lambda k: int(suite.get(k, 0))
    return attr("tests"), attr("failures") + attr("errors"), attr("skipped")


def run(suite: Suite, logs_root: Path) -> tuple[list[Result], int]:
    total = check_partitions(suite)
    out_dir = logs_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    running = []
    for p in suite.partitions:
        log_path = out_dir / f"{p.name}.log"
        argv = _pytest(suite.test_file, "-q", "-k", p.select, "--basetemp", str(out_dir / f"{p.name}-tmp"),
                       "--junitxml", str(out_dir / f"{p.name}.xml"))
        log = log_path.open("w")
        proc = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT)
        running.append((p, proc, log, log_path))
    finished: dict[str, Result] = {}
    while len(finished) < len(running):
        for p, proc, log, log_path in running:
            if p.name in finished or proc.poll() is None:
                continue
            log.close()
            tests, failed, skipped = _junit_counts(out_dir / f"{p.name}.xml")
            r = Result(p, proc.returncode, tests, failed, skipped, time.monotonic() - start, log_path)
            finished[p.name] = r
            print(f"{p.name}: {r.passed} of {r.tests} passed (exit {r.exit_code}, {r.wall_s:.0f}s) {log_path}",
                  flush=True)
        time.sleep(1)
    results = [finished[p.name] for p in suite.partitions]
    (out_dir / "SUMMARY.md").write_text(_summary(suite, results, total))
    return results, total


def _summary(suite: Suite, results: Sequence[Result], total: int) -> str:
    rows = [
        f"| {r.partition.name} | `{r.partition.select}` | {r.passed} | {r.failed} | {r.skipped} "
        f"| {r.exit_code} | {r.wall_s:.0f}s | {r.log_path} |"
        for r in results
    ]
    passed = sum(r.passed for r in results)
    head = ("| partition | selection | passed | failed | skipped | exit code | wall time | log |\n"
            "|---|---|---|---|---|---|---|---|\n")
    return (f"# Adapter {suite.title} summary\n\n`{suite.test_file}`: {passed} of {total} tests passed.\n\n"
            + head + "\n".join(rows) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=sorted(SUITES), default="g1")
    parser.add_argument("--logs-root", type=Path, help="default: the suite's own, logs/p1 or logs/p7")
    args = parser.parse_args(argv)
    suite = SUITES[args.suite]
    logs_root = args.logs_root or Path(suite.logs_root)
    logs_root = logs_root if logs_root.is_absolute() else REPO_ROOT / logs_root
    results, total = run(suite, logs_root)
    ok = all(r.exit_code == 0 for r in results) and sum(r.passed for r in results) == total
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
