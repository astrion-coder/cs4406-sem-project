"""Run a grid tier: one fresh adapter process per (cell, phase), resumable, with timeouts and a memory cap.

    uv run python -m cs4406_sem_project.harness.run_grid --tier tier1 [--dry-run]
    uv run python -m cs4406_sem_project.harness.run_grid --resume <run_id>

Layout of results/raw/<run_id>/:
    plan.json             the tier's cells, fixed at the first start; a resume must expand to the same
    meta.json             harness SHA, platform, start time, tier
    <cell_id>.jsonl       final rows of a finished (cell, phase); its presence is what "done" means
    ledger.jsonl          one line per attempt: status, exit code, peak footprint, hygiene
    rejected/             rows of attempts that broke measurement hygiene, kept as evidence

The adapter writes <cell_id>.jsonl.part. Only after its rows validate and hygiene passed is the file
renamed into place, so killing the runner at any point leaves each cell either done or to be rerun.
A cell that ran out of memory, timed out or crashed is a result (spec 9): its final file holds any
rows the adapter wrote plus one harness_failure row.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform as py_platform
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import validate
from cs4406_sem_project.harness.grid import Cell

ROOT = gridmod.ROOT
RESULTS = ROOT / "results" / "raw"
ADAPTER_DIR = ROOT / "build" / "adapters"
REFERENCE_PLATFORM = ROOT / "env" / "reference_platform.txt"
GIB = 1 << 30
STDERR_TAIL = 2000

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_HYGIENE = 0, 1, 2, 3


@dataclass(frozen=True)
class Finished:
    """What one adapter process did."""

    returncode: int | None
    stderr: str
    peak_footprint_bytes: int | None
    over_cap: bool
    timed_out: bool


Execute = Callable[[list[str], float, int, dict], Finished]


class Watch(Protocol):
    def __enter__(self) -> "Watch": ...
    def __exit__(self, *exc) -> None: ...
    @property
    def problems(self) -> list[str]: ...
    def record(self) -> dict: ...


class HygieneError(RuntimeError):
    """A timing run broke spec 7's conditions. Every later cell would too, so the run stops."""


class ContractError(RuntimeError):
    """An adapter wrote rows the schema or the plan does not allow. A bug to fix, not a result."""


def _memcap_execute(argv, timeout_s, cap_bytes, env) -> Finished:
    from cs4406_sem_project.harness import memcap  # macOS only: loads libproc.dylib

    try:
        f = memcap.run(argv, timeout_s=timeout_s, cap_bytes=cap_bytes, env=env)
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return Finished(None, err, None, False, True)
    return Finished(f.returncode, f.stderr, f.peak_footprint_bytes, f.over_cap, False)


def _psutil_execute(argv, timeout_s, cap_bytes, env) -> Finished:
    """Development fallback off macOS: peak RSS of the process tree, no compressed memory."""
    import tempfile

    import psutil

    with tempfile.TemporaryFile("w+") as err:
        child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err, text=True, env=env)
        proc, peak, deadline = psutil.Process(child.pid), 0, time.monotonic() + timeout_s
        timed_out = over = False
        while child.poll() is None:
            try:
                tree = [proc, *proc.children(recursive=True)]
                peak = max(peak, sum(p.memory_info().rss for p in tree))
            except psutil.NoSuchProcess:
                tree = []
            over = peak > cap_bytes
            timed_out = not over and time.monotonic() > deadline
            if over or timed_out:
                for p in reversed(tree):
                    try:
                        p.kill()
                    except psutil.NoSuchProcess:
                        pass
                break
            time.sleep(0.05)
        child.wait()
        err.seek(0)
        return Finished(None if timed_out else child.returncode, err.read(), peak, over, timed_out)


@dataclass
class _NoWatch:
    """Off macOS there is no pmset or clamshell state to read; the platform check refuses those rows."""

    problems: list[str] = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def record(self) -> dict:
        return {"checked": False}


def default_execute() -> Execute:
    return _memcap_execute if sys.platform == "darwin" else _psutil_execute


