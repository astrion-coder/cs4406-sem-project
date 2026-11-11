"""RQ4 driver: expand configs/recovery_policies.yaml into adapter runs and execute them serially.

One `Run` is one adapter process: an arm at one cell, with one fault, policy and fault rate. Runs are
timing runs, so they never overlap. Each goes through `caffeinate -dims` and `memcap.run` under a
`hygiene.Watch`, and appends one line to `manifest.jsonl` whether it succeeds or not. Rerunning with
the same --run-id skips the Runs the manifest calls complete. Output files are never edited or
deleted: a failed attempt's partial output is renamed `<name>.failed-<k>.jsonl`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

import yaml

from cs4406_sem_project.harness import hygiene, memcap

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "recovery_policies.yaml"
DEFAULT_ADAPTER_DIR = REPO_ROOT / "build" / "adapters"
DEFAULT_RAW_DIR = REPO_ROOT / "results" / "raw"

Kind = Literal["trials", "offline", "sweep"]
KINDS: tuple[Kind, ...] = ("trials", "offline", "sweep")
BINARY = {"S0-go": "simplepir-adapter", "S0-cpp": "vsp-adapter", "S1": "vsp-adapter",
          "S2": "vsp-adapter", "S3": "apir-adapter", "S4": "apir-adapter",
          "M0": "apir-adapter", "M1": "apir-adapter"}
# A policy makes at most 3 further attempts after the first rejection (adapters README, "Recovery").
MAX_ATTEMPTS = 4
RE_PREPROCESS_ATTEMPTS = MAX_ATTEMPTS - 1


@dataclass(frozen=True)
class Run:
    arm: str
    db_bytes: int
    record_bytes: int
    kind: Kind
    fault: str
    policy: str
    fault_prob: float
    replicas_cheat: bool
    phase: Literal["online", "offline"]
    reps: int
    warmup: int

    @property
    def name(self) -> str:
        prob = f"{self.fault_prob:g}".replace(".", "_")
        cheat = "-cheat" if self.replicas_cheat else ""
        return (f"{self.kind}-{self.arm}-db{self.db_bytes}-rb{self.record_bytes}-"
                f"{self.fault}-{self.policy}-p{prob}{cheat}")

    @property
    def binary(self) -> str:
        return BINARY[self.arm]

    @property
    def expected_records(self) -> int:
        return self.reps + self.warmup

    def argv(self, adapter_dir: Path, seed: int, out: Path) -> list[str]:
        argv = [str(adapter_dir / self.binary), "--arm", self.arm, "--db-bytes", str(self.db_bytes),
                "--record-bytes", str(self.record_bytes), "--phase", self.phase,
                "--reps", str(self.reps), "--warmup", str(self.warmup), "--seed", str(seed),
                "--fault", self.fault, "--fault-prob", f"{self.fault_prob:g}",
                "--recovery", self.policy]
        if self.replicas_cheat:
            argv.append("--replicas-cheat")
        return argv + ["--out", str(out)]


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    unknown = {a for a in cfg["policies"]} - set(BINARY)
    unknown |= {a for arms in cfg["faults"]["offline"].values() for a in arms} - set(BINARY)
    if unknown:
        raise ValueError(f"{path}: arms with no adapter binary: {sorted(unknown)}")
    if "retry_same" in {p for ps in cfg["policies"].values() for p in ps}:
        raise ValueError(f"{path}: retry_same is never allowed (decision 12)")
    return cfg


def _cells(arm: str, cfg: dict, db_sizes: Sequence[int] | None,
           record_sizes: Sequence[int] | None) -> list[tuple[int, int]]:
    focus = cfg["focus_cells"]
    cap = cfg.get("max_db_bytes", {}).get(arm)
    dbs = [d for d in (db_sizes or focus["db_bytes"]) if cap is None or d <= cap]
    records = [0] if arm in cfg["one_bit_arms"] else list(record_sizes or focus["record_bytes"])
    return [(d, r) for d in dbs for r in records]


def plan(cfg: dict, *, only: Sequence[str] | None = None, kinds: Sequence[str] = KINDS,
         trials: int | None = None, queries: int | None = None,
         db_sizes: Sequence[int] | None = None, record_sizes: Sequence[int] | None = None) -> list[Run]:
    trials = cfg["trials"] if trials is None else trials
    queries = cfg["cheating"]["queries"] if queries is None else queries
    warmup = cfg["warmup"]
    arms = [a for a in cfg["policies"] if only is None or a in only]
    runs: list[Run] = []
    for kind in (k for k in KINDS if k in kinds):
        for arm in arms:
            for db, rb in _cells(arm, cfg, db_sizes, record_sizes):
                cell = dict(arm=arm, db_bytes=db, record_bytes=rb, kind=kind, warmup=warmup)
                if kind == "trials":
                    for fault in cfg["faults"]["online"]:
                        for policy in cfg["policies"][arm]:
                            runs.append(Run(**cell, fault=fault, policy=policy, fault_prob=1.0,
                                            replicas_cheat=False, phase="online", reps=trials))
                elif kind == "offline":
                    for fault, fault_arms in cfg["faults"]["offline"].items():
                        if arm in fault_arms:
                            runs.append(Run(**cell, fault=fault, policy="abort", fault_prob=1.0,
                                            replicas_cheat=False, phase="offline", reps=trials))
                else:
                    for policy in cfg["policies"][arm]:
                        for rate in cfg["cheating"]["rates"]:
                            runs.append(Run(**cell, fault=cfg["cheating"]["fault"], policy=policy,
                                            fault_prob=float(rate),
                                            replicas_cheat=policy == "switch_replica",
                                            phase="online", reps=queries))
    names = [r.name for r in runs]
    if len(set(names)) != len(names):
        raise ValueError("the plan has two Runs with the same name")
    return runs


# ---------------------------------------------------------------- dry run

def load_estimates(path: Path) -> dict[tuple[str, int], dict[int, dict]]:
    """Rows of a notebook-07 estimates.jsonl that have both times, by (arm, db_bytes) then record_bytes."""
    table: dict[tuple[str, int], dict[int, dict]] = {}
    for line in Path(path).read_text().splitlines():
        row = json.loads(line)
        if row.get("offline_s") is not None and row.get("query_s") is not None:
            table.setdefault((row["arm"], row["db_bytes"]), {})[row["record_bytes"]] = row
    return table


def estimate_run_s(run: Run, table) -> tuple[float, str] | None:
    """Upper bound on one Run's seconds and how its times were found, or None with no times for its cell."""
    sizes = table.get((run.arm, run.db_bytes))
    if not sizes:
        return None
    if run.record_bytes in sizes:
        row, how = sizes[run.record_bytes], "measured"
    else:
        rb = max(sizes)
        row, how = sizes[rb], f"larger measured record size {rb} B"
    off, q, n = row["offline_s"], row["query_s"], run.expected_records
    if run.phase == "offline":
        return n * off, how
    attempts = 1 if run.policy == "abort" else MAX_ATTEMPTS
    reprep = RE_PREPROCESS_ATTEMPTS * off if run.policy == "re_preprocess" else 0
    return off + n * (attempts * q + reprep), how


