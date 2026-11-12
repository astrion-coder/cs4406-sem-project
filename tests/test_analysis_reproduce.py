import re

import pandas as pd
import pytest
import yaml

from cs4406_sem_project.analysis import frame, reproduce, synthetic
from cs4406_sem_project.harness import grid

DOC = (reproduce.ROOT / "docs" / "reproduction_targets.md").read_text(encoding="utf-8")
RAW = yaml.safe_load(reproduce.TARGETS_PATH.read_text(encoding="utf-8"))["targets"]
TARGETS = {t.id: t for t in reproduce.load_targets()}
GIB, KIB = 1 << 30, 1 << 10


def _numbers(text: str) -> set[float]:
    text = text.replace("×10^", "e")
    return {float(m.rstrip(".")) for m in re.findall(r"\d+(?:\.\d+)?(?:e\d+)?", text)}


@pytest.mark.parametrize("raw", RAW, ids=[t["id"] for t in RAW])
def test_targets_quote_the_doc_and_invent_no_value(raw):
    assert raw["ref"] in DOC
    for piece in raw["published"].split(" ... "):
        assert piece in DOC
    quoted = _numbers(raw["published"])
    quoted |= {1 + n / 100 for n in quoted}  # "12% to 40% slowdown" is a 1.12 to 1.40 ratio
    for n in [raw.get("value"), *(raw.get("range") or [])]:
        if n is not None and quoted - {1.0, 2.0}:
            assert any(abs(n - q) < 1e-9 for q in quoted), f"{raw['id']}: {n} is not in {raw['published']!r}"


def test_target_ids_are_unique():
    assert len(TARGETS) == len(RAW)


def _user_time(cells: dict[tuple[str, int, int], float], reps: int = 30) -> pd.DataFrame:
    return pd.DataFrame([{"arm": arm, "db_bytes": db, "record_bytes": rec, "phase": "online", "rep": r,
                          "user_time_s": v} for (arm, db, rec), v in cells.items() for r in range(reps)])


def _row(t3: pd.DataFrame, target: str) -> pd.Series:
    return t3[t3.id == target].iloc[0]


def test_ratio_inside_plot_precision_passes_and_outside_needs_explaining():
    t = [TARGETS["R3-time-ratio"]]  # about 1.9x, read off a plot
    ok = reproduce.compare(_user_time({("M1", GIB, KIB): 2.1, ("M0", GIB, KIB): 1.0}), t)
    bad = reproduce.compare(_user_time({("M1", GIB, KIB): 3.0, ("M0", GIB, KIB): 1.0}), t)
    assert _row(ok, "R3-time-ratio").ours == pytest.approx(2.1) and _row(ok, "R3-time-ratio").verdict == "pass"
    assert _row(bad, "R3-time-ratio").verdict == "explain"
    assert _row(bad, "R3-time-ratio").deviation == pytest.approx(3.0 / 1.9 - 1)


def test_max_ratio_and_trend_use_every_listed_size():
    sizes = [grid.parse_size(s) for s in ("1MiB", "16MiB", "128MiB", "1GiB")]
    rising = {("M1", db, KIB): r for db, r in zip(sizes, (1.2, 1.4, 1.6, 3.5))} | {("M0", db, KIB): 1.0 for db in sizes}
    t3 = reproduce.compare(_user_time(rising), [TARGETS["R1-time"], TARGETS["R2-time"]])
    summary = t3[t3.cell.str.contains("listed sizes")].set_index("id")
    assert summary.loc["R1-time", "ours"] == pytest.approx(3.5)
    assert summary.loc["R1-time", "verdict"] == "explain"  # above the published 2.9x maximum
    assert summary.loc["R2-time", "verdict"] == "pass"


def test_hw_absolute_targets_report_a_factor_not_a_verdict():
    t3 = reproduce.compare(_user_time({("M0", GIB, KIB): 0.26}), [TARGETS["R3-time-M0"]])
    row = _row(t3, "R3-time-M0")
    assert row.verdict == "hw factor" and row.deviation == pytest.approx(0.26 / 0.13 - 1)


def test_missing_cells_are_reported_not_dropped():
    t3 = reproduce.compare(_user_time({("M0", GIB, KIB): 1.0}), [TARGETS["R5"]])
    assert list(t3.verdict) == ["missing", "missing"]


def test_every_target_has_a_row_on_the_synthetic_tier1_grid():
    df = frame.rows_to_frame(synthetic.rows_for_cells(grid.expand(grid.load(), "tier1")))
    t3 = reproduce.compare(df, list(TARGETS.values()))
    assert set(t3.id) == set(TARGETS)
    assert "missing" not in set(t3.verdict)
    assert set(t3.verdict) <= {"pass", "explain", "hw factor", "context (different baseline)", "see summary row"}


def test_absolute_values_compare_in_the_published_unit():
    df = pd.DataFrame([{"arm": "M0", "db_bytes": GIB, "record_bytes": KIB, "phase": "online", "rep": r,
                        "online_bytes": 2.2 * (1 << 20)} for r in range(5)])
    row = _row(reproduce.compare(df, [TARGETS["R3-bw-M0"]]), "R3-bw-M0")  # about 2.1 MiB, plot reading
    assert row.ours == pytest.approx(2.2) and row.verdict == "pass"
