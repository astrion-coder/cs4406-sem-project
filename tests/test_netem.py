import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import loadgen, netem, validate
from cs4406_sem_project.harness.netem import LinkError, NetemRunner

pytestmark = pytest.mark.usefixtures("on_ac_power")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "cs4406_sem_project" / "harness" / "dummynet.sh"
FAKE = ROOT / "tests" / "fake_serve.py"
PLATFORM = "test-platform"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash")


def dummynet(*args: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, SCRIPT.as_posix(), *args], capture_output=True, text=True,
                          env={**os.environ, **env})


@needs_bash
def test_dummynet_parses():
    assert subprocess.run([BASH, "-n", SCRIPT.as_posix()]).returncode == 0


@needs_bash
@pytest.mark.parametrize("name", sorted(netem.load_profiles()))
def test_pipe_parameters_follow_the_profiles_file(name):
    p = netem.load_profiles()[name]
    out = dummynet("params", name)
    assert out.returncode == 0, out.stderr
    kv = dict(ln.split("=", 1) for ln in out.stdout.splitlines() if "=" in ln and not ln.startswith("dnctl"))
    assert (float(kv["down_mbps"]), float(kv["up_mbps"]), float(kv["rtt_ms"])) == (p.down_mbps, p.up_mbps, p.rtt_ms)
    delay = math.floor(p.rtt_ms / 2)  # half the RTT each way, in dnctl's whole milliseconds
    assert int(kv["delay_ms"]) == delay
    assert kv["delay_exact"] == ("yes" if delay == p.rtt_ms / 2 else "no")
    pipes = [ln for ln in out.stdout.splitlines() if ln.startswith("dnctl")]
    assert pipes == [f"dnctl pipe 44001 config bw {kv['down_mbps']}Mbit/s delay {delay}",
                     f"dnctl pipe 44002 config bw {kv['up_mbps']}Mbit/s delay {delay}"]


@needs_bash
def test_lan_rtt_is_below_dnctl_resolution():
    out = dummynet("params", "lan")
    assert "delay_exact=no" in out.stdout


@needs_bash
def test_rules_shape_only_the_given_ports_both_ways():
    out = dummynet("rules", "5001", "5002")
    assert out.returncode == 0
    assert out.stdout.splitlines() == [
        "dummynet out quick on lo0 proto tcp from any port { 5001 5002 } to any pipe 44001",
        "dummynet out quick on lo0 proto tcp from any to any port { 5001 5002 } pipe 44002",
    ]
    assert dummynet("rules", "70000").returncode != 0
    assert dummynet("rules", "x").returncode != 0


@needs_bash
def test_unknown_profile_is_refused():
    out = dummynet("params", "satellite")
    assert out.returncode != 0 and "no profile 'satellite'" in out.stderr


@needs_bash
@pytest.mark.skipif(sys.platform == "darwin", reason="checks the refusal off macOS")
@pytest.mark.parametrize("args", [("up", "lan"), ("down",), ("status",), ("calibrate", "lan")])
def test_privileged_commands_refuse_off_macos(args, tmp_path):
    out = dummynet(*args, VPIR_DUMMYNET_STATE=str(tmp_path / "state"))
    assert out.returncode == 1 and "only on macOS" in out.stderr
    assert not (tmp_path / "state").exists()


def state_file(tmp_path, **over) -> Path:
    kv = {"pf_token": "123", "profile": "broadband", "down_mbps": "100", "up_mbps": "20", "rtt_ms": "20",
          "delay_ms": "10", "ports": "47001 47002", "up_at": "2026-11-15T10:00:00Z", "calibrated": "pass",
          "calib_rtt_ms": "20.4", "calib_down_mbps": "95.1", "calib_up_mbps": "19.2"}
    kv.update(over)
    path = tmp_path / "state"
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    return path


def test_calibrated_link_gives_its_ports(tmp_path):
    link = netem.read_link(state_file(tmp_path))
    assert netem.listen_ports("broadband", netem.load_profiles(), link) == (47001, 47002)
    assert link.raw["calib_rtt_ms"] == "20.4"