FORMULA = """upper bound per Run, from one measured offline_s and query_s per cell:
  offline Run:             (reps + warmup) * offline_s
  online Run, abort:       offline_s + (reps + warmup) * query_s
  online Run, other policy: offline_s + (reps + warmup) * (4 * query_s)
  online Run, re_preprocess: offline_s + (reps + warmup) * (4 * query_s + 3 * offline_s)
(4 = the first attempt plus up to 3 more; every rep is assumed faulted and every attempt to be
rejected. Not counted: database generation and the stale server that stale_record builds, both
untimed in the adapter. A cell with no measured record size at its database size is left out.)"""


def dry_run(runs: list[Run], estimates: Path | None, out=sys.stdout) -> None:
    p = lambda s="": print(s, file=out)
    p(f"{len(runs)} Runs, {sum(r.expected_records for r in runs)} adapter repetitions "
      "(reps + warmup per Run, summed)")
    p(f"{'kind':8s} {'arm':7s} {'runs':>6s} {'adapter reps':>13s}")
    for kind in KINDS:
        for arm in BINARY:
            sel = [r for r in runs if r.kind == kind and r.arm == arm]
            if sel:
                p(f"{kind:8s} {arm:7s} {len(sel):6d} {sum(r.expected_records for r in sel):13d}")
        sel = [r for r in runs if r.kind == kind]
        if sel:
            p(f"{kind:8s} {'total':7s} {len(sel):6d} {sum(r.expected_records for r in sel):13d}")
    if estimates is None:
        return
    table = load_estimates(estimates)
    p()
    p(FORMULA)
    seconds = {k: 0.0 for k in KINDS}
    missing, borrowed = [], set()
    for r in runs:
        est = estimate_run_s(r, table)
        if est is None:
            missing.append((r.arm, r.db_bytes, r.record_bytes))
            continue
        seconds[r.kind] += est[0]
        if est[1] != "measured":
            borrowed.add((r.arm, r.db_bytes, r.record_bytes, est[1]))
    p()
    for kind in KINDS:
        if any(r.kind == kind for r in runs):
            p(f"{kind:8s} upper bound {seconds[kind] / 3600:9.1f} h")
    p(f"{'total':8s} upper bound {sum(seconds.values()) / 3600:9.1f} h")
    for arm, db, rb, how in sorted(borrowed):
        p(f"  {arm} db {db} record {rb}: times taken from the {how}")
    for cell in sorted(set(missing)):
        p(f"  no estimate for {cell}; left out of the bounds")


