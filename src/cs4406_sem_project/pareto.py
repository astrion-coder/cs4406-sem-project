"""Pareto frontier over (latency, effectiveness) points: lower latency and higher
effectiveness are both better."""

import numpy as np


def pareto_mask(latency, effectiveness) -> np.ndarray:
    """Boolean mask of non-dominated points.

    A point is dominated if another point has latency <= and effectiveness >=, with at least
    one strict. Exact duplicates are both kept.
    """
    lat = np.asarray(latency, dtype=float)
    eff = np.asarray(effectiveness, dtype=float)
    if lat.shape != eff.shape:
        raise ValueError("latency and effectiveness must have the same shape")
    # Sweep by latency ascending (ties: best effectiveness first); a point is on the frontier
    # iff it beats the best effectiveness of every strictly faster-or-equal point before it.
    order = np.lexsort((-eff, lat))
    mask = np.zeros(len(lat), dtype=bool)
    best = -np.inf
    prev = None
    for i in order:
        if eff[i] > best:
            mask[i] = True
            best = eff[i]
            prev = i
        elif prev is not None and lat[i] == lat[prev] and eff[i] == eff[prev]:
            mask[i] = True  # exact duplicate of a frontier point
    return mask


def frontier(df, latency_col: str = "p95_ms", eff_col: str = "ndcg@10", group_col: str | None = "concurrency"):
    """Return ``df`` with an ``on_frontier`` column, computed separately per ``group_col``."""
    df = df.copy()
    df["on_frontier"] = False
    groups = [(None, df)] if group_col is None else df.groupby(group_col)
    for _, g in groups:
        df.loc[g.index, "on_frontier"] = pareto_mask(g[latency_col].to_numpy(), g[eff_col].to_numpy())
    return df
