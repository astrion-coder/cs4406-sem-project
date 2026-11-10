import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest
import yaml

from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import run_grid
from cs4406_sem_project.harness import validate
from cs4406_sem_project.harness.run_grid import ContractError, HygieneError, Runner

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_adapter.py"
PLATFORM = "test-platform"


def test_tier1_has_the_spec_cell_count():
    cells = gridmod.expand(gridmod.load(), "tier1")
    assert len({c.cell_id.rsplit("_", 1)[0] for c in cells}) == 103  # spec 5
    assert len(cells) == 206


def test_spec_repetitions_and_caps():
    cells = {c.cell_id: c for c in gridmod.expand(gridmod.load(), "tier1")}
    assert cells["S1_1GiB_1KiB_offline"].reps == 3
    assert cells["S1_128MiB_1KiB_offline"].reps == 5
    assert cells["S1_1GiB_1KiB_online"].reps == 30
    assert all(c.warmup == 1 for c in cells.values())
    assert cells["S4_1MiB_1bit_offline"].timeout_s == 45 * 60  # decision 10
    assert {c.db_bytes for c in cells.values() if c.arm == "S4"} == {64 << 10, 256 << 10, 1 << 20}
    assert {c.db_bytes for c in cells.values() if c.arm == "S3"} == {1 << 20, 16 << 20, 128 << 20, 1 << 30}


def test_one_bit_cells_send_record_bytes_zero():
    c = next(c for c in gridmod.expand(gridmod.load(), "smoke") if c.arm == "S3")
    args = c.adapter_args(Path("o.jsonl"))
    assert args[args.index("--record-bytes") + 1] == "0"
    assert c.record_bytes is None and c.retrieved_bits == 1 and c.n_records == 8 * c.db_bytes


def test_smoke_tier_covers_every_arm():
    assert {c.arm for c in gridmod.expand(gridmod.load(), "smoke")} == set(gridmod.ARMS)


@pytest.mark.parametrize("text,n", [("32B", 32), ("1KiB", 1024), ("16MiB", 16 << 20), ("1GiB", 1 << 30), (7, 7)])
def test_parse_size(text, n):
    assert gridmod.parse_size(text) == n
    assert gridmod.parse_size(gridmod.fmt_size(n)) == n


@pytest.mark.parametrize("bad", ["1MB", "1.5MiB", "MiB", "1 mib"])
def test_parse_size_rejects(bad):
    with pytest.raises(ValueError):
        gridmod.parse_size(bad)


def test_one_bit_arm_in_sweep_is_rejected():
    g = gridmod.load()
    g["tiers"]["smoke"]["sweep"]["arms"].append("S3")
    with pytest.raises(ValueError, match="one bit"):
        gridmod.expand(g, "smoke")