def default_watch() -> Callable[[], Watch]:
    if sys.platform == "darwin":
        from cs4406_sem_project.harness.hygiene import Watch as HygieneWatch

        return HygieneWatch
    return _NoWatch


def current_platform() -> str:
    if sys.platform == "darwin":
        from cs4406_sem_project.harness.sysinfo import platform_id

        return platform_id()
    return f"{py_platform.machine()}-{sys.platform}-{py_platform.processor() or 'unknown'}"


def git_sha() -> str | None:
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "src", "configs"],
                               capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return sha + ("-dirty" if dirty else "")


def split_command(text: str) -> list[str]:
    """shlex.split, except that on Windows backslashes stay literal and quotes are removed."""
    if os.name != "nt":
        return shlex.split(text)
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t for t in shlex.split(text, posix=False)]


def new_run_id(tier: str) -> str:
    return f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}_{tier}_{secrets.token_hex(3)}"


@dataclass
class Runner:
    cells: list[Cell]
    run_dir: Path
    run_id: str
    platform: str
    reference_platform: str
    adapter_dir: Path
    mem_cap_bytes: int
    harness_sha: str | None = None
    launcher: list[str] = field(default_factory=list)  # prefix for every adapter command
    execute: Execute = field(default_factory=default_execute)
    watch: Callable[[], Watch] = field(default_factory=default_watch)
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True)
    program: str = "run_grid"  # the module a live lock holder runs

    def final_path(self, c: Cell) -> Path:
        return self.run_dir / f"{c.cell_id}.jsonl"

    def pending(self) -> list[Cell]:
        return [c for c in self.cells if not self.final_path(c).exists()]

    def adapter_args(self, c: Cell, out: Path) -> list[str]:
        return c.adapter_args(out)

    def argv(self, c: Cell, out: Path) -> list[str]:
        cmd = [*self.launcher, str(self.adapter_dir / c.binary), *self.adapter_args(c, out)]
        if sys.platform == "darwin":
            cmd = ["caffeinate", "-dims", *cmd]  # spec 7: no sleep during a timing run
        return cmd

    def env(self) -> dict:
        return {**os.environ, "VPIR_RUN_ID": self.run_id, "VPIR_GIT_SHA": self.harness_sha or "",
                "VPIR_PLATFORM": self.platform}

    def _ledger(self, entry: dict) -> None:
        with (self.run_dir / "ledger.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def _failure_row(self, c: Cell, status: str, error: str, fin: Finished, wall_s: float, partial: int) -> dict:
        return {
            "run_id": self.run_id, "git_sha_harness": self.harness_sha, "platform": self.platform,
            "adapter": validate.ADAPTER_OF[c.arm], "arm": c.arm, "cell_id": c.cell_id,
            "db_bytes": c.db_bytes, "record_bytes": c.record_bytes, "phase": c.phase, "seed": c.seed,
            "fault": "none", "fault_prob": 0.0, "recovery": "abort", "reps": c.reps, "warmup": c.warmup,
            "status": status, "error": error, "partial_rows": partial,
            "harness": {"exit_code": fin.returncode, "wall_s": wall_s, "peak_footprint_bytes": fin.peak_footprint_bytes,
                        "timeout_s": c.timeout_s, "mem_cap_bytes": self.mem_cap_bytes,
                        "stderr_tail": fin.stderr[-STDERR_TAIL:]},
        }

    def _read_rows(self, c: Cell, part: Path) -> list[dict]:
        if not part.exists():
            return []
        lines = [ln for ln in part.read_text().splitlines() if ln.strip()]
        problems = [p for p in validate.check_lines(lines, reference_platform=self.reference_platform)
                    if p.severity == "error"]
        if problems:
            raise ContractError(f"{c.cell_id}: adapter rows fail validation: {problems[0]}")
        rows = [json.loads(ln) for ln in lines]
        for r in rows:
            mismatch = {k: (r[k], v) for k, v in (("arm", c.arm), ("db_bytes", c.db_bytes),
                        ("record_bytes", c.record_bytes), ("phase", c.phase), ("seed", c.seed),
                        ("run_id", self.run_id)) if r[k] != v}
            if mismatch:
                raise ContractError(f"{c.cell_id}: row does not match its cell: {mismatch}")
        return rows

    def run_cell(self, c: Cell) -> str:
        part = self.final_path(c).with_suffix(".jsonl.part")
        part.unlink(missing_ok=True)
        t0 = time.monotonic()
        with self.watch() as w:
            fin = self.execute(self.argv(c, part), c.timeout_s, self.mem_cap_bytes, self.env())
        wall_s = time.monotonic() - t0
        entry = {"cell_id": c.cell_id, "at": dt.datetime.now(dt.timezone.utc).isoformat(), "wall_s": wall_s,
                 "exit_code": fin.returncode, "peak_footprint_bytes": fin.peak_footprint_bytes,
                 "hygiene": w.record()}
        if w.problems:
            rejected = self.run_dir / "rejected"
            rejected.mkdir(exist_ok=True)
            if part.exists():
                part.rename(rejected / f"{c.cell_id}.{int(time.time())}.jsonl")
            self._ledger({**entry, "status": "hygiene_failed"})
            raise HygieneError(f"{c.cell_id}: {'; '.join(w.problems)}")

        rows = self._read_rows(c, part)
        unsupported = bool(rows) and all(r["status"] == "unsupported_params" for r in rows)
        status, error = ("unsupported_params" if unsupported else "ok"), None
        if fin.over_cap:
            status, error = "oom", f"memory footprint passed the {self.mem_cap_bytes / GIB:g} GiB cap"
        elif fin.timed_out:
            status, error = "timeout", f"no exit within {c.timeout_s:g} s"
        elif fin.returncode != 0:
            status, error = "crash", f"adapter exited {fin.returncode}"
        elif not unsupported and len(rows) != c.expected_rows:
            status, error = "crash", f"adapter exited 0 but wrote {len(rows)} of {c.expected_rows} rows"
        if error is not None:
            rows.append(self._failure_row(c, status, error, fin, wall_s, len(rows)))

        with part.open("w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        part.replace(self.final_path(c))
        self._ledger({**entry, "status": status, "rows": len(rows), "error": error})
        return status

    def _take_lock(self) -> None:
        """One runner per run directory, and no adapter left over from a killed runner.

        A killed runner's adapter keeps running and would compete with the resumed run's timings, so
        any process whose command line points into this run directory is killed first.
        """
        import psutil

        lock = self.run_dir / "runner.pid"
        if lock.exists():
            pid = int(lock.read_text() or 0)
            if pid != os.getpid() and psutil.pid_exists(pid):
                try:
                    alive = self.program in " ".join(psutil.Process(pid).cmdline())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    alive = False
                if alive:
                    raise ContractError(f"runner {pid} is still working on {self.run_dir}")
        lock.write_text(str(os.getpid()))
        marker = str(self.run_dir)
        for p in psutil.process_iter(["pid", "cmdline"]):
            if p.info["pid"] != os.getpid() and any(marker in a for a in p.info["cmdline"] or ()):
                self.log(f"killing leftover adapter {p.info['pid']} from an earlier runner")
                try:
                    p.kill()
                    p.wait(10)
                except psutil.Error:
                    pass

    def run(self) -> dict[str, int]:
        if self.platform != self.reference_platform:
            raise ContractError(f"this machine is {self.platform!r}; reported numbers come only from "
                                f"{self.reference_platform!r} (spec 7)")
        self._take_lock()
        todo = self.pending()
        self.log(f"{self.run_id}: {len(self.cells) - len(todo)} of {len(self.cells)} done, {len(todo)} to run")
        counts: dict[str, int] = {}
        for i, c in enumerate(todo, 1):
            self.log(f"[{i}/{len(todo)}] {c.cell_id} (timeout {c.timeout_s:g} s)")
            status = self.run_cell(c)
            counts[status] = counts.get(status, 0) + 1
            self.log(f"    {status}")
        return counts


def write_plan(run_dir: Path, cells: list[Cell], meta: dict) -> None:
    """Fix the plan at the first start. A resume must expand to the same cells."""
    plan_path = run_dir / "plan.json"
    plan = [c.to_dict() for c in cells]
    if plan_path.exists():
        old = json.loads(plan_path.read_text())
        if old != plan:
            raise ContractError(f"{plan_path} differs from the grid now; start a new run instead of resuming")
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=1) + "\n")
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")


