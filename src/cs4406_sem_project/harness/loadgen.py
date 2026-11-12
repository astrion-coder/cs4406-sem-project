"""RQ6 and RQ7: drive the arm's serve-mode servers with build/loadgen/loadgen at every focus cell (spec 5.1).

    uv run python -m cs4406_sem_project.harness.loadgen --run-id <run_id> [--dry-run]
    uv run python -m cs4406_sem_project.harness.loadgen --run-id <run_id> --g7      # gate G7

Per focus cell the server(s) start in serve mode with W workers and print their ready line. The arm's own
`--mode client --corpus-out` then records a verified corpus once, and the load generator replays it
(docs/serve_protocol.md). configs/load.yaml fixes the three sweeps:

    closed     W = closed_loop.workers, C over closed_loop.clients, `runs` runs each. It stops after the first
               C whose median p95 exceeds stop_when_p95_over x the median p95 at the first C (spec 5.1).
    open       W = open_loop.workers, Poisson rate = each offered_load_fraction x the saturation throughput,
               the highest median throughput over the closed sweep's client counts.
    multicore  W over multicore.workers at C = multicore.clients, multicore.reps runs each (RQ7).

Servers restart only when W changes. Layout of results/raw/<run_id>/loadgen/:
    plan.json, meta.json    the focus cells and load.yaml, fixed at the first start
    corpus/<cell_id>.bin, <cell_id>.client.jsonl     the corpus and the corpus run's client records
    <cell_id>/<point>.jsonl                          one loadgen row per run; its presence means done
    ledger.jsonl            one line per process: corpus runs and load-generator runs
    servers/                each server start's summary record and stderr, and servers/ledger.jsonl
    rejected/               rows of runs that broke measurement hygiene
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import netem, run_grid, validate
from cs4406_sem_project.harness.run_grid import ContractError, HygieneError, Runner

LOAD_PATH = gridmod.ROOT / "configs" / "load.yaml"
LOADGEN = gridmod.ROOT / "build" / "loadgen" / "loadgen"
DRAIN_S = 300.0  # answers still in flight after the window; longer than any focus cell's backlog should need
SLACK_S = 120.0  # loadgen start-up, connections and STATS on top of warm-up, window and drain
EXPERIMENTS = ("closed", "open", "multicore")


class Unsupported(Exception):
    """The arm rejects the cell's parameters: its servers exit 0 without a ready line."""


@dataclass(frozen=True)
class Target:
    """One focus (arm, database, record) cell, served with any W."""

    arm: str
    db_bytes: int
    record_bytes: int | None
    seed: int
    binary: str
    timeout_s: float

    @property
    def cell_id(self) -> str:
        rec = "1bit" if self.record_bytes is None else gridmod.fmt_size(self.record_bytes)
        return f"{self.arm}_{gridmod.fmt_size(self.db_bytes)}_{rec}"

    @property
    def servers(self) -> int:
        return netem.servers_of(self.arm)

    def to_dict(self) -> dict:
        return {"cell_id": self.cell_id, "arm": self.arm, "db_bytes": self.db_bytes,
                "record_bytes": self.record_bytes, "seed": self.seed, "binary": self.binary,
                "timeout_s": self.timeout_s}


@dataclass(frozen=True)
class Point:
    """One sweep point: `runs` load-generator runs against servers with `workers` workers."""

    experiment: str  # closed, open or multicore
    workers: int
    runs: int
    clients: int | None = None  # closed and multicore
    fraction: float | None = None  # open: of the saturation throughput

    @property
    def loop(self) -> str:
        return "open" if self.experiment == "open" else "closed"

    @property
    def name(self) -> str:
        if self.experiment == "open":
            return f"open_w{self.workers}_f{self.fraction:.2f}"
        return f"{self.experiment}_w{self.workers}_c{self.clients}"


def targets(grid: dict, tier: str) -> list[Target]:
    seen: dict[tuple, Target] = {}
    for c in gridmod.expand(grid, tier):
        if c.phase == "online":
            seen.setdefault((c.arm, c.db_bytes, c.record_bytes),
                            Target(c.arm, c.db_bytes, c.record_bytes, c.seed, c.binary, c.timeout_s))
    return list(seen.values())


