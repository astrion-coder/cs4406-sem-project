import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

from cs4406_sem_project.analysis import frame
from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import run_grid, session_check

sys.path.insert(0, str(Path(__file__).parent))
import fake_adapter  # noqa: E402

pytestmark = pytest.mark.usefixtures("on_ac_power")

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_adapter.py"
PLATFORM = "test-platform"


def adapter_rows(arm: str, phase: str, n: int, **metrics) -> list[dict]:
    a = argparse.Namespace(arm=arm, phase=phase, db_bytes=1 << 20, record_bytes=32, seed=7, warmup=0)
    out = []
    for rep in range(n):
        r = fake_adapter.row(a, rep)
        r.update(run_id="r", platform=PLATFORM)
        for key, value in metrics.items():
            r["metrics"][key] = value(rep) if callable(value) else value
        out.append(r)
    return out


OFFLINE = dict(server_preproc_s=10.0, server_preproc_proof_s=None, client_offline_s=2.0,
               offline_down_bytes=1000, offline_up_bytes=0)
ONLINE = dict(query_gen_s=0.01, client_recover_s=0.02, client_verify_s=0.01, server_answer_s=[0.1],
              up_bytes=100, up_bytes_per_server=[100], down_bytes=200, down_bytes_per_server=[200])


def grid_frame(**online) -> pd.DataFrame:
    return frame.rows_to_frame(adapter_rows("S0-go", "offline", 5, **OFFLINE)
                               + adapter_rows("S0-go", "online", 30, **{**ONLINE, **online}))


def session_frame(q=100, **online) -> pd.DataFrame:
    return frame.rows_to_frame(adapter_rows("S0-go", "offline", 1, **OFFLINE)
                               + adapter_rows("S0-go", "online", q, **{**ONLINE, **online}))


def at(table, q, quantity) -> dict:
    return table[(table.q == q) & (table.quantity == quantity)].iloc[0].to_dict()


def test_agreeing_session_is_not_flagged():
    t = session_check.compare(grid_frame(), session_frame())
    server = at(t, 100, "server_s")
    assert server["computed"] == pytest.approx(10.0 + 100 * 0.1)
    assert server["measured"] == pytest.approx(server["computed"])
    client = at(t, 100, "client_s")
    assert client["computed"] == pytest.approx(2.0 + 100 * 0.04)
    assert at(t, 100, "bytes")["computed"] == 1000 + 100 * 300
    assert at(t, 100, "total_s")["computed"] == pytest.approx(server["computed"] + client["computed"])
    assert not t.flagged.any()
    assert set(t.q) == {1, 10, 100}


def test_a_slow_session_is_flagged_above_five_percent():
    slow = session_frame(server_answer_s=lambda rep: [0.2 if rep < 9 else 0.1])  # nine slow queries
    t = session_check.compare(grid_frame(), slow)
    server = at(t, 100, "server_s")
    assert server["rel_diff"] == pytest.approx(0.9 / 20.0)
    assert not server["flagged"]
    slower = session_frame(server_answer_s=lambda rep: [0.2 if rep < 11 else 0.1])
    t = session_check.compare(grid_frame(), slower)
    assert at(t, 100, "server_s")["flagged"]
    assert at(t, 100, "total_s")["rel_diff"] == pytest.approx(1.1 / 26.0)
    assert not at(t, 100, "bytes")["flagged"]
    assert not t[t.q != 100].flagged.any()  # only Q = 100 is the spec's check


def test_median_not_mean_is_the_model():
    skewed = grid_frame(server_answer_s=lambda rep: [10.0 if rep == 0 else 0.1])
    assert at(session_check.compare(skewed, session_frame()), 100, "server_s")["rel_diff"] == pytest.approx(0)


def test_measured_q_uses_the_first_q_queries():
    t = session_check.compare(grid_frame(), session_frame(server_answer_s=lambda rep: [0.3 if rep == 0 else 0.1]))
    assert at(t, 1, "server_s")["measured"] == pytest.approx(10.3)
    assert at(t, 10, "server_s")["measured"] == pytest.approx(10.0 + 0.3 + 9 * 0.1)


def test_incomplete_session_is_reported_not_compared():
    t = session_check.compare(grid_frame(), session_frame(q=60))
    row = at(t, 100, "server_s")
    assert math.isnan(row["measured"]) and not row["flagged"] and "60 of 100" in row["note"]
    missing = session_check.compare(grid_frame(), frame.rows_to_frame([]))
    assert "0 offline and 0 of 100" in at(missing, 100, "bytes")["note"]


def test_session_cells_are_cold_and_unwarmed():
    cells = session_check.session_cells(gridmod.load(), "focus", 100)
    grid = {c.cell_id: c for c in gridmod.expand(gridmod.load(), "focus")}
    assert len(cells) == len(grid)
    for c in cells:
        assert c.warmup == 0
        assert c.reps == (1 if c.phase == "offline" else 100)
        g = grid[c.cell_id]
        assert c.timeout_s == (g.timeout_s if c.phase == "offline" else g.timeout_s * 100 / g.expected_rows)


SMALL = {
    "seed": 7, "warmup": 1, "mem_cap_gib": 4,
    "timeout_s": {"default": {"default": 60}},
    "adapters": {a: "fake-adapter" for a in gridmod.ARMS},
    "tiers": {"focus": {"reps": {"online": 3, "offline": {"default": 2}},
                        "sweep": {"arms": ["S0-go", "M1"], "db_bytes": ["1MiB"], "record_bytes": ["32B"]}}},
}


def test_end_to_end_with_the_fake_adapter(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.delenv("FAKE_ADAPTER", raising=False)
    grid_path = tmp_path / "grid.yaml"
    grid_path.write_text(json.dumps(SMALL))
    ref = tmp_path / "ref.txt"
    ref.write_text(PLATFORM)
    monkeypatch.setattr(run_grid, "current_platform", lambda: PLATFORM)
    common = ["--grid", str(grid_path), "--results", str(tmp_path), "--reference-platform", str(ref),
              "--launcher", f'"{sys.executable}" "{FAKE}"']
    assert run_grid.main(["--tier", "focus", *common]) == 0
    grid_run = next(p.name for p in tmp_path.iterdir() if p.is_dir() and "_focus_" in p.name)

    assert session_check.main(["--grid-run", grid_run, "--run-id", "p6", "--queries", "5", *common]) == 0
    out = capsys.readouterr().out
    assert "every total agrees" in out
    session_dir = tmp_path / "p6" / "session"
    assert len(list(session_dir.glob("*_online.jsonl"))) == 2
    report = json.loads((session_dir / "comparison.json").read_text())
    assert report["grid_run"] == grid_run and len(report["rows"]) == 2 * 3 * 4
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert sum(1 for c in calls if c.endswith("online")) == 2 * 2  # grid and session, per arm
