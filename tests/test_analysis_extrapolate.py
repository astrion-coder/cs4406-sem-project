import pandas as pd
import pytest

from cs4406_sem_project.analysis import extrapolate

KIB, MIB, GIB = 1 << 10, 1 << 20, 1 << 30


def _medians(sizes, **metrics) -> pd.DataFrame:
    rows = [{"arm": "S4", "db_bytes": s, "record_bytes": 0, **{m: f(s) for m, f in metrics.items()}} for s in sizes]
    rows.append({"arm": "S3", "db_bytes": MIB, "record_bytes": 0, **{m: 1.0 for m in metrics}})
    return pd.DataFrame(rows)


def test_power_law_is_recovered_and_evaluated_at_the_larger_sizes():
    med = _medians([64 * KIB, 256 * KIB, MIB], digest_s=lambda s: 2e-4 * s ** 0.98, online_bytes=lambda s: 3 * s ** 0.5)
    fits = extrapolate.fits(med).set_index("metric")
    assert fits.loc["digest_s", "exponent"] == pytest.approx(0.98)
    assert fits.loc["online_bytes", "exponent"] == pytest.approx(0.5)
    ext = extrapolate.extrapolate(med).set_index("db_bytes")
    assert list(ext.index) == [16 * MIB, 128 * MIB, GIB]
    assert ext.loc[GIB, "digest_s"] == pytest.approx(2e-4 * GIB ** 0.98)
    assert ext["extrapolated"].all() and set(ext.arm) == {"S4"}


def test_the_one_kib_anchor_stays_out_of_the_fit():
    law = lambda s: 1e-3 * s  # noqa: E731
    med = _medians([KIB, 64 * KIB, 256 * KIB, MIB], digest_s=law)
    med.loc[med.db_bytes == KIB, "digest_s"] = 50.0  # a fixed cost that would bend a fit through 1 KiB
    assert extrapolate.fits(med).set_index("metric").loc["digest_s", "exponent"] == pytest.approx(1.0)


def test_a_metric_with_a_zero_is_not_extrapolated():
    med = _medians([64 * KIB, 256 * KIB, MIB], digest_s=lambda s: s * 1e-3, client_offline_s=lambda s: 0.0)
    assert "client_offline_s" not in set(extrapolate.fits(med).metric)
    assert "client_offline_s" not in extrapolate.extrapolate(med)


def test_one_measured_size_gives_nothing():
    med = _medians([64 * KIB], digest_s=lambda s: s)
    assert extrapolate.fits(med).empty and extrapolate.extrapolate(med).empty