@pytest.mark.parametrize("profile,over,why", [
    ("mobile", {}, "not 'mobile'"),
    ("broadband", {"calibrated": "none"}, "gate G4"),
    ("broadband", {"calibrated": "fail"}, "gate G4"),
    ("broadband", {"up_mbps": "25"}, "differ from configs"),
    ("broadband", {"ports": "47001"}, "two shaped ports"),
    ("none", {}, "needs `dummynet.sh down`"),
    ("wifi", {}, "unknown profile"),
])
def test_wrong_link_is_refused(tmp_path, profile, over, why):
    link = netem.read_link(state_file(tmp_path, **over))
    with pytest.raises(LinkError, match=why):
        netem.listen_ports(profile, netem.load_profiles(), link)


def test_loopback_baseline_needs_no_link(tmp_path):
    assert netem.read_link(tmp_path / "absent") is None
    assert netem.listen_ports("none", netem.load_profiles(), None) == (0, 0)
    with pytest.raises(LinkError, match="no emulated link"):
        netem.listen_ports("lan", netem.load_profiles(), None)


@pytest.mark.parametrize("line,addr", [
    ("vpir-serve ready 127.0.0.1:47001", ("127.0.0.1", 47001)),
    ("vpir-serve ready 127.0.0.1:52133\r", ("127.0.0.1", 52133)),
    ("vpir-serve ready [::1]:8080", ("[::1]", 8080)),
    ("vpir-serve ready 127.0.0.1", None),
    ("layout: 32768 records", None),
    ("  vpir-serve ready 127.0.0.1:1", None),
])
def test_parse_ready(line, addr):
    assert netem.parse_ready(line) == addr


def test_focus_cells_are_client_sessions():
    cells = netem.netem_cells(gridmod.load(), "focus", 30)
    assert len(cells) == 6 * 2 * 2 + 2  # spec 5.1 focus cells; S3 at two sizes, S4 not at all
    assert all(c.phase == "online" and c.reps == 30 and c.warmup == 0 for c in cells)
    assert len({c.cell_id for c in cells}) == len(cells)


def test_rq3_tier_is_the_rq4_cell():
    """Decision 28: RQ3a and RQ3b run on the same cells as RQ6 and RQ7 (load.yaml's `cells`)."""
    load = loadgen.yaml.safe_load(loadgen.LOAD_PATH.read_text())
    rq67 = {(t.arm, t.db_bytes, t.record_bytes)
            for t in loadgen.only_cells(loadgen.targets(gridmod.load(), "focus"), load["cells"])}
    assert {(c.arm, c.db_bytes, c.record_bytes) for c in netem.netem_cells(gridmod.load(), "rq3", 30)} == rq67


