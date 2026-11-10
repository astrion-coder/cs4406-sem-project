import copy
import json
import os
import sys
from pathlib import Path

import psutil
import pytest

from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import loadgen, validate
from cs4406_sem_project.harness.loadgen import LoadgenRunner, Point

ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVE = ROOT / "tests" / "fake_serve.py"
FAKE_LOADGEN = ROOT / "tests" / "fake_loadgen.py"
REAL_LOADGEN = ROOT / "build" / "loadgen" / "loadgen"
PLATFORM = "test-platform"

ROW = {
    "mode": "loadgen", "run_id": "r", "git_sha_harness": None, "platform": PLATFORM, "arm": "S1",
    "db_bytes": 1 << 20, "record_bytes": 32, "seed": 7, "workers": 6, "loop": "closed", "clients": 1,
    "offered_load_qps": None, "run": 0, "warmup_s": 10, "window_s": 60, "sent": 600, "completed": 600,
    "mismatched": 0, "errors": 0, "throughput_qps": 10.0, "lat_p50_s": 0.1, "lat_p95_s": 0.12,
    "lat_p99_s": 0.15, "e2e_lat_p50_s": None, "e2e_lat_p95_s": None, "e2e_lat_p99_s": None,
    "server_cpu_util": 100.0, "server_peak_rss_bytes": 1 << 30, "loadgen_cpu_util": 3.0, "status": "ok",
    "error": None,
}

LOAD = {
    "corpus_queries": 4, "warmup_s": 0, "window_s": 1, "runs": 2,
    "closed_loop": {"clients": [1, 2, 4, 8, 16, 32], "stop_when_p95_over": 10, "workers": 6},
    "open_loop": {"offered_load_fraction": [0.5, 1.1], "arrivals": "poisson", "workers": 6},
    "multicore": {"workers": [1, 2], "clients": 4, "reps": 2},
    "loadgen_cpu_limit_pct": 70,
}


def row(**over) -> dict:
    return {**copy.deepcopy(ROW), **over}


def closed(by_c: dict[int, list[float]], key="lat_p95_s", **over) -> dict[int, list[dict]]:
    return {c: [row(clients=c, **{key: v}, **over) for v in vs] for c, vs in by_c.items()}


def test_targets_are_the_focus_cells():
    ts = loadgen.targets(gridmod.load(), "focus")
    assert len(ts) == 6 * 2 * 2 + 2  # spec 5.1 focus cells; S3 at two sizes, S4 not at all
    assert len({t.cell_id for t in ts}) == len(ts)
    assert {t.servers for t in ts if t.arm in ("M0", "M1")} == {2}


def test_points_follow_load_yaml():
    load = loadgen.yaml.safe_load(loadgen.LOAD_PATH.read_text())
    assert [p.clients for p in loadgen.closed_points(load)] == [1, 2, 4, 8, 16, 32, 64]
    assert {p.workers for p in loadgen.closed_points(load)} == {6}
    assert [p.name for p in loadgen.open_points(load)][:2] == ["open_w6_f0.10", "open_w6_f0.25"]
    assert [(p.workers, p.clients, p.runs) for p in loadgen.multicore_points(load)] == [
        (1, 16, 30), (2, 16, 30), (4, 16, 30), (6, 16, 30)]
    assert Point("multicore", 2, 30, clients=16).loop == "closed"


def test_closed_sweep_runs_counts_in_order():
    assert loadgen.next_clients(LOAD, {}) == 1
    assert loadgen.next_clients(LOAD, closed({1: [0.1, 0.1], 2: [0.2, 0.3]})) == 4


def test_closed_sweep_stops_after_the_first_count_over_ten_times_the_first_p95():
    assert loadgen.next_clients(LOAD, closed({1: [0.1, 0.1], 2: [0.5], 4: [1.0]})) == 8  # 10x is not over
    assert loadgen.next_clients(LOAD, closed({1: [0.1, 0.1], 2: [0.5], 4: [1.01, 1.02]})) is None
    assert loadgen.next_clients(LOAD, closed({1: [0.1, 0.1], 2: [0.1], 4: [0.1], 8: [0.1], 16: [0.1],
                                              32: [0.1]})) is None


def test_closed_sweep_uses_the_median_of_the_runs():
    assert loadgen.next_clients(LOAD, closed({1: [0.1, 0.1, 0.1], 2: [5.0, 0.2, 0.3]})) == 4


def test_closed_sweep_stops_on_an_error_row():
    done = closed({1: [0.1]})
    done[2] = [row(clients=2, status="error", error="x", lat_p95_s=None)]
    assert loadgen.next_clients(LOAD, done) is None


def test_p95_limit_is_ten_times_the_first_count():
    assert loadgen.p95_limit(LOAD, {}) is None
    assert loadgen.p95_limit(LOAD, closed({1: [0.1, 0.3, 0.2]})) == pytest.approx(2.0)