def only_cells(ts: list[Target], cells: dict | None) -> list[Target]:
    """Keep the targets whose database and record sizes load.yaml's `cells` lists ("1bit" for one-bit arms)."""
    if not cells:
        return ts
    dbs = {gridmod.parse_size(s) for s in cells["db_bytes"]}
    recs = {None if s == "1bit" else gridmod.parse_size(s) for s in cells["record_bytes"]}
    return [t for t in ts if t.db_bytes in dbs and t.record_bytes in recs]


def closed_points(load: dict) -> list[Point]:
    cl = load["closed_loop"]
    return [Point("closed", cl["workers"], load["runs"], clients=c) for c in cl["clients"]]


def open_points(load: dict) -> list[Point]:
    ol = load["open_loop"]
    return [Point("open", ol["workers"], load["runs"], fraction=f) for f in ol["offered_load_fraction"]]


def multicore_points(load: dict) -> list[Point]:
    mc = load["multicore"]
    return [Point("multicore", w, mc["reps"], clients=mc["clients"]) for w in mc["workers"]]


def _median(rows: list[dict], key: str) -> float:
    return statistics.median(r[key] for r in rows)


def next_clients(load: dict, done: dict[int, list[dict]]) -> int | None:
    """The next client count the closed sweep runs, or None when it is over: every count has run, a run
    failed, or the last count's median p95 passed stop_when_p95_over x the first count's (spec 5.1)."""
    factor, base = load["closed_loop"]["stop_when_p95_over"], None
    for c in load["closed_loop"]["clients"]:
        rows = done.get(c)
        if rows is None:
            return c
        if any(r["status"] == "error" for r in rows):
            return None
        p95 = _median(rows, "lat_p95_s")
        if base is None:
            base = p95
        elif p95 > factor * base:
            return None
    return None


def p95_limit(load: dict, done: dict[int, list[dict]]) -> float | None:
    """The stop rule's bound once the first client count has run, for the rows' `saturated` status."""
    first = done.get(load["closed_loop"]["clients"][0])
    if not first or any(r["status"] == "error" for r in first):
        return None
    return load["closed_loop"]["stop_when_p95_over"] * _median(first, "lat_p95_s")


def saturation_qps(done: dict[int, list[dict]]) -> float | None:
    """The closed sweep's saturation throughput: the highest median throughput over its client counts."""
    medians = [_median(rows, "throughput_qps") for rows in done.values()
               if rows and all(r["status"] != "error" for r in rows)]
    return max(medians) if medians else None


def g7_problems(rows: list[dict], limit_pct: float) -> list[str]:
    """Spec 11 test_loadgen_not_bottleneck: the load generator's CPU stays below the limit at the highest C
    each cell used. Error rows are left out; a cell whose closed loop never ran is a problem."""
    by_cell: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["loop"] == "closed" and r["status"] != "error":
            by_cell.setdefault((r["arm"], r["db_bytes"], r["record_bytes"]), []).append(r)
    if not by_cell:
        return ["no closed-loop rows"]
    problems = []
    for (arm, db, rec), rs in sorted(by_cell.items(), key=lambda kv: str(kv[0])):
        top = max(r["clients"] for r in rs)
        for r in rs:
            if r["clients"] != top:
                continue
            util = r["loadgen_cpu_util"]
            where = f"{arm} db {db} record {rec} W {r['workers']} C {top} run {r['run']}"
            if util is None:
                problems.append(f"{where}: no load-generator CPU measurement")
            elif util >= limit_pct:
                problems.append(f"{where}: load generator at {util:.1f} % CPU, limit {limit_pct:g} %")
    return problems


