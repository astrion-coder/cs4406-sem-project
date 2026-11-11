"""Tests for the RQ4 driver (harness/recovery.py). Adapters here are a fake script, except for one
live smoke through the real apir-adapter."""

import io
import json
import stat
import sys

import pytest
import yaml

from cs4406_sem_project.harness import memcap, recovery
from cs4406_sem_project.harness.recovery import KINDS, Exec, Run

CONFIG = recovery.load_config(recovery.DEFAULT_CONFIG)
RUNS = recovery.plan(CONFIG)
MiB = 1 << 20


def of(kind, **kw):
    return [r for r in RUNS if r.kind == kind and all(getattr(r, k) == v for k, v in kw.items())]


def test_every_kind_is_planned_and_names_are_unique():
    assert {r.kind for r in RUNS} == set(KINDS)
    assert len({r.name for r in RUNS}) == len(RUNS)


def test_s4_has_no_focus_cell():
    assert not [r for r in RUNS if r.arm == "S4"]


def test_baselines_only_abort():
    for arm in ("S0-go", "S0-cpp", "M0"):
        assert {r.policy for r in RUNS if r.arm == arm} == {"abort"}


def test_re_preprocess_only_for_s1_and_s2():
    assert {r.arm for r in RUNS if r.policy == "re_preprocess"} == {"S1", "S2"}


def test_online_trials_cover_every_fault_policy_and_cell():
    runs = of("trials", arm="S1")
    assert {r.fault for r in runs} == set(CONFIG["faults"]["online"])
    assert {r.policy for r in runs} == {"abort", "switch_replica", "re_preprocess"}
    assert {(r.db_bytes, r.record_bytes) for r in runs} == {(128 * MiB, rb) for rb in (32, 1024)}
    assert {(r.db_bytes, r.record_bytes) for r in of("trials", arm="M1")} == {
        (d, rb) for d in (128 * MiB, 1024 * MiB) for rb in (32, 1024)}
    assert all(r.phase == "online" and r.reps == 100 and r.warmup == 1 and r.fault_prob == 1.0
               and not r.replicas_cheat for r in runs)


def test_arms_over_14_gib_at_1_gib_have_no_1_gib_cell():
    assert {r.arm for r in RUNS if r.db_bytes == 1024 * MiB} == {"S0-go", "S0-cpp", "M0", "M1"}


def test_one_bit_arms_use_record_zero():
    assert {r.record_bytes for r in RUNS if r.arm == "S3"} == {0}
    assert {r.record_bytes for r in RUNS if r.arm == "M1"} == {32, 1024}


def test_offline_faults_run_only_for_their_arms_with_abort():
    runs = of("offline")
    assert {(r.fault, r.arm) for r in runs} == {
        ("flip_hint_bit", a) for a in ("S0-go", "S0-cpp", "S1", "S2")} | {
        ("bad_proof", a) for a in ("S1", "S2")}
    assert all(r.policy == "abort" and r.phase == "offline" and r.reps == 100 for r in runs)


def test_sweep_covers_every_rate_and_replicas_cheat_follows_the_policy():
    for r in of("sweep"):
        assert r.fault == "wrong_record" and r.phase == "online" and r.reps == 1000
        assert r.replicas_cheat == (r.policy == "switch_replica")
    assert {r.fault_prob for r in of("sweep", arm="S3")} == {0, 0.001, 0.01, 0.1, 0.5, 1.0}
    cheat = of("sweep", policy="switch_replica")[0]
    assert "--replicas-cheat" in cheat.argv(recovery.DEFAULT_ADAPTER_DIR, 1, "x.jsonl")
    assert "--replicas-cheat" not in of("sweep", policy="abort")[0].argv(
        recovery.DEFAULT_ADAPTER_DIR, 1, "x.jsonl")


def test_overrides_and_filters():
    runs = recovery.plan(CONFIG, only=["M0", "S4"], kinds=["trials"], trials=3, db_sizes=[MiB],
                         record_sizes=[32])
    assert [(r.arm, r.record_bytes) for r in runs].count(("M0", 32)) == 4
    assert [(r.arm, r.record_bytes) for r in runs].count(("S4", 0)) == 8
    assert all(r.reps == 3 and r.db_bytes == MiB for r in runs)
    big = recovery.plan(CONFIG, only=["S4"], db_sizes=[MiB, 2 * MiB])
    assert {r.db_bytes for r in big} == {MiB}