def test_saturation_is_the_highest_median_throughput():
    done = closed({1: [10, 11, 12], 2: [19, 20, 50], 4: [18, 18, 18]}, key="throughput_qps")
    assert loadgen.saturation_qps(done) == 20
    done[8] = [row(clients=8, status="error", throughput_qps=99.0)]
    assert loadgen.saturation_qps(done) == 20
    assert loadgen.saturation_qps({}) is None


def test_g7_checks_the_highest_client_count_of_each_cell():
    rows = [row(clients=1, loadgen_cpu_util=90.0), row(clients=64, loadgen_cpu_util=40.0),
            row(arm="M1", clients=32, loadgen_cpu_util=69.9),
            row(loop="open", clients=None, offered_load_qps=5.0, loadgen_cpu_util=99.0)]
    assert loadgen.g7_problems(rows, 70) == []
    rows.append(row(clients=64, run=1, loadgen_cpu_util=70.0))
    assert len(problems := loadgen.g7_problems(rows, 70)) == 1 and "70.0 % CPU" in problems[0]


def test_g7_fails_without_a_measurement_or_rows():
    assert "no load-generator CPU" in loadgen.g7_problems([row(loadgen_cpu_util=None)], 70)[0]
    assert loadgen.g7_problems([], 70) == ["no closed-loop rows"]
    assert loadgen.g7_problems([row(status="error", error="x")], 70) == ["no closed-loop rows"]


def test_g7_cli(tmp_path, capsys):
    d = tmp_path / "run1" / "loadgen" / "S1_1MiB_32B"
    d.mkdir(parents=True)
    (d / "closed_w6_c64.jsonl").write_text(json.dumps(row(clients=64, loadgen_cpu_util=75.0)) + "\n")
    assert loadgen.main(["--g7", "--run-id", "run1", "--results", str(tmp_path)]) == 1
    assert "G7 FAIL" in capsys.readouterr().out
    (d / "closed_w6_c64.jsonl").write_text(json.dumps(row(clients=64, loadgen_cpu_util=7.0)) + "\n")
    assert loadgen.main(["--g7", "--run-id", "run1", "--results", str(tmp_path)]) == 0


@pytest.mark.parametrize("over,ok", [
    ({}, True),
    ({"loop": "open", "clients": None, "offered_load_qps": 12.5}, True),
    ({"status": "saturated"}, True),
    ({"offered_load_qps": 3.0}, False),  # closed loop has no offered load
    ({"loop": "open", "clients": None}, False),  # open loop needs one
    ({"lat_p99_s": -1.0}, False),
    ({"extra": 1}, False),
])
def test_loadgen_rows_against_the_schema(over, ok):
    r = row(**over)
    assert (validate.check_row(r, reference_platform=PLATFORM) == []) is ok


def test_dry_run_lists_every_point():
    ts = loadgen.targets(gridmod.load(), "focus")
    out = loadgen.dry_run(ts, LOAD, loadgen.EXPERIMENTS)
    assert "closed_w6_c32" in out and "open_w6_f1.10" in out and "multicore_w2_c4" in out
    assert out.count("\n") == 1 + 6 + 2 + 2 + 1
    assert loadgen.bound_s(LOAD, 3, ("closed",)) == 3 * 6 * 2 * 1


SMALL = {
    "seed": 7, "warmup": 1, "mem_cap_gib": 4,
    "timeout_s": {"default": {"default": 60}},
    "adapters": {a: "fake-adapter" for a in gridmod.ARMS},
    "tiers": {"t": {"reps": {"online": 3, "offline": {"default": 2}},
                    "sweep": {"arms": ["S1", "M1"], "db_bytes": ["1MiB"], "record_bytes": ["32B"]}}},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_SERVE", raising=False)
    monkeypatch.delenv("FAKE_LOADGEN", raising=False)
    return tmp_path, log


def make_runner(tmp_path, load=LOAD, loadgen_cmd=None, **kw) -> LoadgenRunner:
    opts = dict(cells=[], run_dir=tmp_path / "raw" / "run1" / "loadgen", run_id="run1", platform=PLATFORM,
                reference_platform=PLATFORM, adapter_dir=tmp_path, mem_cap_bytes=4 << 30, harness_sha="abc1234",
                launcher=[sys.executable, str(FAKE_SERVE)], log=lambda m: None, stop_timeout_s=20,
                load=load, targets=loadgen.targets(SMALL, "t"),
                loadgen_cmd=loadgen_cmd or [sys.executable, str(FAKE_LOADGEN), "loadgen"])
    opts.update(kw)
    opts["run_dir"].mkdir(parents=True, exist_ok=True)
    return LoadgenRunner(**opts)


def loadgen_calls(log: Path) -> list[list[str]]:
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.startswith("[")] if log.exists() else []


def no_fake_alive() -> bool:
    return not any(str(FAKE_SERVE) in " ".join(p.info["cmdline"] or ()) for p in psutil.process_iter(["cmdline"])
                   if p.pid != os.getpid())


