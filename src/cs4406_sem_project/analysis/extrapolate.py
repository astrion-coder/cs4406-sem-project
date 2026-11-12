"""S4 at 16 MiB, 128 MiB and 1 GiB from its measured sizes (spec 4 and 5, decision 10).

At the spec's repetitions S4 runs only at 64 KiB, 256 KiB and 1 MiB. For each metric, a least-squares power
law log(median) = a + b log(db_bytes) is fitted to S4's measured cell medians and evaluated at the larger
sizes. Every row this module returns is labelled extrapolated, with the exponent and the sizes it came from.
None of it is a measurement.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cs4406_sem_project.analysis import frame
from cs4406_sem_project.harness.grid import fmt_size

ARM = "S4"
FIT_SIZES = (64 << 10, 256 << 10, 1 << 20)  # decision 10's sizes; the 1 KiB RQ1 anchor stays out of the fit
SIZES = (16 << 20, 128 << 20, 1 << 30)  # spec 4 names 16 MiB and 128 MiB; target R8 needs 1 GiB
FIT_COLUMNS = ["arm", "metric", "exponent", "intercept", "fit_sizes"]


def fits(medians: pd.DataFrame, arm: str = ARM) -> pd.DataFrame:
    """One row per metric whose median is positive at every fitted size (a power law needs logs)."""
    m = medians[(medians.arm == arm) & medians.db_bytes.isin(FIT_SIZES)].sort_values("db_bytes")
    if len(m) < 2:
        return pd.DataFrame(columns=FIT_COLUMNS)
    x = np.log(m.db_bytes.to_numpy(dtype=float))
    sizes = ",".join(fmt_size(int(s)) for s in m.db_bytes)
    rows = []
    for metric in m.columns.difference(frame.KEY):
        y = m[metric].to_numpy(dtype=float)
        if np.isnan(y).any() or (y <= 0).any():
            continue
        b, a = np.polyfit(x, np.log(y), 1)
        rows.append({"arm": arm, "metric": metric, "exponent": b, "intercept": a, "fit_sizes": sizes})
    return pd.DataFrame(rows, columns=FIT_COLUMNS)


def extrapolate(medians: pd.DataFrame, arm: str = ARM, sizes=SIZES) -> pd.DataFrame:
    """Rows shaped like the medians table, one per size the arm did not run, with extrapolated=True."""
    f = fits(medians, arm)
    measured = set(medians.loc[medians.arm == arm, "db_bytes"])
    rows = []
    for db in sizes:
        if db in measured or f.empty:
            continue
        row = {"arm": arm, "db_bytes": db, "record_bytes": 0, "extrapolated": True}
        for r in f.itertuples():
            row[r.metric] = float(np.exp(r.intercept + r.exponent * np.log(db)))
        rows.append(row)
    return pd.DataFrame(rows)
