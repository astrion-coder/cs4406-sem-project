import json

import pandas as pd
import pytest

from cs4406_sem_project.analysis import aggregate, amortise, frame, plots, synthetic
from cs4406_sem_project.harness import grid, loadgen, session_check

GRID_FIGURES = ["F1_reproduction", "F2_overhead_heatmaps", "F3_absolute_cost", "F4_amortised", "F5_network",
                "F6_breakdown"]
FIGURES = GRID_FIGURES + ["F8_session", "F9_emulated_network", "F10_load_latency", "F11_cores"]
TABLES = ["T1_machine", "T2_scheme_params", "T3_reproduction", "T4_overheads"]


@pytest.fixture(scope="module")
def synthetic_out(tmp_path_factory):
    out = tmp_path_factory.mktemp("figures")
    plots.main(["--synthetic", "tier1", "--out", str(out), "--sysinfo", str(out / "absent.json")])
    return out


@pytest.mark.parametrize("name", FIGURES)
@pytest.mark.parametrize("ext", ["pdf", "png"])
def test_synthetic_tier1_writes_every_figure(synthetic_out, name, ext):
    path = synthetic_out / f"{name}.{ext}"
    assert path.stat().st_size > 1000


@pytest.mark.parametrize("name", TABLES)
def test_synthetic_tables_are_written_and_marked(synthetic_out, name):
    csv = pd.read_csv(synthetic_out / f"{name}.csv")
    md = (synthetic_out / f"{name}.md").read_text(encoding="utf-8")
    assert len(csv) > 0 and (csv.note == plots.SYNTHETIC_MARK).all()
    assert plots.SYNTHETIC_MARK in md and md.count("\n|") >= 3


def test_t1_says_when_sysinfo_is_missing(synthetic_out):
    assert "missing" in pd.read_csv(synthetic_out / "T1_machine.csv").value.item()


def test_t1_reads_sysinfo_when_present(tmp_path):
    path = tmp_path / "sysinfo.json"
    path.write_text(json.dumps({"machine": {"cpu_model": "Apple M1 Pro"}}))
    t1 = plots.t1_machine(path)
    assert t1.set_index("key").value["machine.cpu_model"] == "Apple M1 Pro"


def test_synthetic_processed_tables_go_beside_the_figures(synthetic_out):
    ov = pd.read_parquet(synthetic_out / "processed" / "overheads.parquet")
    assert {"S1/S0-cpp", "S2/S0-cpp", "M1/M0", "S0-go/S0-cpp"} <= set(ov.pair)