def dry_run(cells: list[Cell], done: set[str]) -> str:
    todo = [c for c in cells if c.cell_id not in done]
    lines = [f"{'cell':<34} {'reps':>4} {'timeout':>8}"]
    lines += [f"{c.cell_id:<34} {c.warmup}+{c.reps:<2} {c.timeout_s / 60:>6.0f} m" for c in todo]
    bound_h = sum(c.timeout_s for c in todo) / 3600
    by_arm: dict[str, int] = {}
    for c in todo:
        by_arm[c.arm] = by_arm.get(c.arm, 0) + 1
    lines.append(f"{len(todo)} (cell, phase) runs to do, {len(cells) - len(todo)} done; per arm {by_arm}")
    lines.append(f"upper bound {bound_h:.1f} h (every run hitting its timeout); the smoke run gives a real estimate")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--grid", type=Path, default=gridmod.GRID_PATH)
    p.add_argument("--tier", default="tier1")
    p.add_argument("--resume", metavar="RUN_ID", help="continue results/raw/RUN_ID")
    p.add_argument("--run-id", help="name a new run of --tier, or continue it if results/raw/RUN_ID exists")
    p.add_argument("--dry-run", action="store_true", help="print the plan and an upper time bound")
    p.add_argument("--results", type=Path, default=RESULTS)
    p.add_argument("--adapter-dir", type=Path, default=ADAPTER_DIR)
    p.add_argument("--reference-platform", type=Path, default=REFERENCE_PLATFORM)
    p.add_argument("--launcher", default="", help="command prefix for every adapter run (tests use it)")
    args = p.parse_args(argv)

    grid = gridmod.load(args.grid)
    named = args.run_id and (args.results / args.run_id / "meta.json").exists()
    if args.resume or named:
        run_id = args.resume or args.run_id
        tier = json.loads((args.results / run_id / "meta.json").read_text())["tier"]
        if named and tier != args.tier:
            print(f"{run_id} is a {tier} run, not {args.tier}", file=sys.stderr)
            return EXIT_USAGE
    else:
        run_id, tier = args.run_id or new_run_id(args.tier), args.tier
    cells = gridmod.expand(grid, tier)
    run_dir = args.results / run_id

    if args.dry_run:
        done = {c.cell_id for c in cells if (run_dir / f"{c.cell_id}.jsonl").exists()}
        print(dry_run(cells, done))
        return EXIT_OK

    if not args.reference_platform.exists():
        print(f"{args.reference_platform} is missing; run `make sysinfo` on the reference Mac", file=sys.stderr)
        return EXIT_USAGE
    runner = Runner(
        cells=cells, run_dir=run_dir, run_id=run_id, platform=current_platform(),
        reference_platform=validate.read_reference_platform(args.reference_platform),
        adapter_dir=args.adapter_dir, mem_cap_bytes=int(grid["mem_cap_gib"] * GIB), harness_sha=git_sha(),
        launcher=split_command(args.launcher),
    )
    try:
        write_plan(run_dir, cells, {"run_id": run_id, "tier": tier, "git_sha_harness": runner.harness_sha,
                                    "platform": runner.platform,
                                    "started": dt.datetime.now(dt.timezone.utc).isoformat()})
        counts = runner.run()
    except HygieneError as e:
        print(f"stopped, measurement hygiene broken: {e}\nfix it, then: --resume {run_id}", file=sys.stderr)
        return EXIT_HYGIENE
    except ContractError as e:
        print(f"stopped: {e}", file=sys.stderr)
        return EXIT_ERROR
    print(f"{run_id}: {counts}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