# ---------------------------------------------------------------- execution

@dataclass(frozen=True)
class Exec:
    adapter_dir: Path
    out_dir: Path
    seed: int
    timeout_s: float
    cap_bytes: int
    env: dict
    allow_unclean: bool


def _set_aside(out_dir: Path, name: str) -> None:
    """Rename a previous attempt's files to <name>.failed-<k>.*, using one k for both."""
    jsonl, err = out_dir / f"{name}.jsonl", out_dir / f"{name}.stderr.log"
    if not (jsonl.exists() or err.exists()):
        return
    k = 1
    while (out_dir / f"{name}.failed-{k}.jsonl").exists() or (out_dir / f"{name}.failed-{k}.stderr.log").exists():
        k += 1
    if jsonl.exists():
        jsonl.rename(out_dir / f"{name}.failed-{k}.jsonl")
    if err.exists():
        err.rename(out_dir / f"{name}.failed-{k}.stderr.log")


def _count_records(path: Path) -> int:
    return sum(1 for line in path.read_text().splitlines() if line.strip()) if path.exists() else 0


def execute(run: Run, ex: Exec) -> dict:
    """Run one adapter process and return its manifest line. Never raises for the adapter's own failure."""
    _set_aside(ex.out_dir, run.name)
    out, err_log = ex.out_dir / f"{run.name}.jsonl", ex.out_dir / f"{run.name}.stderr.log"
    argv = ["caffeinate", "-dims", *run.argv(ex.adapter_dir, ex.seed, out)]
    row = {"run": asdict(run), "name": run.name, "argv": argv, "exit_code": None, "timed_out": False,
           "over_cap": False, "peak_footprint_bytes": None,
           "started_utc": datetime.now(timezone.utc).isoformat()}
    with hygiene.Watch() as watch:
        try:
            done = memcap.run(argv, timeout_s=ex.timeout_s, cap_bytes=ex.cap_bytes, env=ex.env)
            row.update(exit_code=done.returncode, over_cap=done.over_cap,
                       peak_footprint_bytes=done.peak_footprint_bytes)
            err_log.write_text(done.stderr)
        except subprocess.TimeoutExpired:
            row["timed_out"] = True
    row.update(wall_s=watch.wall_s, monotonic_s=watch.mono_s, hygiene_problems=watch.problems,
               records=_count_records(out))
    ran_ok = (row["exit_code"] == 0 and not row["timed_out"] and not row["over_cap"]
              and row["records"] == run.expected_records)
    row["outcome"] = ("ok" if ran_ok else "oom" if row["over_cap"] else "timeout" if row["timed_out"]
                      else "failed")
    clean = ex.allow_unclean or not watch.problems
    row["complete"] = ran_ok and clean
    # A cell that needs more memory than the cap or more time than the limit is a result to record
    # (spec 5: status "oom"), not a fault to retry: rerunning it would cost the same and end the same.
    row["settled"] = clean and row["outcome"] != "failed"
    row["citable"] = ran_ok and not watch.problems and not ex.allow_unclean
    return row


def settled_names(manifest: Path) -> set[str]:
    """Runs a resume skips: complete, or ended over the memory cap or the time limit under clean
    conditions. Retrying an oom or timeout cell needs a new --run-id."""
    if not manifest.exists():
        return set()
    rows = (json.loads(line) for line in manifest.read_text().splitlines() if line.strip())
    return {r["name"] for r in rows if r.get("settled", r["complete"])}


def run_all(runs: list[Run], ex: Exec, log=print) -> list[dict]:
    """Execute each Run not yet complete in the manifest, serially. Stops if the machine's state is bad."""
    ex.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = ex.out_dir / "manifest.jsonl"
    done = settled_names(manifest)
    attempts = []
    for i, run in enumerate(runs, 1):
        if run.name in done:
            log(f"[{i}/{len(runs)}] skip (settled) {run.name}")
            continue
        row = execute(run, ex)
        with manifest.open("a") as f:
            f.write(json.dumps(row) + "\n")
        attempts.append(row)
        state = "ok" if row["complete"] else (
            "TIMED OUT" if row["timed_out"] else "OVER CAP" if row["over_cap"]
            else f"FAILED exit {row['exit_code']}, {row['records']}/{run.expected_records} records")
        log(f"[{i}/{len(runs)}] {state} {run.name} ({row['wall_s']:.0f} s) {row['hygiene_problems'] or ''}")
        if row["hygiene_problems"] and not ex.allow_unclean:
            log("stopping: the measurement conditions broke. Fix them and rerun with the same --run-id.")
            break
    return attempts