def _write_run(run, cells):
    run.mkdir()
    for c in cells:
        (run / f"{c.cell_id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in synthetic.rows_for(c)))


def test_runs_from_another_platform_are_refused(tmp_path):
    _write_run(tmp_path / "run", [c for c in grid.expand(grid.load(), "smoke") if c.arm == "S1"])
    ref = tmp_path / "reference_platform.txt"
    ref.write_text("arm64-darwin-Apple M1 Pro\n")
    with pytest.raises(frame.RefusedRows, match="reference platform"):
        plots.main(["--runs", str(tmp_path / "run"), "--out", str(tmp_path / "out"),
                    "--reference-platform", str(ref)])
    assert not (tmp_path / "out").exists()


def test_runs_on_the_reference_platform_render_unmarked(tmp_path):
    _write_run(tmp_path / "run", grid.expand(grid.load(), "smoke"))
    ref = tmp_path / "reference_platform.txt"
    ref.write_text(synthetic.PLATFORM + "\n")
    out = tmp_path / "out"
    plots.main(["--runs", str(tmp_path / "run"), "--out", str(out), "--processed", str(tmp_path / "processed"),
                "--reference-platform", str(ref), "--sysinfo", str(tmp_path / "absent.json")])
    assert all((out / f"{f}.png").exists() for f in GRID_FIGURES)
    assert "note" not in pd.read_csv(out / "T4_overheads.csv").columns
    assert (tmp_path / "processed" / "summary.parquet").exists()


def test_synthetic_f9_table_recovers_the_planted_factor(synthetic_out):
    em = pd.read_parquet(synthetic_out / "processed" / "emulation.parquet")
    assert set(em.profile) == set(synthetic.TRUE_EMULATION_FACTOR)
    for profile, factor in synthetic.TRUE_EMULATION_FACTOR.items():
        ratios = em[em.profile == profile].emulated_over_modelled
        assert ratios.notna().all() and ratios.median() == pytest.approx(factor, rel=0.05)


def test_f8_computed_totals_are_amortise_times_q(synthetic_out):
    totals = pd.read_parquet(synthetic_out / "processed" / "session_totals.parquet")
    med = aggregate.medians(pd.read_parquet(synthetic_out / "processed" / "summary.parquet"))
    assert set(totals.q) == {1, 10, 100, 1000} and totals.computed.notna().all()
    for r in totals.sample(40, random_state=0).itertuples():
        cell = plots._cell(med, r.arm, r.db_bytes, r.record_bytes)
        assert r.computed == pytest.approx(r.q * amortise.AMORTISED[r.quantity](cell, r.q))
    measured = totals.dropna(subset=["measured"])
    assert set(measured.q) == {1, 10, 100}  # a session of 100 cannot measure Q = 1000


def _p6_cells():
    def keep(c):
        return c.arm in ("S0-cpp", "S1", "M0", "M1") and c.db_bytes == 1 << 30 and c.record_bytes == 1024

    g = grid.load()
    return ([c for c in grid.expand(g, "focus") if keep(c)],
            [c for c in session_check.session_cells(g, "focus", session_check.QUERIES) if keep(c)])


def _write_session(tmp_path, grid_run: str):
    grid_cells, session_cells = _p6_cells()
    _write_run(tmp_path / "run", grid_cells)
    _write_run(tmp_path / "session", session_cells)
    grid_df = frame.load_runs([tmp_path / "run"], synthetic.PLATFORM)[0]
    session_df = frame.load_runs([tmp_path / "session"], synthetic.PLATFORM)[0]
    rows = json.loads(session_check.compare(grid_df, session_df).to_json(orient="records"))
    (tmp_path / "session" / "comparison.json").write_text(json.dumps({"grid_run": grid_run, "rows": rows}))
    ref = tmp_path / "reference_platform.txt"
    ref.write_text(synthetic.PLATFORM + "\n")
    return grid_cells


def test_session_from_another_grid_run_is_refused(tmp_path):
    _write_session(tmp_path, grid_run="some-other-run")
    with pytest.raises(SystemExit):
        plots.main(["--runs", str(tmp_path / "run"), "--session", str(tmp_path / "session"),
                    "--out", str(tmp_path / "out"), "--processed", str(tmp_path / "processed"),
                    "--reference-platform", str(tmp_path / "reference_platform.txt")])
    assert not (tmp_path / "out" / "F8_session.png").exists()


def test_session_and_netem_inputs_render_f8_and_f9_unmarked(tmp_path):
    grid_cells = _write_session(tmp_path, grid_run="run")
    profile = amortise.load_profiles()["broadband"]
    netem = tmp_path / "netem" / "broadband"
    netem.mkdir(parents=True)
    for c in grid_cells:
        if c.phase == "online":
            (netem / f"{c.cell_id}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in synthetic.client_rows_for(c, profile)))
    ref = tmp_path / "reference_platform.txt"
    out = tmp_path / "out"
    plots.main(["--runs", str(tmp_path / "run"), "--session", str(tmp_path / "session"),
                "--netem", str(tmp_path / "netem"), "--out", str(out), "--processed", str(tmp_path / "processed"),
                "--reference-platform", str(ref), "--sysinfo", str(tmp_path / "absent.json")])
    assert (out / "F8_session.png").stat().st_size > 1000 and (out / "F9_emulated_network.pdf").stat().st_size > 1000
    em = pd.read_parquet(tmp_path / "processed" / "emulation.parquet")
    assert set(em.profile) == {"broadband"} and set(em.arm) == {"S0-cpp", "S1", "M0", "M1"}


def test_missing_inputs_skip_f8_and_f9_with_a_note(tmp_path, capsys):
    _write_run(tmp_path / "run", grid.expand(grid.load(), "smoke"))
    (tmp_path / "session").mkdir()  # session_check has not written comparison.json yet
    ref = tmp_path / "reference_platform.txt"
    ref.write_text(synthetic.PLATFORM + "\n")
    out = tmp_path / "out"
    plots.main(["--runs", str(tmp_path / "run"), "--session", str(tmp_path / "session"), "--out", str(out),
                "--processed", str(tmp_path / "processed"), "--reference-platform", str(ref),
                "--sysinfo", str(tmp_path / "absent.json")])
    err = capsys.readouterr().err
    assert "skipped F8_session" in err and "skipped F9_emulated_network" in err
    assert "skipped F10_load_latency" in err and "skipped F11_cores" in err
    assert not (out / "F8_session.png").exists() and not (out / "F9_emulated_network.png").exists()
    assert (out / "F4_amortised.png").exists()


def test_synthetic_f10_marks_the_planted_saturation(synthetic_out):
    sat = pd.read_parquet(synthetic_out / "processed" / "saturation.parquet")
    curves = pd.read_parquet(synthetic_out / "processed" / "load_curves.parquet")
    assert len(sat) == curves.cell_id.nunique() == len(loadgen.targets(grid.load(), "focus"))
    for r in sat.itertuples():
        planted = synthetic.planted_capacity_qps(r.arm, r.db_bytes, r.workers)
        assert r.throughput_qps == pytest.approx(planted, rel=0.05), r.cell_id


def test_synthetic_f11_recovers_the_planted_overhead_at_every_w(synthetic_out):
    ov = pd.read_parquet(synthetic_out / "processed" / "core_scaling.parquet")
    assert set(ov.workers) == {1, 2, 4, 6} and set(ov.arm) == set(synthetic.TRUE_SERVER_OVERHEAD)
    for r in ov.itertuples():
        assert r.overhead == pytest.approx(synthetic.TRUE_SERVER_OVERHEAD[r.arm], rel=0.08)


def _write_loadgen(d, platform: str = synthetic.PLATFORM):
    load = loadgen.yaml.safe_load(loadgen.LOAD_PATH.read_text())
    targets = [t for t in loadgen.targets(grid.load(), "focus") if t.arm in ("S0-cpp", "S1")]
    for (cell, point), rows in synthetic.loadgen_points(targets, load).items():
        (d / cell).mkdir(parents=True, exist_ok=True)
        (d / cell / f"{point}.jsonl").write_text("".join(json.dumps(r | {"platform": platform}) + "\n" for r in rows))


def test_loadgen_input_renders_f10_and_f11_unmarked(tmp_path):
    _write_run(tmp_path / "run", grid.expand(grid.load(), "smoke"))
    _write_loadgen(tmp_path / "loadgen")
    ref = tmp_path / "reference_platform.txt"
    ref.write_text(synthetic.PLATFORM + "\n")
    out = tmp_path / "out"
    plots.main(["--runs", str(tmp_path / "run"), "--loadgen", str(tmp_path / "loadgen"), "--out", str(out),
                "--processed", str(tmp_path / "processed"), "--reference-platform", str(ref),
                "--sysinfo", str(tmp_path / "absent.json")])
    assert (out / "F10_load_latency.png").stat().st_size > 1000 and (out / "F11_cores.pdf").stat().st_size > 1000
    assert set(pd.read_parquet(tmp_path / "processed" / "core_scaling.parquet").pair) == {"S1/S0-cpp"}


def test_loadgen_rows_from_another_platform_are_refused(tmp_path):
    _write_run(tmp_path / "run", grid.expand(grid.load(), "smoke"))
    _write_loadgen(tmp_path / "loadgen", platform="arm64-darwin-Apple M1 Pro")
    ref = tmp_path / "reference_platform.txt"
    ref.write_text(synthetic.PLATFORM + "\n")
    with pytest.raises(frame.RefusedRows, match="reference platform"):
        plots.main(["--runs", str(tmp_path / "run"), "--loadgen", str(tmp_path / "loadgen"),
                    "--out", str(tmp_path / "out"), "--processed", str(tmp_path / "processed"),
                    "--reference-platform", str(ref)])
    assert not (tmp_path / "out").exists()
