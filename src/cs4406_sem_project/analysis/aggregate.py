"""Per-cell summaries and overhead ratios with paired bootstrap CIs (spec 6, Derived quantities).

    uv run python -m cs4406_sem_project.analysis.aggregate --runs results/raw/<run_id> ... \
        [--out results/processed]
"""

from __future__ import annotations

import argparse
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from cs4406_sem_project.analysis import frame

ROOT = Path(__file__).resolve().parents[3]
REFERENCE_PLATFORM = ROOT / "env" / "reference_platform.txt"
PROCESSED = ROOT / "results" / "processed"

METRICS = {
    "online": ("online_bytes", "up_bytes", "down_bytes", "server_online_s", "client_online_s", "user_time_s",
               "query_gen_s", "client_recover_s", "client_verify_s", "throughput_GBps"),
    "offline": ("offline_bytes", "offline_down_bytes", "offline_down_bytes_hint_only", "offline_up_bytes",
                "client_storage_bytes", "server_offline_s", "server_preproc_s", "server_preproc_proof_s",
                "client_offline_s"),
}
RATIO_METRICS = {
    "online": ("online_bytes", "server_online_s", "client_online_s", "user_time_s"),
    "offline": ("offline_bytes", "client_storage_bytes", "server_offline_s", "client_offline_s"),
}
BOOTSTRAP_RESAMPLES = 10_000
SEED = 4406
PADDING_LIMIT = 1.1  # decision 19


@dataclass(frozen=True)
class Pair:
    num: str
    den: str
    role: str

    @property
    def label(self) -> str:
        return f"{self.num}/{self.den}"


PAIRS = (  # spec 4, primary comparisons
    Pair("S1", "S0-cpp", "price"),
    Pair("S2", "S0-cpp", "price"),
    Pair("M1", "M0", "price"),
    Pair("S0-go", "S0-cpp", "implementation ablation"),
)


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (cell, phase, metric): median, p95 and n over repetitions."""
    parts = []
    for phase, metrics in METRICS.items():
        sub = df[df.phase == phase]
        long = sub.melt(id_vars=frame.KEY + ["rep"], value_vars=list(metrics), var_name="metric").dropna(
            subset=["value"])
        g = long.groupby(frame.KEY + ["metric"])["value"]
        out = pd.DataFrame({"median": g.median(), "p95": g.quantile(0.95), "n": g.size()}).reset_index()
        parts.append(out.assign(phase=phase))
    out = pd.concat(parts, ignore_index=True)
    padding = df.groupby(frame.KEY)["padding_factor"].max().rename("padding_factor").reset_index()
    return out.merge(padding, on=frame.KEY, how="left")[
        frame.KEY + ["phase", "metric", "median", "p95", "n", "padding_factor"]]


def seed_for(*parts: object) -> list[int]:
    return [SEED, zlib.crc32("|".join(map(str, parts)).encode())]


def paired_bootstrap(num: np.ndarray, den: np.ndarray, *, seed: Iterable[int] | int = SEED,
                     resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float, float, float]:
    """median(num)/median(den) and its 95 % percentile CI. num[i] and den[i] share a repetition index,
    and each resample draws the same indices for both, so rep-level drift cancels in the ratio."""
    num, den = np.asarray(num, float), np.asarray(den, float)
    if len(num) != len(den) or len(num) == 0:
        raise ValueError("paired samples must be non-empty and the same length")
    point = _ratio(np.median(num), np.median(den))
    idx = np.random.default_rng(seed).integers(0, len(num), size=(resamples, len(num)))
    with np.errstate(divide="ignore", invalid="ignore"):
        boot = np.median(num[idx], axis=1) / np.median(den[idx], axis=1)
    boot = boot[np.isfinite(boot)]
    if boot.size == 0:
        return point, np.nan, np.nan
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return point, float(lo), float(hi)


def _ratio(a: float, b: float) -> float:
    return float(a / b) if b and np.isfinite(b) and np.isfinite(a) else np.nan


def overheads(df: pd.DataFrame, pairs: Iterable[Pair] = PAIRS,
              resamples: int = BOOTSTRAP_RESAMPLES) -> pd.DataFrame:
    """One row per (pair, db size, record size, phase, metric). A cell either arm lacks is left out."""
    rows = []
    cells = ["db_bytes", "record_bytes"]
    for pair in pairs:
        a, b = df[df.arm == pair.num], df[df.arm == pair.den]
        for phase, metrics in RATIO_METRICS.items():
            joined = a[a.phase == phase].merge(b[b.phase == phase], on=cells + ["rep"], suffixes=("_v", "_b"))
            for (db, rec), g in joined.groupby(cells):
                padded = bool((g[["padding_factor_v", "padding_factor_b"]] > PADDING_LIMIT).any(axis=None))
                for metric in metrics:
                    pair_rows = g[[f"{metric}_v", f"{metric}_b"]].dropna()
                    if pair_rows.empty:
                        continue
                    ratio, lo, hi = paired_bootstrap(
                        pair_rows.iloc[:, 0].to_numpy(), pair_rows.iloc[:, 1].to_numpy(),
                        seed=seed_for(pair.label, db, rec, phase, metric), resamples=resamples)
                    rows.append({"pair": pair.label, "num": pair.num, "den": pair.den, "role": pair.role,
                                 "db_bytes": db, "record_bytes": rec, "phase": phase, "metric": metric,
                                 "ratio": ratio, "ci_lo": lo, "ci_hi": hi, "n_pairs": len(pair_rows),
                                 "padded": padded})
    return pd.DataFrame(rows, columns=["pair", "num", "den", "role", "db_bytes", "record_bytes", "phase",
                                       "metric", "ratio", "ci_lo", "ci_hi", "n_pairs", "padded"])


def medians(summary: pd.DataFrame) -> pd.DataFrame:
    """One row per cell, one column per metric median, both phases side by side."""
    wide = summary.pivot_table(index=frame.KEY, columns="metric", values="median", aggfunc="first")
    wide.columns.name = None
    return wide.reset_index()


def write_tables(tables: dict[str, pd.DataFrame], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, table in tables.items():
        path = out / f"{name}.parquet"
        table.to_parquet(path, index=False)
        paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=Path, nargs="+", required=True)
    p.add_argument("--out", type=Path, default=PROCESSED)
    p.add_argument("--reference-platform", type=Path, default=REFERENCE_PLATFORM)
    args = p.parse_args(argv)
    df, status = frame.load_runs(args.runs, args.reference_platform.read_text().strip())
    summary = summarise(df)
    paths = write_tables({"frame": df, "status": status, "summary": summary, "overheads": overheads(df)},
                         args.out)
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
