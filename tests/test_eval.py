import numpy as np

from cs4406_sem_project.eval import latency_summary, paired_bootstrap


def test_bootstrap_detects_real_difference():
    rng = np.random.default_rng(1)
    a = rng.random(100)
    res = paired_bootstrap(a, a + 0.05 + rng.normal(0, 0.01, 100))
    assert res["significant"] and res["ci_low"] > 0


def test_bootstrap_no_difference():
    rng = np.random.default_rng(2)
    a = rng.random(100)
    res = paired_bootstrap(a, a + rng.normal(0, 0.05, 100))
    assert not res["significant"]


def test_latency_summary_percentiles_in_ms():
    s = latency_summary(np.arange(1, 101) * 1_000_000)  # 1..100 ms
    assert s["n"] == 100
    assert abs(s["p50_ms"] - 50.5) < 1e-9
    assert abs(s["p95_ms"] - 95.05) < 1e-9