def test_driver_runs_every_sweep_and_resumes(env):
    tmp, log = env
    r = make_runner(tmp)
    assert r.run() == {"done": 2}
    for t in r.targets:
        d = r.run_dir / t.cell_id
        # The fake's p95 at W = 6: 0.012 s at C = 1, then 0.02, 0.04, 0.08 and 0.16 > 0.12 at C = 16.
        assert sorted(p.name for p in d.iterdir()) == sorted(
            [f"closed_w6_c{c}.jsonl" for c in (1, 2, 4, 8, 16)]
            + ["open_w6_f0.50.jsonl", "open_w6_f1.10.jsonl", "multicore_w1_c4.jsonl", "multicore_w2_c4.jsonl"])
        rows = loadgen.read_rows(r.run_dir)
        assert all(validate.check_row(x, reference_platform=PLATFORM) == [] for x in rows)
        mine = [x for x in rows if x["arm"] == t.arm]
        assert len(mine) == 9 * 2 and sorted({x["run"] for x in mine}) == [0, 1]
        assert {x["workers"] for x in mine if x["loop"] == "open"} == {6}
        assert sorted({x["workers"] for x in mine if x["clients"] == 4 and x["workers"] < 6}) == [1, 2]
        sat = 6 * 20.0  # W x per_worker_qps, reached from C = 2
        assert sorted(x["offered_load_qps"] for x in mine if x["loop"] == "open") == pytest.approx(
            [0.5 * sat] * 2 + [1.1 * sat] * 2)
        assert [x["status"] for x in mine if x["clients"] == 16] == ["saturated"] * 2
        corpus, client = r.corpus_paths(t)
        assert corpus.exists() and len(client.read_text().splitlines()) == LOAD["corpus_queries"]
    starts = [ln for ln in (r.run_dir / "servers" / "ledger.jsonl").read_text().splitlines()]
    assert [json.loads(s)["workers"] for s in starts] == [6, 1, 2] * 2  # restarts only when W changes
    assert no_fake_alive()

    n = len(loadgen_calls(log))
    assert make_runner(tmp).run() == {"done": 2}
    assert len(loadgen_calls(log)) == n


def test_closed_loop_rows_carry_the_stop_rule_limit(env):
    tmp, log = env
    make_runner(tmp, load={**LOAD, "runs": 1}, experiments=("closed",)).run()
    limits = [(c[c.index("--clients") + 1], c[c.index("--p95-limit-s") + 1] if "--p95-limit-s" in c else None)
              for c in loadgen_calls(log)]
    assert [c for c, lim in limits if lim is None] == ["1", "1"]  # one per cell
    assert [float(lim) for _, lim in limits if lim is not None] == pytest.approx([0.12] * 8)


def test_a_loadgen_crash_leaves_its_point_to_do(env, monkeypatch):
    tmp, log = env
    monkeypatch.setenv("FAKE_LOADGEN", json.dumps({"crash_clients": 4}))
    r = make_runner(tmp, experiments=("closed",))
    assert r.run() == {"failed": 2}
    t = r.targets[0]
    assert r.point_path(t, Point("closed", 6, 2, clients=2)).exists()
    assert not r.point_path(t, Point("closed", 6, 2, clients=4)).exists()
    failed = [json.loads(ln) for ln in (r.run_dir / "ledger.jsonl").read_text().splitlines()]
    assert [e["point"] for e in failed if e["status"] == "failed"] == ["closed_w6_c4"] * 2

    monkeypatch.delenv("FAKE_LOADGEN")
    assert make_runner(tmp, experiments=("closed",)).run() == {"done": 2}
    assert r.point_path(t, Point("closed", 6, 2, clients=16)).exists()
    assert no_fake_alive()


def test_unsupported_servers_skip_the_cell(env, monkeypatch):
    tmp, log = env
    monkeypatch.setenv("FAKE_SERVE", json.dumps({"S1": "unsupported"}))
    assert make_runner(tmp).run() == {"unsupported_params": 1, "done": 1}
    assert not (tmp / "raw" / "run1" / "loadgen" / "S1_1MiB_32B").exists()


def test_rows_from_another_machine_are_refused(env):
    tmp, _ = env
    with pytest.raises(loadgen.ContractError, match="reported numbers come only from"):
        make_runner(tmp, platform="laptop").run()


@pytest.mark.skipif(not REAL_LOADGEN.exists(), reason="build/loadgen/loadgen is not built (make loadgen)")
def test_real_loadgen_against_the_fake_servers(env):
    tmp, _ = env
    load = {**LOAD, "runs": 1, "closed_loop": {**LOAD["closed_loop"], "clients": [1, 4]},
            "open_loop": {**LOAD["open_loop"], "offered_load_fraction": [0.5]},
            "multicore": {**LOAD["multicore"], "workers": [2], "reps": 1}}
    r = make_runner(tmp, load=load, loadgen_cmd=[str(REAL_LOADGEN)])
    assert r.run() == {"done": 2}
    rows = loadgen.read_rows(r.run_dir)
    assert len(rows) == 2 * 4
    assert all(validate.check_row(x, reference_platform=PLATFORM) == [] for x in rows)
    assert all(x["status"] in ("ok", "saturated") and x["mismatched"] == 0 and x["completed"] > 0 for x in rows)
    assert all(x["e2e_lat_p50_s"] > x["lat_p50_s"] for x in rows)
