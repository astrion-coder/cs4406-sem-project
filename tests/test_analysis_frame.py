import json

import pandas as pd
import pytest

from cs4406_sem_project.analysis import amortise, frame, synthetic
from cs4406_sem_project.harness import grid, validate

TIER1 = grid.expand(grid.load(), "tier1")


@pytest.fixture(scope="module")
def rows():
    return synthetic.rows_for_cells(TIER1)


def test_synthetic_rows_validate(rows):
    assert [p for r in rows for p in validate.check_row(r, reference_platform=synthetic.PLATFORM)] == []


def test_synthetic_rows_are_reproducible():
    cell = TIER1[0]
    assert synthetic.rows_for(cell) == synthetic.rows_for(cell)
    assert synthetic.rows_for(cell, seed=1) != synthetic.rows_for(cell)


def test_frame_drops_warmups_and_keeps_every_cell(rows):
    df = frame.rows_to_frame(rows)
    assert len(df) == sum(c.reps for c in TIER1)
    assert len(df.groupby(frame.KEY + ["phase"])) == len(TIER1)
    assert (df.loc[df.arm.isin(["S3", "S4"]), "record_bytes"] == 0).all()


def test_two_server_times_take_the_max_and_the_sum():
    cell = next(c for c in TIER1 if c.arm == "M1" and c.phase == "online")
    row = synthetic.rows_for(cell)[1]
    row["metrics"]["server_answer_s"] = [1.0, 3.0]
    df = frame.rows_to_frame([row])
    assert df.server_answer_s_max.item() == 3.0 and df.server_answer_s_sum.item() == 4.0
    assert df.server_online_s.item() == 3.0


def test_fused_verify_counts_as_zero_in_client_time():
    cell = next(c for c in TIER1 if c.arm == "M1" and c.phase == "online")
    row = synthetic.rows_for(cell)[1]
    m = row["metrics"]
    assert frame.rows_to_frame([row]).client_online_s.item() == pytest.approx(m["query_gen_s"] + m["client_recover_s"])


def test_faulted_and_failed_rows_are_left_out():
    cell = next(c for c in TIER1 if c.arm == "S1" and c.phase == "online")
    rows = synthetic.rows_for(cell)
    rows[1]["faulted"] = True
    rows[2]["status"] = "unsupported_params"
    assert len(frame.rows_to_frame(rows)) == cell.reps - 2