def read_rows(run_dir: Path) -> list[dict]:
    """Every loadgen row of a run: the finished points under <cell_id>/."""
    rows = []
    for path in sorted(run_dir.glob("*/*.jsonl")):
        if path.parent.name not in ("corpus", "servers", "rejected"):
            rows += [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return rows


def bound_s(load: dict, n_targets: int, experiments: tuple[str, ...]) -> float:
    """Warm-up plus window of every planned run, before the stop rule cuts the closed sweep."""
    per_run = load["warmup_s"] + load["window_s"]
    plans = {"closed": closed_points, "open": open_points, "multicore": multicore_points}
    runs = sum(p.runs for e in experiments for p in plans[e](load))
    return n_targets * runs * per_run


@dataclass
class LoadgenRunner(Runner):
    load: dict = field(default_factory=dict)
    targets: list[Target] = field(default_factory=list)
    experiments: tuple[str, ...] = EXPERIMENTS
    loadgen_cmd: list[str] = field(default_factory=lambda: [str(LOADGEN)])
    drain_s: float = DRAIN_S
    stop_timeout_s: float = netem.STOP_TIMEOUT_S
    footprint: Callable[[int], int] = field(default_factory=netem._footprint)
    program: str = "harness.loadgen"
    _serving: tuple[int, netem.Servers] | None = field(default=None, init=False, repr=False)

    def point_path(self, t: Target, p: Point) -> Path:
        return self.run_dir / t.cell_id / f"{p.name}.jsonl"

    def corpus_paths(self, t: Target) -> tuple[Path, Path]:
        d = self.run_dir / "corpus"
        return d / f"{t.cell_id}.bin", d / f"{t.cell_id}.client.jsonl"

    def done_rows(self, t: Target, p: Point) -> list[dict] | None:
        path = self.point_path(t, p)
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()] if path.exists() else None

    def closed_done(self, t: Target) -> dict[int, list[dict]]:
        out = {}
        for p in closed_points(self.load):
            rows = self.done_rows(t, p)
            if rows is not None:
                out[p.clients] = rows
        return out

    def _ledger(self, entry: dict) -> None:
        super()._ledger({"at": dt.datetime.now(dt.timezone.utc).isoformat(), **entry})

    def _servers(self, t: Target, workers: int) -> netem.Servers | None:
        """Servers for t with `workers` workers, restarted only when W changes. None if they did not start."""
        if self._serving and self._serving[0] == workers and all(
                s.proc.poll() is None for s in self._serving[1].members):
            return self._serving[1]
        self._stop_servers(t)
        d = self.run_dir / "servers"
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{t.cell_id}.w{workers}.{int(time.time())}"
        argvs = []
        for k in range(t.servers):
            argv = [*self.launcher, str(self.adapter_dir / t.binary), "--mode", "serve", "--arm", t.arm,
                    "--db-bytes", str(t.db_bytes), "--record-bytes", str(t.record_bytes or 0), "--seed", str(t.seed),
                    "--workers", str(workers), "--listen", "127.0.0.1:0", "--out", str(d / f"{stem}.server{k}.jsonl")]
            argvs.append(argv + (["--server-id", str(k)] if t.servers == 2 else []))
        self.log(f"    starting {t.servers} server(s) with W = {workers}")
        servers = netem.start_servers(argvs, [d / f"{stem}.server{k}.stderr.log" for k in range(t.servers)],
                                      self.env(), self.mem_cap_bytes, t.timeout_s, self.footprint)
        self._serving = (workers, servers)
        if servers.failure is None and not servers.unsupported:
            return servers
        self._stop_servers(t)
        if servers.unsupported:
            raise Unsupported(t.cell_id)
        return None

    def _stop_servers(self, t: Target) -> None:
        if self._serving is None:
            return
        workers, s = self._serving
        self._serving = None
        if not s.stopped:
            s.stop(self.stop_timeout_s)
        with (self.run_dir / "servers" / "ledger.jsonl").open("a") as f:
            f.write(json.dumps({
                "cell_id": t.cell_id, "workers": workers, "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "addresses": [m.addr for m in s.members], "exit_codes": s.stopped,
                "peak_footprint_bytes": s.peak_footprint_bytes, "over_cap": s.over_cap,
                "unsupported": s.unsupported, "failure": None if s.failure is None else s.failure.stderr[-2000:],
            }) + "\n")

    def _corpus(self, t: Target, servers: netem.Servers) -> bool:
        corpus, client = self.corpus_paths(t)
        if corpus.exists() and client.exists():
            return True
        corpus.parent.mkdir(parents=True, exist_ok=True)
        cpart, kpart = corpus.with_suffix(".bin.part"), client.with_suffix(".jsonl.part")
        for p in (cpart, kpart):
            p.unlink(missing_ok=True)
        q = self.load["corpus_queries"]
        argv = [*self.launcher, str(self.adapter_dir / t.binary), "--mode", "client", "--arm", t.arm,
                "--db-bytes", str(t.db_bytes), "--record-bytes", str(t.record_bytes or 0), "--seed", str(t.seed),
                "--connect", servers.addresses((0, 0)), "--session-queries", str(q), "--out", str(kpart),
                "--corpus-out", str(cpart)]
        t0 = time.monotonic()
        fin = self.execute(argv, t.timeout_s, self.mem_cap_bytes, self.env())
        lines = [ln for ln in kpart.read_text().splitlines() if ln.strip()] if kpart.exists() else []
        problems = [p for p in validate.check_lines(lines, reference_platform=self.reference_platform)
                    if p.severity == "error"]
        ok = fin.returncode == 0 and len(lines) == q and not problems and cpart.exists()
        self._ledger({"cell_id": t.cell_id, "point": "corpus", "wall_s": time.monotonic() - t0,
                      "exit_code": fin.returncode, "rows": len(lines), "status": "ok" if ok else "failed",
                      "error": None if ok else (str(problems[0]) if problems else fin.stderr[-2000:])})
        if ok:
            cpart.replace(corpus)
            kpart.replace(client)
        return ok

    def _check_row(self, t: Target, p: Point, run: int, row: dict) -> None:
        want = {"mode": "loadgen", "run_id": self.run_id, "arm": t.arm, "db_bytes": t.db_bytes,
                "record_bytes": t.record_bytes, "seed": t.seed, "loop": p.loop, "run": run}
        if row["status"] != "error":
            want["workers"] = p.workers
            if p.loop == "closed":
                want["clients"] = p.clients
        diff = {k: (row.get(k), v) for k, v in want.items() if row.get(k) != v}
        if diff:
            raise ContractError(f"{t.cell_id} {p.name} run {run}: row does not match its point: {diff}")

    def run_point(self, t: Target, p: Point, *, rate: float | None = None,
                  limit_s: float | None = None) -> list[dict] | None:
        """Every run of one point, or None if a process failed without a row (the point stays to do)."""
        final = self.point_path(t, p)
        if final.exists():
            return self.done_rows(t, p)
        servers = self._servers(t, p.workers)
        if servers is None or not self._corpus(t, servers):
            return None
        corpus, client = self.corpus_paths(t)
        final.parent.mkdir(parents=True, exist_ok=True)
        part = final.with_suffix(".jsonl.part")
        part.unlink(missing_ok=True)
        rows: list[dict] = []
        for run in range(p.runs):
            argv = [*self.loadgen_cmd, "--corpus", str(corpus), "--client-records", str(client),
                    "--connect", servers.addresses((0, 0)), "--loop", p.loop,
                    "--warmup-s", str(self.load["warmup_s"]), "--window-s", str(self.load["window_s"]),
                    "--drain-s", str(self.drain_s), "--run", str(run), "--out", str(part),
                    "--run-id", self.run_id, "--platform", self.platform, "--git-sha", self.harness_sha or ""]
            if p.loop == "closed":
                argv += ["--clients", str(p.clients)]
                if limit_s is not None:
                    argv += ["--p95-limit-s", repr(limit_s)]
            else:
                argv += ["--rate", repr(rate), "--seed", str(run + 1)]
            timeout = self.load["warmup_s"] + self.load["window_s"] + self.drain_s + SLACK_S
            t0 = time.monotonic()
            with self.watch() as w:
                fin = self.execute(argv, timeout, self.mem_cap_bytes, self.env())
            entry = {"cell_id": t.cell_id, "point": p.name, "run": run, "wall_s": time.monotonic() - t0,
                     "exit_code": fin.returncode, "timed_out": fin.timed_out, "hygiene": w.record(),
                     "servers_over_cap": servers.over_cap}
            if w.problems:
                rejected = self.run_dir / "rejected"
                rejected.mkdir(exist_ok=True)
                if part.exists():
                    part.rename(rejected / f"{t.cell_id}.{p.name}.{int(time.time())}.jsonl")
                self._ledger({**entry, "status": "hygiene_failed"})
                raise HygieneError(f"{t.cell_id} {p.name} run {run}: {'; '.join(w.problems)}")
            lines = [ln for ln in part.read_text().splitlines() if ln.strip()] if part.exists() else []
            if fin.returncode not in (0, 1) or len(lines) != run + 1:
                self._ledger({**entry, "status": "failed", "error": fin.stderr[-2000:]})
                return None
            problems = [x for x in validate.check_lines(lines[-1:], reference_platform=self.reference_platform)
                        if x.severity == "error"]
            if problems:
                raise ContractError(f"{t.cell_id} {p.name} run {run}: loadgen row fails validation: {problems[0]}")
            row = json.loads(lines[-1])
            self._check_row(t, p, run, row)
            rows.append(row)
            self._ledger({**entry, "status": row["status"], "error": row["error"]})
        part.replace(final)
        return rows

    def run_target(self, t: Target) -> str:
        try:
            if "closed" in self.experiments:
                while (c := next_clients(self.load, self.closed_done(t))) is not None:
                    p = next(p for p in closed_points(self.load) if p.clients == c)
                    self.log(f"  {t.cell_id} {p.name}")
                    if self.run_point(t, p, limit_s=p95_limit(self.load, self.closed_done(t))) is None:
                        return "failed"
            if "open" in self.experiments:
                sat = saturation_qps(self.closed_done(t))
                if sat is None or sat <= 0:
                    self.log(f"  {t.cell_id}: no closed-loop saturation throughput, open loop skipped")
                    return "no_saturation"
                for p in open_points(self.load):
                    self.log(f"  {t.cell_id} {p.name} at {p.fraction * sat:.4g} q/s")
                    if self.run_point(t, p, rate=p.fraction * sat) is None:
                        return "failed"
            if "multicore" in self.experiments:
                for p in multicore_points(self.load):
                    self.log(f"  {t.cell_id} {p.name}")
                    if self.run_point(t, p) is None:
                        return "failed"
            return "done"
        except Unsupported:
            return "unsupported_params"
        finally:
            self._stop_servers(t)

    def run(self) -> dict[str, int]:
        if self.platform != self.reference_platform:
            raise ContractError(f"this machine is {self.platform!r}; reported numbers come only from "
                                f"{self.reference_platform!r} (spec 7)")
        self._take_lock()
        counts: dict[str, int] = {}
        for i, t in enumerate(self.targets, 1):
            self.log(f"[{i}/{len(self.targets)}] {t.cell_id}")
            status = self.run_target(t)
            counts[status] = counts.get(status, 0) + 1
        return counts