SMALL = {
    "seed": 7, "warmup": 1, "mem_cap_gib": 4,
    "timeout_s": {"default": {"default": 60}},
    "adapters": {a: "fake-adapter" for a in gridmod.ARMS},
    "tiers": {"t": {"reps": {"online": 3, "offline": {"default": 2}},
                    "sweep": {"arms": ["S0-go", "M1"], "db_bytes": ["1MiB"], "record_bytes": ["32B"]},
                    "one_bit": {"S3": ["1MiB"]}}},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_SERVE", raising=False)
    return tmp_path, log


def make_runner(tmp_path, grid=SMALL, queries=4, **kw) -> NetemRunner:
    cells = netem.netem_cells(grid, "t", queries)
    opts = dict(cells=cells, run_dir=tmp_path / "raw" / "run1" / "netem" / "none", run_id="run1",
                platform=PLATFORM, reference_platform=PLATFORM, adapter_dir=tmp_path, mem_cap_bytes=4 << 30,
                harness_sha="abc1234", launcher=[sys.executable, str(FAKE)], log=lambda m: None,
                stop_timeout_s=20)
    opts.update(kw)
    opts["run_dir"].mkdir(parents=True, exist_ok=True)
    return NetemRunner(**opts)


def rows(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines()]


def calls(log: Path) -> list[list[str]]:
    return [ln.split() for ln in log.read_text().splitlines()] if log.exists() else []


def no_fake_alive() -> bool:
    return not any(str(FAKE) in " ".join(p.info["cmdline"] or ()) for p in psutil.process_iter(["cmdline"])
                   if p.pid != os.getpid())


def test_servers_start_client_connects_and_servers_stop(env):
    tmp, log = env
    r = make_runner(tmp)
    assert r.run() == {"ok": 3}
    served = {(c[1], c[2]): c[3] for c in calls(log) if c[0] == "serve"}
    clients = {c[1]: c[2] for c in calls(log) if c[0] == "client"}
    assert clients["M1"] == f"{served[('M1', '0')]},{served[('M1', '1')]}"  # each server's own ready address
    assert clients["S0-go"] == served[("S0-go", "None")]
    for c in r.cells:
        out = rows(r.final_path(c))
        assert len(out) == 4 and all(o["mode"] == "client" for o in out)
        assert validate.check_file(r.final_path(c), reference_platform=PLATFORM) == []
    ledger = {e["cell_id"]: e for e in rows(r.run_dir / "servers" / "ledger.jsonl")}
    assert len(ledger["M1_1MiB_32B_online"]["exit_codes"]) == 2
    if os.name != "nt":  # Windows has no SIGTERM handler to write the summary
        assert all(e["exit_codes"] == [0] * len(e["exit_codes"]) for e in ledger.values())
        assert ledger["M1_1MiB_32B_online"]["summary_rows"] == [1, 1]
        summary = r.run_dir / "servers" / "M1_1MiB_32B_online.server1.jsonl"
        assert validate.check_file(summary) == []
    assert no_fake_alive()


def test_listens_on_the_shaped_ports(env):
    tmp, log = env
    import socket

    ports = []
    for _ in range(2):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        s.close()
    r = make_runner(tmp, ports=tuple(ports), profile="broadband")
    assert r.run() == {"ok": 3}
    served = [c[3] for c in calls(log) if c[0] == "serve" and c[1] == "M1"]
    assert served == [f"127.0.0.1:{ports[0]}", f"127.0.0.1:{ports[1]}"]


def test_second_run_does_nothing(env):
    tmp, log = env
    make_runner(tmp).run()
    n = len(calls(log))
    assert make_runner(tmp).run() == {}
    assert len(calls(log)) == n


def test_server_that_dies_before_ready_is_a_crash_and_no_client_runs(env, monkeypatch):
    tmp, log = env
    monkeypatch.setenv("FAKE_SERVE", json.dumps({"M1": "die"}))
    r = make_runner(tmp)
    assert r.run() == {"ok": 2, "crash": 1}
    assert not [c for c in calls(log) if c[0] == "client" and c[1] == "M1"]
    fail = rows(r.run_dir / "M1_1MiB_32B_online.jsonl")[-1]
    assert fail["status"] == "crash" and "did not all become ready" in fail["harness"]["stderr_tail"]
    assert validate.check_file(r.run_dir / "M1_1MiB_32B_online.jsonl", reference_platform=PLATFORM) == []
    assert no_fake_alive()


def test_unsupported_servers_leave_the_verdict_to_the_client(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setenv("FAKE_SERVE", json.dumps({"S0-go": "unsupported"}))
    r = make_runner(tmp)
    assert r.run() == {"ok": 2, "unsupported_params": 1}


def test_server_that_never_gets_ready_times_out(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setenv("FAKE_SERVE", json.dumps({"S3": "silent"}))
    g = json.loads(json.dumps(SMALL))
    g["timeout_s"] = {"default": {"default": 3}}
    r = make_runner(tmp, grid=g)
    assert r.run() == {"ok": 2, "timeout": 1}
    assert no_fake_alive()


def test_servers_over_the_memory_cap_are_oom(env, monkeypatch):
    tmp, _ = env
    monkeypatch.setenv("FAKE_SERVE", json.dumps({"S0-go": "hog"}))
    r = make_runner(tmp, mem_cap_bytes=128 << 20)
    counts = r.run()
    assert counts.get("oom") == 1
    fail = rows(r.run_dir / "S0-go_1MiB_32B_online.jsonl")[-1]
    assert fail["status"] == "oom"


def test_main_refuses_without_the_requested_link(tmp_path, capsys):
    ref = tmp_path / "ref.txt"
    ref.write_text(PLATFORM)
    code = netem.main(["--profile", "mobile", "--state", str(tmp_path / "absent"), "--results", str(tmp_path),
                       "--reference-platform", str(ref)])
    assert code == 2 and "no emulated link" in capsys.readouterr().err
    assert not list(tmp_path.glob("*/netem"))