# ---------------------------------------------------------------- CLI

def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()


def write_meta(out_dir: Path, run_id: str, config_path: Path, ex: Exec, binaries: set[str]) -> None:
    from cs4406_sem_project.harness import sysinfo
    path = out_dir / "meta.json"
    if path.exists():
        if json.loads(path.read_text())["citable"] == ex.allow_unclean:
            raise SystemExit(f"{path} was written by a run with allow_unclean="
                             f"{not ex.allow_unclean}; use a new --run-id")
        return
    meta = {"run_id": run_id, "git_sha": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain", "src", "Makefile", "configs")),
            "citable": not ex.allow_unclean, "seed": ex.seed,
            "timeout_s": ex.timeout_s, "memory_cap_bytes": ex.cap_bytes,
            "config_path": str(config_path), "config": config_path.read_text(),
            "adapters": {b: hashlib.sha256((ex.adapter_dir / b).read_bytes()).hexdigest()
                         for b in sorted(binaries)},
            "sysinfo": sysinfo.snapshot()}
    path.write_text(json.dumps(meta, indent=1, default=str))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--run-id", help="default: rq4-<UTC time>. Reuse an id to resume.")
    ap.add_argument("--only", nargs="+", metavar="ARM")
    ap.add_argument("--kinds", default=",".join(KINDS), help="comma-separated subset of trials,offline,sweep")
    ap.add_argument("--adapter-dir", type=Path, default=DEFAULT_ADAPTER_DIR)
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help="runs go in <raw-dir>/<run-id>/")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--estimates", type=Path, help="notebook 07 estimates.jsonl, for --dry-run bounds")
    ap.add_argument("--allow-unclean", action="store_true",
                    help="skip the AC and lid check and ignore hygiene problems (smoke runs; not citable)")
    ap.add_argument("--trials", type=int)
    ap.add_argument("--queries", type=int)
    ap.add_argument("--db-bytes", type=int, nargs="+")
    ap.add_argument("--record-bytes", type=int, nargs="+")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kinds = args.kinds.split(",")
    if bad := set(kinds) - set(KINDS):
        raise SystemExit(f"unknown --kinds {sorted(bad)}")
    cfg = load_config(args.config)
    if args.only and (bad := set(args.only) - set(cfg["policies"])):
        raise SystemExit(f"unknown --only arms {sorted(bad)}")
    runs = plan(cfg, only=args.only, kinds=kinds, trials=args.trials, queries=args.queries,
                db_sizes=args.db_bytes, record_sizes=args.record_bytes)
    if args.dry_run:
        dry_run(runs, args.estimates)
        return 0
    binaries = {r.binary for r in runs}
    if missing := [b for b in sorted(binaries) if not (args.adapter_dir / b).is_file()]:
        raise SystemExit(f"missing adapter binaries in {args.adapter_dir}: {missing}")
    if not args.allow_unclean:
        if hygiene.power_source() != "AC Power":
            raise SystemExit(f"plug the Mac in first ({hygiene.power_source()})")
        if hygiene.lid_closed() is not False:
            raise SystemExit("open the lid first: a closed lid sleeps the Mac despite caffeinate")
    run_id = args.run_id or "rq4-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.raw_dir / run_id
    env = {**os.environ, "VPIR_RUN_ID": run_id, "VPIR_GIT_SHA": _git("rev-parse", "HEAD")}
    ex = Exec(args.adapter_dir, out_dir, cfg["seed"], cfg["timeout_s"],
              int(cfg["memory_cap_gib"] * memcap.GIB), env, args.allow_unclean)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_meta(out_dir, run_id, args.config, ex, binaries)
    print(f"run {run_id}: {len(runs)} Runs into {out_dir}" + (" (not citable)" if args.allow_unclean else ""))
    t0 = time.monotonic()
    run_all(runs, ex)
    left = [r.name for r in runs if r.name not in settled_names(out_dir / "manifest.jsonl")]
    print(f"{len(runs) - len(left)} of {len(runs)} Runs settled (complete, oom or timeout) "
          f"in {time.monotonic() - t0:.0f} s")
    for name in left:
        print(f"  not settled, rerun with the same --run-id: {name}")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
