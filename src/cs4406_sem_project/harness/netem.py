"""RQ3b: one client against the arm's own servers over an emulated link, at every focus cell (spec 5.1).

    sudo src/cs4406_sem_project/harness/dummynet.sh up broadband
    sudo src/cs4406_sem_project/harness/dummynet.sh calibrate broadband      # gate G4
    uv run python -m cs4406_sem_project.harness.netem --profile broadband --run-id <run_id>
    uv run python -m cs4406_sem_project.harness.netem --profile none --run-id <run_id>   # loopback baseline

The humans bring the link up with dummynet.sh, which needs root. This module never does: it reads the
state file dummynet.sh writes, and refuses to run unless the link is the requested profile, matches
configs/network_profiles.yaml and passed calibration. `--profile none` refuses while any link is up.

Per cell the server(s) start in serve mode on the shaped ports (two for M0 and M1), the harness waits for
each ready line, runs `--mode client --session-queries Q` through run_grid's Runner (memory cap, hygiene,
validation, resumability), then stops the servers with SIGTERM. Layout of results/raw/<run_id>/netem/<profile>/:
    plan.json, meta.json, ledger.jsonl, <cell_id>.jsonl   as in run_grid; the rows are client records
    link.json                                             the link state at every start of this profile
    servers/<cell_id>.server<k>.jsonl, .stderr.log        each server's summary record and log
    servers/ledger.jsonl                                  per cell: server exit codes and peak footprints

The profile lives in the path, not the rows: client rows are schema adapter records, which take no extra key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psutil
import yaml

from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import run_grid, validate
from cs4406_sem_project.harness.grid import Cell
from cs4406_sem_project.harness.run_grid import ContractError, Finished, HygieneError, Runner

PROFILES_PATH = gridmod.ROOT / "configs" / "network_profiles.yaml"
STATE_PATH = Path(os.environ.get("VPIR_DUMMYNET_STATE", "/var/run/vpir-dummynet.state"))
NONE = "none"
QUERIES = 30  # spec 5.1: 30 per profile
WORKERS = 1  # one client, so one worker; RQ7 varies this
STOP_TIMEOUT_S = 120
READY = re.compile(r"^vpir-serve ready (\S+):(\d+)\s*$")


class LinkError(RuntimeError):
    """The emulated link is not the one this run asks for."""


@dataclass(frozen=True)
class Profile:
    name: str
    down_mbps: float
    up_mbps: float
    rtt_ms: float


def load_profiles(path: Path = PROFILES_PATH) -> dict[str, Profile]:
    raw = yaml.safe_load(path.read_text())
    return {name: Profile(name, float(v["down_mbps"]), float(v["up_mbps"]), float(v["rtt_ms"]))
            for name, v in raw.items()}


@dataclass(frozen=True)
class Link:
    """What dummynet.sh up and calibrate left in the state file."""

    profile: str
    down_mbps: float
    up_mbps: float
    rtt_ms: float
    ports: tuple[int, ...]
    calibrated: str  # pass, fail or none
    raw: dict[str, str]


def read_link(path: Path = STATE_PATH) -> Link | None:
    if not path.exists():
        return None
    kv = dict(ln.split("=", 1) for ln in path.read_text().splitlines() if "=" in ln)
    try:
        return Link(kv["profile"], float(kv["down_mbps"]), float(kv["up_mbps"]), float(kv["rtt_ms"]),
                    tuple(int(p) for p in kv["ports"].split()), kv.get("calibrated", "none"), kv)
    except (KeyError, ValueError) as e:
        raise LinkError(f"{path} is not a dummynet.sh state file: {e!r}") from e


def listen_ports(profile: str, profiles: dict[str, Profile], link: Link | None) -> tuple[int, int]:
    """The ports the servers listen on, after checking the link is the one asked for."""
    if profile == NONE:
        if link is not None:
            raise LinkError(f"a {link.profile!r} link is up; the loopback baseline needs `dummynet.sh down`")
        return 0, 0
    if profile not in profiles:
        raise LinkError(f"unknown profile {profile!r}; have {sorted(profiles)} and {NONE!r}")
    up = f"sudo src/cs4406_sem_project/harness/dummynet.sh up {profile}"
    if link is None:
        raise LinkError(f"no emulated link is up; run: {up}")
    if link.profile != profile:
        raise LinkError(f"the link is {link.profile!r}, not {profile!r}; run: {up}")
    p = profiles[profile]
    if (link.down_mbps, link.up_mbps, link.rtt_ms) != (p.down_mbps, p.up_mbps, p.rtt_ms):
        raise LinkError(f"the link's values differ from configs/network_profiles.yaml for {profile}; run: {up}")
    if link.calibrated != "pass":
        raise LinkError(f"the {profile} link has calibration {link.calibrated!r}, and gate G4 needs a pass; "
                        f"run: sudo src/cs4406_sem_project/harness/dummynet.sh calibrate {profile}")
    if len(set(link.ports)) < 2:
        raise LinkError(f"two-server arms need two shaped ports, the link has {link.ports}")
    return link.ports[0], link.ports[1]


def parse_ready(line: str) -> tuple[str, int] | None:
    m = READY.match(line)
    return (m.group(1), int(m.group(2))) if m else None


def netem_cells(grid: dict, tier: str, queries: int) -> list[Cell]:
    """One client cell per focus (arm, db, record): `queries` online rows and no warm-up, which client
    mode does not have."""
    seen: dict[tuple, Cell] = {}
    for c in gridmod.expand(grid, tier):
        if c.phase == "online":
            seen.setdefault((c.arm, c.db_bytes, c.record_bytes),
                            Cell(c.arm, c.db_bytes, c.record_bytes, "online", queries, 0, c.seed,
                                 c.timeout_s, c.binary))
    return list(seen.values())


def servers_of(arm: str) -> int:
    return 2 if arm in validate.TWO_SERVER_ARMS else 1


def _footprint() -> Callable[[int], int]:
    if sys.platform == "darwin":
        from cs4406_sem_project.harness import memcap

        return memcap.footprint_bytes

    def rss(pid: int) -> int:
        try:
            return psutil.Process(pid).memory_info().rss
        except psutil.Error:
            return 0

    return rss


def _tree(proc: subprocess.Popen) -> list[psutil.Process]:
    """The process and its children: a launcher (uv's on Windows, a test's interpreter) runs the server
    as a child, as memcap.run assumes too."""
    try:
        p = psutil.Process(proc.pid)
        return [p, *p.children(recursive=True)]
    except psutil.Error:
        return []


def _kill_tree(proc: subprocess.Popen) -> None:
    for p in reversed(_tree(proc)):
        try:
            p.kill()
        except psutil.Error:
            pass
    proc.wait()


@dataclass
class Server:
    proc: subprocess.Popen
    log: Path
    addr: tuple[str, int] | None = None
    lines: list[str] = field(default_factory=list)
    ready: threading.Event = field(default_factory=threading.Event)

    def pump(self) -> None:
        with self.log.open("w") as log:
            for line in self.proc.stderr:
                log.write(line)
                log.flush()
                self.lines.append(line.rstrip("\n"))
                if self.addr is None and (addr := parse_ready(line.rstrip("\n"))):
                    self.addr = addr
                    self.ready.set()
        self.ready.set()  # stderr closed: the process is gone, ready or not


@dataclass
class Servers:
    """The servers of one cell. `failure` is set when they did not all become ready."""

    members: list[Server]
    peak_footprint_bytes: int = 0
    over_cap: bool = False
    failure: Finished | None = None
    unsupported: bool = False
    stopped: list[int | None] = field(default_factory=list)

    def addresses(self, ports: tuple[int, int]) -> str:
        """Each server's ready address. A server without one gets its listen port: with unsupported
        parameters the client rejects them before it connects, and after a failure no client runs."""
        return ",".join(f"{s.addr[0]}:{s.addr[1]}" if s.addr else f"127.0.0.1:{ports[k]}"
                        for k, s in enumerate(self.members))

    def stderr_tail(self) -> str:
        return "\n".join(f"server {k}: {ln}" for k, s in enumerate(self.members) for ln in s.lines[-20:])

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        for s in self.members:
            if s.proc.poll() is None:
                s.proc.send_signal(signal.SIGTERM)
        codes = []
        for s in self.members:
            try:
                codes.append(s.proc.wait(timeout_s))
            except subprocess.TimeoutExpired:
                _kill_tree(s.proc)
                codes.append(None)
        self.stopped = codes


def start_servers(argvs: list[list[str]], logs: list[Path], env: dict, cap_bytes: int, ready_timeout_s: float,
                  footprint: Callable[[int], int]) -> Servers:
    members = []
    for argv, log in zip(argvs, logs):
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
        members.append(Server(proc, log))
        threading.Thread(target=members[-1].pump, daemon=True).start()
    servers = Servers(members)

    def watch() -> None:
        while any(s.proc.poll() is None for s in members):
            total = sum(footprint(p.pid) for s in members for p in _tree(s.proc))
            servers.peak_footprint_bytes = max(servers.peak_footprint_bytes, total)
            if total > cap_bytes:
                servers.over_cap = True
                for s in members:
                    _kill_tree(s.proc)
                return
            time.sleep(0.1)

    threading.Thread(target=watch, daemon=True).start()

    deadline = time.monotonic() + ready_timeout_s
    for s in members:
        s.ready.wait(max(0.0, deadline - time.monotonic()))
    if all(s.addr for s in members):
        return servers
    for s in members:
        _kill_tree(s.proc)
    codes = [s.proc.returncode for s in members]
    if not servers.over_cap and all(s.addr is None for s in members) and all(c == 0 for c in codes):
        servers.unsupported = True  # serve protocol: unsupported parameters, no ready line, exit 0
        return servers
    timed_out = time.monotonic() >= deadline and not servers.over_cap
    failed = next((c for c in codes if c not in (0, None) and c >= 0), None)
    servers.failure = Finished(None if timed_out else (failed if failed is not None else 1),
                               f"servers did not all become ready (exit codes {codes})\n{servers.stderr_tail()}",
                               servers.peak_footprint_bytes, servers.over_cap, timed_out)
    return servers


@dataclass
class NetemRunner(Runner):
    profile: str = NONE
    ports: tuple[int, int] = (0, 0)
    workers: int = WORKERS
    stop_timeout_s: float = STOP_TIMEOUT_S
    footprint: Callable[[int], int] = field(default_factory=_footprint)
    program: str = "netem"
    _servers: Servers | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        client_execute = self.execute

        def execute(argv, timeout_s, cap_bytes, env) -> Finished:
            s = self._servers
            if s.failure is not None:
                return s.failure
            fin = client_execute(argv, timeout_s, cap_bytes, env)
            s.stop(self.stop_timeout_s)
            if not s.over_cap:
                return fin
            return Finished(fin.returncode, f"{fin.stderr}\nservers passed the memory cap", fin.peak_footprint_bytes,
                            True, fin.timed_out)

        self.execute = execute

    def _server_dir(self) -> Path:
        d = self.run_dir / "servers"
        d.mkdir(exist_ok=True)
        return d

    def server_argv(self, c: Cell, k: int, out: Path) -> list[str]:
        argv = [*self.launcher, str(self.adapter_dir / c.binary), "--mode", "serve", "--arm", c.arm,
                "--db-bytes", str(c.db_bytes), "--record-bytes", str(c.record_bytes or 0), "--seed", str(c.seed),
                "--workers", str(self.workers), "--listen", f"127.0.0.1:{self.ports[k]}", "--out", str(out)]
        if servers_of(c.arm) == 2:
            argv += ["--server-id", str(k)]
        return argv

    def adapter_args(self, c: Cell, out: Path) -> list[str]:
        return ["--mode", "client", "--arm", c.arm, "--db-bytes", str(c.db_bytes),
                "--record-bytes", str(c.record_bytes or 0), "--seed", str(c.seed),
                "--connect", self._servers.addresses(self.ports), "--session-queries", str(c.reps),
                "--out", str(out)]

    @contextmanager
    def _serving(self, c: Cell) -> Iterator[Servers]:
        d = self._server_dir()
        n = servers_of(c.arm)
        outs = [d / f"{c.cell_id}.server{k}.jsonl" for k in range(n)]
        for o in outs:
            o.unlink(missing_ok=True)
        servers = start_servers([self.server_argv(c, k, outs[k]) for k in range(n)],
                                [d / f"{c.cell_id}.server{k}.stderr.log" for k in range(n)],
                                self.env(), self.mem_cap_bytes, c.timeout_s, self.footprint)
        try:
            yield servers
        finally:
            if not servers.stopped:
                servers.stop(self.stop_timeout_s)
            with (d / "ledger.jsonl").open("a") as f:
                f.write(json.dumps({
                    "cell_id": c.cell_id, "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "profile": self.profile, "addresses": [s.addr for s in servers.members],
                    "exit_codes": servers.stopped, "peak_footprint_bytes": servers.peak_footprint_bytes,
                    "over_cap": servers.over_cap, "unsupported": servers.unsupported,
                    "summary_rows": [sum(1 for _ in o.open()) if o.exists() else 0 for o in outs],
                }) + "\n")

    def run_cell(self, c: Cell) -> str:
        with self._serving(c) as servers:
            self._servers = servers
            try:
                return super().run_cell(c)
            finally:
                self._servers = None


@contextmanager
def _caffeinated() -> Iterator[None]:
    """Keep the Mac awake for the whole run, server start-up included (spec 7)."""
    if sys.platform != "darwin":
        yield
        return
    proc = subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())])
    try:
        yield
    finally:
        proc.terminate()


def _record_link(run_dir: Path, profile: str, link: Link | None) -> None:
    path = run_dir / "link.json"
    history = json.loads(path.read_text()) if path.exists() else []
    history.append({"at": dt.datetime.now(dt.timezone.utc).isoformat(), "profile": profile,
                    "state": None if link is None else link.raw})
    path.write_text(json.dumps(history, indent=1) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile", required=True, help=f"a profile in network_profiles.yaml, or {NONE!r}")
    p.add_argument("--run-id", help="results/raw/RUN_ID; default a new id. Reuse it to resume or add profiles")
    p.add_argument("--tier", default="focus")
    p.add_argument("--queries", type=int, default=QUERIES)
    p.add_argument("--workers", type=int, default=WORKERS)
    p.add_argument("--grid", type=Path, default=gridmod.GRID_PATH)
    p.add_argument("--profiles", type=Path, default=PROFILES_PATH)
    p.add_argument("--state", type=Path, default=STATE_PATH, help="dummynet.sh's state file")
    p.add_argument("--results", type=Path, default=run_grid.RESULTS)
    p.add_argument("--adapter-dir", type=Path, default=run_grid.ADAPTER_DIR)
    p.add_argument("--reference-platform", type=Path, default=run_grid.REFERENCE_PLATFORM)
    p.add_argument("--launcher", default="", help="command prefix for every adapter run (tests use it)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    grid = gridmod.load(args.grid)
    cells = netem_cells(grid, args.tier, args.queries)
    run_id = args.run_id or run_grid.new_run_id("p6")
    run_dir = args.results / run_id / "netem" / args.profile
    if args.dry_run:
        done = {c.cell_id for c in cells if (run_dir / f"{c.cell_id}.jsonl").exists()}
        print(run_grid.dry_run(cells, done))
        return run_grid.EXIT_OK
    try:
        link = read_link(args.state)
        ports = listen_ports(args.profile, load_profiles(args.profiles), link)
    except LinkError as e:
        print(f"refused: {e}", file=sys.stderr)
        return run_grid.EXIT_USAGE
    if not args.reference_platform.exists():
        print(f"{args.reference_platform} is missing; run `make sysinfo` on the reference Mac", file=sys.stderr)
        return run_grid.EXIT_USAGE

    runner = NetemRunner(
        cells=cells, run_dir=run_dir, run_id=run_id, platform=run_grid.current_platform(),
        reference_platform=validate.read_reference_platform(args.reference_platform),
        adapter_dir=args.adapter_dir, mem_cap_bytes=int(grid["mem_cap_gib"] * run_grid.GIB),
        harness_sha=run_grid.git_sha(), launcher=run_grid.split_command(args.launcher),
        profile=args.profile, ports=ports, workers=args.workers,
    )
    try:
        run_grid.write_plan(run_dir, cells, {
            "run_id": run_id, "tier": args.tier, "experiment": "RQ3b", "profile": args.profile,
            "queries": args.queries, "workers": args.workers, "git_sha_harness": runner.harness_sha,
            "platform": runner.platform, "started": dt.datetime.now(dt.timezone.utc).isoformat()})
        _record_link(run_dir, args.profile, link)
        with _caffeinated():
            counts = runner.run()
    except HygieneError as e:
        print(f"stopped, measurement hygiene broken: {e}\nfix it, then rerun with --run-id {run_id}", file=sys.stderr)
        return run_grid.EXIT_HYGIENE
    except ContractError as e:
        print(f"stopped: {e}", file=sys.stderr)
        return run_grid.EXIT_ERROR
    print(f"{run_id} netem/{args.profile}: {counts}", file=sys.stderr)
    return run_grid.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