FAKE = f"""#!{sys.executable}
import json, pathlib, sys
a = sys.argv[1:]
v = lambda k: a[a.index(k) + 1]
n = int(v("--reps")) + int(v("--warmup"))
here = pathlib.Path(__file__).parent
if (here / ("hang-" + v("--arm") + "-" + v("--fault"))).exists():
    import time
    time.sleep(30)
if (here / ("fail-" + v("--arm") + "-" + v("--fault"))).exists():
    n //= 2
    code = 1
else:
    code = 0
with open(v("--out"), "a") as f:
    for i in range(n):
        f.write(json.dumps({{"rep": i}}) + "\\n")
sys.exit(code)
"""


@pytest.fixture
def fake_dir(tmp_path):
    d = tmp_path / "adapters"
    d.mkdir()
    for name in ("simplepir-adapter", "vsp-adapter", "apir-adapter"):
        (d / name).write_text(FAKE)
        (d / name).chmod(d.joinpath(name).stat().st_mode | stat.S_IXUSR)
    return d


def exec_in(tmp_path, fake_dir):
    return Exec(fake_dir, tmp_path / "raw" / "t", seed=1, timeout_s=60, cap_bytes=memcap.GIB,
                env={"PATH": "/usr/bin:/bin"}, allow_unclean=True)


def test_resume_skips_complete_runs_and_reruns_failed_ones_keeping_the_partial(tmp_path, fake_dir):
    runs = recovery.plan(CONFIG, only=["S0-go", "M0"], kinds=["trials"], trials=4, db_sizes=[MiB],
                         record_sizes=[32])
    runs = [r for r in runs if r.fault in ("flip_answer_bit", "wrong_record")]
    assert len(runs) == 4
    ex = exec_in(tmp_path, fake_dir)
    (fake_dir / "fail-M0-wrong_record").touch()
    first = recovery.run_all(runs, ex, log=lambda s: None)
    assert [r["complete"] for r in first] == [True, True, True, False]
    failed = runs[3]
    assert first[3]["exit_code"] == 1 and first[3]["records"] == 2
    assert recovery.settled_names(ex.out_dir / "manifest.jsonl") == {r.name for r in runs[:3]}
    kept = (ex.out_dir / f"{failed.name}.jsonl").read_text()

    (fake_dir / "fail-M0-wrong_record").unlink()
    done_text = (ex.out_dir / f"{runs[0].name}.jsonl").read_text()
    second = recovery.run_all(runs, ex, log=lambda s: None)
    assert [r["name"] for r in second] == [failed.name]
    assert second[0]["complete"] and second[0]["records"] == 5
    assert (ex.out_dir / f"{failed.name}.failed-1.jsonl").read_text() == kept
    assert (ex.out_dir / f"{failed.name}.failed-1.stderr.log").exists()
    assert (ex.out_dir / f"{failed.name}.jsonl").read_text().count("\n") == 5
    assert (ex.out_dir / f"{runs[0].name}.jsonl").read_text() == done_text
    lines = (ex.out_dir / "manifest.jsonl").read_text().splitlines()
    assert len(lines) == 5
    assert recovery.settled_names(ex.out_dir / "manifest.jsonl") == {r.name for r in runs}
    assert not any(json.loads(l)["citable"] for l in lines)


def test_a_timed_out_run_is_recorded_once_and_not_retried(tmp_path, fake_dir):
    runs = recovery.plan(CONFIG, only=["M0"], kinds=["trials"], trials=2, db_sizes=[MiB], record_sizes=[32])
    runs = [r for r in runs if r.fault == "wrong_record"]
    ex = Exec(fake_dir, tmp_path / "raw" / "t", seed=1, timeout_s=2, cap_bytes=memcap.GIB,
              env={"PATH": "/usr/bin:/bin"}, allow_unclean=True)
    (fake_dir / "hang-M0-wrong_record").touch()
    first = recovery.run_all(runs, ex, log=lambda s: None)
    assert [(r["outcome"], r["complete"], r["settled"]) for r in first] == [("timeout", False, True)]
    assert recovery.run_all(runs, ex, log=lambda s: None) == []


