import json

import pandas as pd
import pytest

from cs4406_sem_project.analysis import frame, plots, synthetic
from cs4406_sem_project.harness import grid

FIGURES = ["F1_reproduction", "F2_overhead_heatmaps", "F3_absolute_cost", "F4_amortised", "F5_network",
           "F6_breakdown"]
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
    assert all((out / f"{f}.png").exists() for f in FIGURES)
    assert "note" not in pd.read_csv(out / "T4_overheads.csv").columns
    assert (tmp_path / "processed" / "summary.parquet").exists()
