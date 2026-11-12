import json

import numpy as np
import pandas as pd
import pytest

from cs4406_sem_project.analysis import aggregate, frame, synthetic
from cs4406_sem_project.harness import grid

TIER1 = grid.expand(grid.load(), "tier1")


@pytest.fixture(scope="module")
def df():
    return frame.rows_to_frame(synthetic.rows_for_cells(TIER1))


@pytest.fixture(scope="module")
def server_overheads(df):
    pairs = [p for p in aggregate.PAIRS if p.num in synthetic.TRUE_SERVER_OVERHEAD]
    ov = aggregate.overheads(df, pairs, resamples=2_000)
    return ov[ov.metric == "server_online_s"]


def test_every_price_pair_and_cell_gets_a_server_ratio(server_overheads):
    cells = {(c.db_bytes, c.record_bytes) for c in TIER1 if c.arm == "S1"}
    for arm in synthetic.TRUE_SERVER_OVERHEAD:
        got = server_overheads[server_overheads.num == arm]
        assert set(zip(got.db_bytes, got.record_bytes)) == cells
        assert (got.n_pairs == 30).all()


def test_median_ratio_recovers_the_planted_server_overhead(server_overheads):
    truth = server_overheads.num.map(synthetic.TRUE_SERVER_OVERHEAD)
    assert ((server_overheads.ratio / truth - 1).abs() < 0.03).all()


def test_bootstrap_ci_covers_the_planted_server_overhead(server_overheads):
    truth = server_overheads.num.map(synthetic.TRUE_SERVER_OVERHEAD)
    covered = (server_overheads.ci_lo <= truth) & (truth <= server_overheads.ci_hi)
    assert (server_overheads.ci_lo <= server_overheads.ratio).all()
    assert (server_overheads.ratio <= server_overheads.ci_hi).all()
    assert covered.mean() >= 0.9


def test_bootstrap_is_deterministic_and_seed_dependent():
    rng = np.random.default_rng(1)
    a, b = rng.lognormal(0, 0.1, 30) * 2, rng.lognormal(0, 0.1, 30)
    assert aggregate.paired_bootstrap(a, b, seed=7) == aggregate.paired_bootstrap(a, b, seed=7)
    assert aggregate.paired_bootstrap(a, b, seed=7)[1:] != aggregate.paired_bootstrap(a, b, seed=8)[1:]


def test_bootstrap_pairs_by_repetition():
    drift = np.linspace(1, 3, 30)  # shared per-rep drift, e.g. thermal throttling
    point, lo, hi = aggregate.paired_bootstrap(2 * drift, drift)
    assert point == lo == hi == pytest.approx(2.0)


def test_summary_has_median_p95_and_n(df):
    s = aggregate.summarise(df)
    cell = s[(s.arm == "S1") & (s.db_bytes == 1 << 20) & (s.record_bytes == 32) & (s.metric == "server_online_s")]
    vals = df[(df.arm == "S1") & (df.db_bytes == 1 << 20) & (df.record_bytes == 32) & (df.phase == "online")]
    assert cell["median"].item() == pytest.approx(vals.server_online_s.median())
    assert cell["p95"].item() == pytest.approx(vals.server_online_s.quantile(0.95))
    assert cell["n"].item() == 30


def test_zero_baseline_gives_no_ratio():
    point, lo, hi = aggregate.paired_bootstrap(np.ones(5), np.zeros(5))
    assert np.isnan(point) and np.isnan(lo) and np.isnan(hi)


def test_cli_writes_processed_tables(tmp_path):
    cells = [c for c in TIER1 if c.db_bytes == 1 << 20 and c.arm in ("S1", "S0-cpp")]
    run = tmp_path / "run"
    run.mkdir()
    for c in cells:
        (run / f"{c.cell_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in synthetic.rows_for(c)))
    ref = tmp_path / "ref.txt"
    ref.write_text(synthetic.PLATFORM + "\n")
    aggregate.main(["--runs", str(run), "--out", str(tmp_path / "out"), "--reference-platform", str(ref)])
    ov = pd.read_parquet(tmp_path / "out" / "overheads.parquet")
    assert set(ov.pair) == {"S1/S0-cpp"} and len(ov) > 0