def test_load_runs_refuses_other_platforms(tmp_path):
    cell = next(c for c in TIER1 if c.arm == "S0-go" and c.phase == "online")
    (tmp_path / f"{cell.cell_id}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in synthetic.rows_for(cell)))
    df, status = frame.load_runs([tmp_path], synthetic.PLATFORM)
    assert len(df) == cell.reps and status.empty
    with pytest.raises(frame.RefusedRows, match="reference platform"):
        frame.load_runs([tmp_path], "arm64-darwin-Apple M1 Pro")


def test_load_runs_leaves_out_the_rows_of_a_cell_that_timed_out(tmp_path):
    timed_out = next(c for c in TIER1 if c.arm == "S3" and c.phase == "offline")
    finished = next(c for c in TIER1 if c.arm == "S0-go" and c.phase == "online")
    partial = synthetic.rows_for(timed_out)[:3]
    failure = {
        "run_id": "synthetic", "git_sha_harness": "abc1234", "platform": synthetic.PLATFORM,
        "adapter": "apir_go", "arm": "S3", "cell_id": timed_out.cell_id, "db_bytes": timed_out.db_bytes,
        "record_bytes": timed_out.record_bytes, "phase": "offline", "seed": 1, "fault": "none",
        "fault_prob": 0.0, "recovery": "abort", "reps": timed_out.reps, "warmup": timed_out.warmup,
        "status": "timeout", "error": "no exit within 7200 s", "partial_rows": len(partial),
        "harness": {"exit_code": None, "wall_s": 7200.5, "peak_footprint_bytes": None,
                    "timeout_s": 7200.0, "mem_cap_bytes": 14 << 30, "stderr_tail": ""},
    }
    _write(tmp_path / "run", {timed_out.cell_id: partial + [failure],
                              finished.cell_id: synthetic.rows_for(finished)})
    df, status = frame.load_runs([tmp_path / "run"], synthetic.PLATFORM)
    assert set(df.arm) == {"S0-go"} and len(df) == finished.reps
    assert status.to_dict("records") == [
        {"arm": "S3", "db_bytes": timed_out.db_bytes, "record_bytes": 0, "phase": "offline",
         "status": "timeout", "error": "no exit within 7200 s", "partial_rows": 3}]


FOCUS = grid.expand(grid.load(), "focus")


def _write(run_dir, rows_by_cell):
    run_dir.mkdir(parents=True)
    for cell_id, rows in rows_by_cell.items():
        (run_dir / f"{cell_id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_synthetic_client_rows_validate_as_client_rows():
    cell = next(c for c in FOCUS if c.arm == "M1" and c.phase == "online")
    rows = synthetic.client_rows_for(cell, amortise.load_profiles()["mobile"])
    assert [p for r in rows for p in validate.check_row(r, reference_platform=synthetic.PLATFORM)] == []
    assert all(r["mode"] == "client" and len(r["metrics"]["rtt_s"]) == 2 for r in rows)


def test_load_netem_takes_the_profile_from_the_path(tmp_path):
    cell = next(c for c in FOCUS if c.arm == "S1" and c.phase == "online")
    for name in ("lan", "mobile"):
        prof = amortise.load_profiles()[name]
        _write(tmp_path / "netem" / name, {cell.cell_id: synthetic.client_rows_for(cell, prof)})
    df = frame.load_netem(tmp_path / "netem", synthetic.PLATFORM)
    assert sorted(df.profile.unique()) == ["lan", "mobile"]
    assert (df["mode"] == "client").all() and df.rtt_s_max.notna().all()
    with pytest.raises(frame.RefusedRows, match="reference platform"):
        frame.load_netem(tmp_path / "netem", "arm64-darwin-Apple M1 Pro")


def test_load_netem_carries_the_links_rtt_check(tmp_path):
    cell = next(c for c in FOCUS if c.arm == "S1" and c.phase == "online")
    for name, state in (("lan", {"rtt_checked": "no", "calib_rtt_ms": "0.31"}), ("mobile", None)):
        _write(tmp_path / "netem" / name, {cell.cell_id: synthetic.client_rows_for(cell, amortise.load_profiles()[name])})
        if state:
            (tmp_path / "netem" / name / "link.json").write_text(json.dumps([{"profile": name, "state": state}]))
    df = frame.load_netem(tmp_path / "netem", synthetic.PLATFORM).groupby("profile").first()
    assert not df.loc["lan", "rtt_checked"] and df.loc["lan", "calib_rtt_ms"] == 0.31
    assert df.loc["mobile", "rtt_checked"] and pd.isna(df.loc["mobile", "calib_rtt_ms"])


def test_load_session_reads_the_comparison_and_checks_the_rows(tmp_path):
    cell = next(c for c in FOCUS if c.arm == "S1")
    _write(tmp_path / "session", {cell.cell_id: synthetic.rows_for(cell)})
    assert frame.load_session(tmp_path / "session", synthetic.PLATFORM) is None
    (tmp_path / "session" / "comparison.json").write_text(json.dumps({"rows": [
        {"arm": "S1", "db_bytes": 1, "record_bytes": 1, "q": 100, "quantity": "bytes", "computed": 2.0,
         "measured": None, "rel_diff": None, "flagged": False, "note": "incomplete"}]}))
    table = frame.load_session(tmp_path / "session", synthetic.PLATFORM)
    assert table.computed.item() == 2.0 and table.measured.isna().all()
    with pytest.raises(frame.RefusedRows, match="reference platform"):
        frame.load_session(tmp_path / "session", "arm64-darwin-Apple M1 Pro")


def _loadgen_points():
    from cs4406_sem_project.harness import loadgen

    load = loadgen.yaml.safe_load(loadgen.LOAD_PATH.read_text())
    targets = [t for t in loadgen.targets(grid.load(), "focus") if t.cell_id in ("S1_1GiB_1KiB", "S3_1GiB_1bit")]
    return synthetic.loadgen_points(targets, load)


def _write_loadgen(d, points):
    for (cell, point), rows in points.items():
        (d / cell).mkdir(parents=True, exist_ok=True)
        (d / cell / f"{point}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_synthetic_loadgen_rows_validate():
    rows = [r for rs in _loadgen_points().values() for r in rs]
    assert [p for r in rows for p in validate.check_row(r, reference_platform=synthetic.PLATFORM)] == []


def test_load_loadgen_keeps_the_cell_and_point_from_the_path_and_drops_errors(tmp_path):
    points = _loadgen_points()
    points[("S1_1GiB_1KiB", "closed_w6_c1")][0] |= {"status": "error", "error": "boom"}
    _write_loadgen(tmp_path, points)
    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "S1_1GiB_1KiB.client.jsonl").write_text("not a loadgen row\n")
    df = frame.load_loadgen(tmp_path, synthetic.PLATFORM)
    assert len(df) == sum(len(rs) for rs in points.values()) - 1
    assert set(df.experiment) == {"closed", "open", "multicore"}
    c1 = df[(df.cell_id == "S1_1GiB_1KiB") & (df.point == "closed_w6_c1")]
    assert list(c1.run) == [1, 2] and (c1.clients == 1).all()
    assert (df[df.arm == "S3"].record_bytes == 0).all() and "saturated" in set(df.status)


def test_load_loadgen_refuses_other_platforms(tmp_path):
    _write_loadgen(tmp_path, _loadgen_points())
    with pytest.raises(frame.RefusedRows, match="reference platform"):
        frame.load_loadgen(tmp_path, "arm64-darwin-Apple M1 Pro")
