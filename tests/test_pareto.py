import numpy as np

from cs4406_sem_project.pareto import pareto_mask


def test_simple_frontier():
    lat = [10, 20, 30, 15, 40]
    eff = [0.40, 0.50, 0.60, 0.35, 0.55]
    # (15, 0.35) is dominated by (10, 0.40); (40, 0.55) by (30, 0.60)
    assert pareto_mask(lat, eff).tolist() == [True, True, True, False, False]


def test_equal_latency_keeps_only_best():
    assert pareto_mask([10, 10], [0.3, 0.5]).tolist() == [False, True]


def test_equal_effectiveness_keeps_only_fastest():
    assert pareto_mask([10, 20], [0.5, 0.5]).tolist() == [True, False]


def test_exact_duplicates_both_kept():
    assert pareto_mask([10, 10], [0.5, 0.5]).tolist() == [True, True]


def test_random_points_match_brute_force():
    rng = np.random.default_rng(0)
    lat, eff = rng.random(200), rng.random(200)
    dominated = [
        any(lat[j] <= lat[i] and eff[j] >= eff[i] and (lat[j] < lat[i] or eff[j] > eff[i]) for j in range(200))
        for i in range(200)
    ]
    assert pareto_mask(lat, eff).tolist() == [not d for d in dominated]