def test_dry_run_arithmetic(tmp_path):
    est = tmp_path / "estimates.jsonl"
    rows = [dict(arm="S1", db_bytes=MiB, record_bytes=8192, offline_s=100.0, query_s=10.0),
            dict(arm="S1", db_bytes=MiB, record_bytes=32, offline_s=50.0, query_s=5.0),
            dict(arm="S1", db_bytes=2 * MiB, record_bytes=32, offline_s=80.0, query_s=8.0),
            dict(arm="S1", db_bytes=2 * MiB, record_bytes=8192, offline_s=200.0, query_s=20.0),
            dict(arm="S0-go", db_bytes=MiB, record_bytes=32, offline_s=None, query_s=None)]
    est.write_text("\n".join(json.dumps(r) for r in rows))
    table = recovery.load_estimates(est)
    run = lambda **kw: Run(**{**dict(arm="S1", db_bytes=MiB, record_bytes=32, kind="trials",
                                     fault="flip_answer_bit", policy="abort", fault_prob=1.0,
                                     replicas_cheat=False, phase="online", reps=3, warmup=1), **kw})
    assert recovery.estimate_run_s(run(), table) == (50 + 4 * 5, "measured")
    assert recovery.estimate_run_s(run(policy="switch_replica"), table)[0] == 50 + 4 * 4 * 5
    assert recovery.estimate_run_s(run(policy="re_preprocess"), table)[0] == 50 + 4 * (4 * 5 + 3 * 50)
    assert recovery.estimate_run_s(run(kind="offline", phase="offline"), table)[0] == 4 * 50
    s, how = recovery.estimate_run_s(run(record_bytes=1024), table)
    assert s == 100 + 4 * 10 and "8192" in how
    assert recovery.estimate_run_s(run(db_bytes=4 * MiB), table) is None
    assert recovery.estimate_run_s(run(arm="S0-go"), table) is None

    assert recovery.expected_run_s(run(), table) == 50 + 4 * 5
    assert recovery.expected_run_s(run(policy="switch_replica"), table) == 50 + 4 * 2 * 5
    assert recovery.expected_run_s(run(policy="re_preprocess"), table) == 50 + 4 * (4 * 5 + 3 * 50)
    sweep = run(kind="sweep", policy="re_preprocess", fault_prob=0.5)
    assert recovery.expected_run_s(sweep, table) == 50 + 4 * (1.875 * 5 + 0.875 * 50)
    cheating = run(kind="sweep", policy="switch_replica", fault_prob=0.5, replicas_cheat=True)
    assert recovery.expected_run_s(cheating, table) == 50 + 4 * 1.875 * 5
    assert recovery.expected_run_s(run(kind="sweep", policy="re_preprocess", fault_prob=0.0), table) == 50 + 4 * 5
    assert recovery.expected_run_s(run(kind="offline", phase="offline"), table) == 4 * 50
    assert recovery.expected_run_s(run(arm="S0-go"), table) is None

    runs = [run(), run(policy="switch_replica", record_bytes=1024), run(db_bytes=4 * MiB)]
    out = io.StringIO()
    recovery.dry_run(runs, est, out)
    text = out.getvalue()
    total_s = (50 + 4 * 5) + (100 + 4 * 4 * 10)
    expected_s = (50 + 4 * 5) + (100 + 4 * 2 * 10)
    assert "3 Runs, 12 adapter repetitions" in text
    assert (f"total    upper bound {total_s / 3600:9.1f} h   expected {expected_s / 3600:9.1f} h") in text
    assert "no estimate for ('S1', " in text and "larger measured record size 8192" in text


def test_live_smoke_m0_through_the_real_adapter(tmp_path):
    binary = recovery.DEFAULT_ADAPTER_DIR / "apir-adapter"
    if not binary.is_file():
        pytest.skip("apir-adapter not built; run make build")
    cfg = {**CONFIG, "faults": {"online": ["flip_answer_bit"], "offline": {}},
           "timeout_s": 600, "memory_cap_gib": 2}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    code = recovery.main(["--config", str(config), "--only", "M0", "--kinds", "trials", "--trials", "3",
                          "--db-bytes", str(MiB), "--record-bytes", "32", "--allow-unclean",
                          "--raw-dir", str(tmp_path / "raw"), "--run-id", "smoke"])
    out_dir = tmp_path / "raw" / "smoke"
    assert code == 0
    (line,) = (out_dir / "manifest.jsonl").read_text().splitlines()
    row = json.loads(line)
    assert row["complete"] and not row["citable"] and row["records"] == 4
    records = [json.loads(l) for l in (out_dir / f"{row['name']}.jsonl").read_text().splitlines()]
    assert [r["arm"] for r in records] == ["M0"] * 4
    assert all(r["fault"] == "flip_answer_bit" and r["run_id"] == "smoke" for r in records)
    assert json.loads((out_dir / "meta.json").read_text())["citable"] is False
