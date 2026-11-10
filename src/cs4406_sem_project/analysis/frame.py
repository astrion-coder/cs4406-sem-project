"""Turn raw result rows into the one table every analysis module reads.

One row per (run, arm, cell, phase, repetition) of an ok, non-warm-up, unfaulted adapter row.
Per-server lists become `<metric>_max` (the parallel time) and `<metric>_sum` (the total work).
Rows from a platform other than the reference are refused (spec 7), as is any row the validator
rejects, so analysis never sees a row the harness would not have written.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

from cs4406_sem_project.harness import validate

KEY = ["arm", "db_bytes", "record_bytes"]  # a cell; record_bytes is 0 for the one-bit arms S3 and S4
PER_SERVER = ("server_answer_s", "server_answer_cpu_s", "up_bytes_per_server", "down_bytes_per_server",
              "rtt_s", "rtt_cpu_s")


class RefusedRows(ValueError):
    pass


def _flatten(row: dict) -> dict:
    m = row["metrics"]
    out = {
        "run_id": row["run_id"], "arm": row["arm"], "adapter": row["adapter"],
        "db_bytes": row["db_bytes"], "record_bytes": row["record_bytes"] or 0,
        "retrieved_bits": row["retrieved_bits"], "phase": row["phase"], "rep": row["rep"],
        "mode": row.get("mode", "bench"), "correct": row["correct"], "detected": row["detected"],
        "padding_factor": row["scheme_params"].get("padding_factor"),
    }
    for key, value in m.items():
        if key in PER_SERVER:
            vals = [v for v in (value or []) if v is not None]
            out[f"{key}_max"] = max(vals) if vals else None
            out[f"{key}_sum"] = sum(vals) if vals else None
        else:
            out[key] = value
    return out


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    """Totals the figures and ratios use. A missing component counts as 0 only where it is
    structurally absent (a fused verify, an arm with no client offline work)."""
    z = lambda col: df[col].fillna(0.0)  # noqa: E731
    df["online_bytes"] = df["up_bytes"] + df["down_bytes"]
    df["offline_bytes"] = df["offline_down_bytes"] + z("offline_up_bytes")
    df["client_online_s"] = df["query_gen_s"] + df["client_recover_s"] + z("client_verify_s")
    df["server_online_s"] = df["server_answer_s_max"]
    df["server_offline_s"] = df["server_preproc_s"] + z("server_preproc_proof_s")
    df["throughput_GBps"] = df["db_bytes"] / 1e9 / df["server_answer_s_max"]
    return df


def rows_to_frame(rows: Iterable[dict]) -> pd.DataFrame:
    keep = [_flatten(r) for r in rows
            if validate.kind(r) == "adapter" and r["status"] == "ok" and not r["is_warmup"]
            and not r["faulted"] and r["fault"] == "none"]
    df = pd.DataFrame(keep)
    if df.empty:
        return df
    for col in df.columns:
        if df[col].dtype == object and col not in ("run_id", "arm", "adapter", "phase", "mode",
                                                    "correct", "detected"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = _derive(df).replace([np.inf, -np.inf], np.nan)
    return df.sort_values(KEY + ["phase", "rep"]).reset_index(drop=True)


def read_rows(paths: Iterable[Path], reference_platform: str) -> list[dict]:
    rows = []
    for path in paths:
        problems = [p for p in validate.check_file(path, reference_platform=reference_platform)
                    if p.severity == "error"]
        if problems:
            raise RefusedRows(f"{path}: {problems[0]}")
        rows += [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return rows


def run_files(run_dir: Path) -> list[Path]:
    return sorted(p for p in run_dir.glob("*.jsonl") if p.name != "ledger.jsonl")


def load_runs(run_dirs: Iterable[Path], reference_platform: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The measurement table, and one row per cell that did not finish ok (status, error)."""
    rows = read_rows([p for d in run_dirs for p in run_files(d)], reference_platform)
    status = pd.DataFrame([
        {"arm": r["arm"], "db_bytes": r["db_bytes"], "record_bytes": r["record_bytes"] or 0,
         "phase": r["phase"], "status": r["status"], "error": r.get("error")}
        for r in rows if validate.kind(r) in ("adapter", "failure") and r["status"] != "ok"
    ]).drop_duplicates()
    return rows_to_frame(rows), status
