import json

import pytest

from cs4406_sem_project.analysis import frame, synthetic
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