def write_plan(run_dir: Path, ts: list[Target], load: dict, meta: dict) -> None:
    """Fix the cells and load.yaml at the first start. A resume must have the same."""
    plan = {"targets": [t.to_dict() for t in ts], "load": load}
    path = run_dir / "plan.json"
    if path.exists():
        if json.loads(path.read_text()) != plan:
            raise ContractError(f"{path} differs from the grid or load.yaml now; start a new run instead")
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=1) + "\n")
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")


def dry_run(ts: list[Target], load: dict, experiments: tuple[str, ...]) -> str:
    plans = {"closed": closed_points, "open": open_points, "multicore": multicore_points}
    points = [p for e in experiments for p in plans[e](load)]
    lines = [f"{'point':<22} {'W':>2} {'C':>4} {'fraction':>8} {'runs':>4}"]
    lines += [f"{p.name:<22} {p.workers:>2} {p.clients or '':>4} {p.fraction or '':>8} {p.runs:>4}" for p in points]
    lines.append(f"cells: {', '.join(t.cell_id for t in ts)}")
    lines.append(f"{len(ts)} cells x {sum(p.runs for p in points)} runs; warm-up and windows alone take at most "
                 f"{bound_s(load, len(ts), experiments) / 3600:.1f} h, before server start-up, corpus and drain "
                 "time, and before the stop rule cuts the closed sweep")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-id", help="results/raw/RUN_ID; default a new id. Reuse it to resume")
    p.add_argument("--tier", default="focus")
    p.add_argument("--arms", help="comma-separated arms; default every focus arm")
    p.add_argument("--experiments", default=",".join(EXPERIMENTS), help="any of closed,open,multicore")
    p.add_argument("--g7", action="store_true", help="check gate G7 on --run-id's rows and exit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--grid", type=Path, default=gridmod.GRID_PATH)
    p.add_argument("--load", type=Path, default=LOAD_PATH)
    p.add_argument("--results", type=Path, default=run_grid.RESULTS)
    p.add_argument("--adapter-dir", type=Path, default=run_grid.ADAPTER_DIR)
    p.add_argument("--loadgen", default=str(LOADGEN), help="load-generator command (tests use a fake)")
    p.add_argument("--reference-platform", type=Path, default=run_grid.REFERENCE_PLATFORM)
    p.add_argument("--launcher", default="", help="command prefix for every adapter run (tests use it)")
    args = p.parse_args(argv)

    load = yaml.safe_load(args.load.read_text())
    if args.g7:
        if not args.run_id:
            print("--g7 needs --run-id", file=sys.stderr)
            return run_grid.EXIT_USAGE
        rows = read_rows(args.results / args.run_id / "loadgen")
        problems = g7_problems(rows, load["loadgen_cpu_limit_pct"])
        for line in problems:
            print(f"G7 FAIL: {line}")
        print(f"G7 {'FAIL' if problems else 'PASS'}: {len(rows)} rows, limit {load['loadgen_cpu_limit_pct']} %")
        return run_grid.EXIT_ERROR if problems else run_grid.EXIT_OK

    experiments = tuple(e for e in args.experiments.split(",") if e)
    if not set(experiments) <= set(EXPERIMENTS):
        print(f"--experiments takes {','.join(EXPERIMENTS)}", file=sys.stderr)
        return run_grid.EXIT_USAGE
    grid = gridmod.load(args.grid)
    ts = only_cells(targets(grid, args.tier), load.get("cells"))
    if args.arms:
        ts = [t for t in ts if t.arm in args.arms.split(",")]
    if args.dry_run:
        print(dry_run(ts, load, experiments))
        return run_grid.EXIT_OK
    if not args.reference_platform.exists():
        print(f"{args.reference_platform} is missing; run `make sysinfo` on the reference Mac", file=sys.stderr)
        return run_grid.EXIT_USAGE
    loadgen_cmd = run_grid.split_command(args.loadgen)
    if args.loadgen == str(LOADGEN) and not LOADGEN.exists():
        print(f"{LOADGEN} is missing; run `make loadgen`", file=sys.stderr)
        return run_grid.EXIT_USAGE

    run_id = args.run_id or run_grid.new_run_id("p9")
    run_dir = args.results / run_id / "loadgen"
    runner = LoadgenRunner(
        cells=[], run_dir=run_dir, run_id=run_id, platform=run_grid.current_platform(),
        reference_platform=validate.read_reference_platform(args.reference_platform),
        adapter_dir=args.adapter_dir, mem_cap_bytes=int(grid["mem_cap_gib"] * run_grid.GIB),
        harness_sha=run_grid.git_sha(), launcher=run_grid.split_command(args.launcher),
        load=load, targets=ts, experiments=experiments, loadgen_cmd=loadgen_cmd,
    )
    try:
        write_plan(run_dir, ts, load, {
            "run_id": run_id, "tier": args.tier, "experiment": "RQ6, RQ7", "git_sha_harness": runner.harness_sha,
            "platform": runner.platform, "started": dt.datetime.now(dt.timezone.utc).isoformat()})
        with netem._caffeinated():
            counts = runner.run()
    except HygieneError as e:
        print(f"stopped, measurement hygiene broken: {e}\nfix it, then rerun with --run-id {run_id}", file=sys.stderr)
        return run_grid.EXIT_HYGIENE
    except ContractError as e:
        print(f"stopped: {e}", file=sys.stderr)
        return run_grid.EXIT_ERROR
    print(f"{run_id} loadgen: {counts}", file=sys.stderr)
    return run_grid.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