SMALL = {
    "seed": 7, "warmup": 1, "mem_cap_gib": 4,
    "timeout_s": {"default": {"default": 60}},
    "adapters": {a: "fake-adapter" for a in gridmod.ARMS},
    "tiers": {"t": {"reps": {"online": 3, "offline": {"default": 2}},
                    "sweep": {"arms": ["S0-go", "M1"], "db_bytes": ["1MiB"], "record_bytes": ["32B", "1KiB"]},
                    "one_bit": {"S3": ["1MiB"]}}},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_ADAPTER", raising=False)
    return tmp_path, log


def make_runner(tmp_path, grid=SMALL, **kw) -> Runner:
    cells = gridmod.expand(grid, "t")
    run_dir = tmp_path / "raw" / "run1"
    run_grid.write_plan(run_dir, cells, {"tier": "t"})
    opts = dict(cells=cells, run_dir=run_dir, run_id="run1", platform=PLATFORM, reference_platform=PLATFORM,
                adapter_dir=tmp_path, mem_cap_bytes=4 << 30, harness_sha="abc1234",
                launcher=[sys.executable, str(FAKE)], log=lambda m: None)
    opts.update(kw)
    return Runner(**opts)


def rows(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines()]


def test_runs_every_cell_once_and_rows_validate(env):
    tmp, log = env
    r = make_runner(tmp)
    assert r.run() == {"ok": 10}
    assert len(log.read_text().splitlines()) == 10
    for c in r.cells:
        out = rows(r.final_path(c))
        assert len(out) == c.expected_rows
        assert all(o["run_id"] == "run1" and o["platform"] == PLATFORM for o in out)
        assert validate.check_file(r.final_path(c), reference_platform=PLATFORM) == []
    ledger = rows(r.run_dir / "ledger.jsonl")
    assert [e["status"] for e in ledger] == ["ok"] * 10
    assert all(e["peak_footprint_bytes"] > 0 for e in ledger)
    assert not list(r.run_dir.glob("*.part"))


def test_second_run_does_nothing(env):
    tmp, log = env
    make_runner(tmp).run()
    assert make_runner(tmp).run() == {}
    assert len(log.read_text().splitlines()) == 10


@pytest.mark.parametrize("behaviour,status,partial", [("crash", "crash", 1), ("short", "crash", 1)])
def test_crash_is_a_recorded_result(env, monkeypatch, behaviour, status, partial):
    tmp, _ = env
    monkeypatch.setenv("FAKE_ADAPTER", json.dumps({"M1_1048576_32_online": behaviour}))
    r = make_runner(tmp)
    assert r.run() == {"ok": 9, status: 1}
    out = rows(r.run_dir / "M1_1MiB_32B_online.jsonl")
    assert len(out) == partial + 1
    fail = out[-1]
    assert fail["status"] == status and fail["partial_rows"] == partial
    assert validate.kind(fail) == "failure"
    assert validate.check_row(fail, reference_platform=PLATFORM) == []


def test_timeout_kills_and_records(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setenv("FAKE_ADAPTER", json.dumps({"S3_1048576_0_offline": "hang"}))
    g = json.loads(json.dumps(SMALL))
    g["timeout_s"] = {"default": {"default": 60}, "S3": 3}
    r = make_runner(tmp, grid=g)
    t0 = time.monotonic()
    counts = r.run()
    assert counts["timeout"] == 1 and time.monotonic() - t0 < 60
    fail = rows(r.run_dir / "S3_1MiB_1bit_offline.jsonl")[-1]
    assert fail["status"] == "timeout" and fail["harness"]["timeout_s"] == 3


def test_memory_cap_records_oom(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setenv("FAKE_ADAPTER", json.dumps({"S0-go_1048576_32_offline": "oom"}))
    r = make_runner(tmp, mem_cap_bytes=48 << 20)
    counts = r.run()
    assert counts.get("oom") == 1, counts
    fail = rows(r.run_dir / "S0-go_1MiB_32B_offline.jsonl")[-1]
    assert fail["status"] == "oom" and fail["harness"]["peak_footprint_bytes"] > 48 << 20


def test_unsupported_params_is_a_result(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setenv("FAKE_ADAPTER", json.dumps({"S3_": "unsupported"}))
    r = make_runner(tmp)
    assert r.run() == {"ok": 8, "unsupported_params": 2}
    out = rows(r.run_dir / "S3_1MiB_1bit_online.jsonl")
    assert [o["status"] for o in out] == ["unsupported_params"]


class BadWatch:
    problems = ["the machine slept for about 600 s during the run"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def record(self):
        return {"problems": self.problems}


def test_hygiene_failure_stops_and_keeps_cell_pending(env):
    tmp, log = env
    r = make_runner(tmp, watch=BadWatch)
    with pytest.raises(HygieneError, match="slept"):
        r.run()
    assert len(log.read_text().splitlines()) == 1
    assert len(r.pending()) == 10
    assert len(list((r.run_dir / "rejected").iterdir())) == 1
    assert rows(r.run_dir / "ledger.jsonl")[0]["status"] == "hygiene_failed"


def test_wrong_platform_runs_nothing(env):
    tmp, log = env
    r = make_runner(tmp, platform="x86_64-linux-other")
    with pytest.raises(ContractError, match="only from"):
        r.run()
    assert not log.exists()


def test_resume_refuses_a_changed_grid(tmp_path):
    cells = gridmod.expand(SMALL, "t")
    run_grid.write_plan(tmp_path, cells, {"tier": "t"})
    run_grid.write_plan(tmp_path, cells, {"tier": "t"})
    with pytest.raises(ContractError, match="differs"):
        run_grid.write_plan(tmp_path, cells[:-1], {"tier": "t"})


def test_dry_run_prints_plan_and_bound(tmp_path, capsys):
    gpath = tmp_path / "grid.yaml"
    gpath.write_text(yaml.safe_dump(SMALL))
    assert run_grid.main(["--grid", str(gpath), "--tier", "t", "--dry-run", "--results", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "S0-go_1MiB_32B_offline" in out
    assert "10 (cell, phase) runs to do" in out
    assert "upper bound 0.2 h" in out


def test_resume(tmp_path):
    gpath = tmp_path / "grid.yaml"
    gpath.write_text(yaml.safe_dump(SMALL))
    ref = tmp_path / "reference_platform.txt"
    ref.write_text(run_grid.current_platform() + "\n")
    results, log = tmp_path / "raw", tmp_path / "calls.log"
    env = {**os.environ, "FAKE_LOG": str(log), "FAKE_SLEEP": "0.15"}
    env.pop("FAKE_ADAPTER", None)
    common = ["--grid", str(gpath), "--results", str(results), "--reference-platform", str(ref),
              "--adapter-dir", str(tmp_path), "--launcher", f'"{sys.executable}" "{FAKE}"']
    cli = [sys.executable, "-m", "cs4406_sem_project.harness.run_grid"]

    first = subprocess.Popen([*cli, "--tier", "t", *common], env=env, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    run_dir = None
    while time.monotonic() < deadline:
        dirs = list(results.glob("*_t_*")) if results.exists() else []
        if dirs and len(list(dirs[0].glob("*_*_*_*.jsonl"))) >= 3:
            run_dir = dirs[0]
            break
        time.sleep(0.05)
    first.kill()
    first.wait()
    assert run_dir is not None, "first run made no progress"

    done_before = {p.stem for p in run_dir.glob("*_*_*_*.jsonl")}
    assert 0 < len(done_before) < 10
    mtimes = {p.name: p.stat().st_mtime_ns for p in run_dir.glob("*_*_*_*.jsonl")}

    second = subprocess.run([*cli, "--resume", run_dir.name, *common], env=env, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    plan = {c["cell_id"]: c for c in json.loads((run_dir / "plan.json").read_text())}
    assert {p.stem for p in run_dir.glob("*_*_*_*.jsonl")} == set(plan)
    assert not list(run_dir.glob("*.part"))

    def cell_id(call: str) -> str:
        arm, db, rec, phase = call.split()
        rec_s = "1bit" if arm in ("S3", "S4") else gridmod.fmt_size(int(rec))
        return f"{arm}_{gridmod.fmt_size(int(db))}_{rec_s}_{phase}"

    runs = Counter(cell_id(c) for c in log.read_text().splitlines())
    assert all(runs[cid] == 1 for cid in done_before), runs
    assert all(runs[cid] >= 1 for cid in plan)
    assert sum(n - 1 for n in runs.values()) <= 1, runs
    for p in run_dir.glob("*_*_*_*.jsonl"):
        if p.name in mtimes:
            assert p.stat().st_mtime_ns == mtimes[p.name]
    for p in run_dir.glob("*_*_*_*.jsonl"):
        assert validate.check_file(p, reference_platform=run_grid.current_platform()) == []
