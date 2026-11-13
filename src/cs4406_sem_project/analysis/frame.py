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
        "scheme_params": json.dumps(row["scheme_params"], sort_keys=True),
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
    df["user_time_s"] = df["client_online_s"] + df["server_online_s"]  # Colombo et al.'s "user time" (p.3845)
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
                                                    "correct", "detected", "scheme_params"):
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


def _run_cell(r: dict) -> tuple:
    return r["run_id"], r["arm"], r["db_bytes"], r["record_bytes"] or 0, r["phase"]


def load_runs(run_dirs: Iterable[Path], reference_platform: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The measurement table, and one row per cell that did not finish ok (status, error, partial_rows).
    A cell that failed contributes only its status. The rows it wrote before the failure stay in the
    raw file, but they are fewer than its repetitions and were measured under whatever made it fail."""
    rows = read_rows([p for d in run_dirs for p in run_files(d)], reference_platform)
    failed = [r for r in rows if validate.kind(r) in ("adapter", "failure") and r["status"] != "ok"]
    status = pd.DataFrame([
        {"arm": r["arm"], "db_bytes": r["db_bytes"], "record_bytes": r["record_bytes"] or 0,
         "phase": r["phase"], "status": r["status"], "error": r.get("error"),
         "partial_rows": r.get("partial_rows")}
        for r in failed
    ]).drop_duplicates()
    unfinished = {_run_cell(r) for r in failed}
    return rows_to_frame(r for r in rows if _run_cell(r) not in unfinished), status


SESSION_COMPARISON = "comparison.json"


def load_session(session_dir: Path, reference_platform: str) -> pd.DataFrame | None:
    """session_check.py's comparison of measured and computed totals, or None before it has run.
    The session's own rows go through the checks load_runs applies, so a foreign run is refused."""
    read_rows(run_files(session_dir), reference_platform)
    path = session_dir / SESSION_COMPARISON
    if not path.exists():
        return None
    table = pd.DataFrame(json.loads(path.read_text())["rows"])
    for col in ("computed", "measured", "rel_diff"):
        table[col] = pd.to_numeric(table[col], errors="coerce")
    return table


def _link_state(profile_dir: Path) -> dict:
    """The link state netem.py last recorded for this profile (link.json), or {} for the loopback baseline."""
    path = profile_dir / "link.json"
    history = json.loads(path.read_text()) if path.exists() else []
    return (history[-1].get("state") or {}) if history else {}


def load_netem(netem_dir: Path, reference_platform: str) -> pd.DataFrame:
    """netem.py's client rows, one subdirectory per profile; the profile is in the path, not the rows.
    Each row also carries its link's rtt_checked and calibrated RTT (decision 22: LAN's is recorded only)."""
    parts = []
    for d in sorted(p for p in netem_dir.iterdir() if p.is_dir()):
        df, _status = load_runs([d], reference_platform)
        if not df.empty:
            state = _link_state(d)
            calib = state.get("calib_rtt_ms")
            parts.append(df.assign(profile=d.name, rtt_checked=state.get("rtt_checked", "yes") != "no",
                                   calib_rtt_ms=float(calib) if calib else np.nan))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


LOADGEN_COLUMNS = ("arm", "db_bytes", "workers", "loop", "clients", "offered_load_qps", "run", "throughput_qps",
                   "lat_p50_s", "lat_p95_s", "lat_p99_s", "server_cpu_util", "status")
LOADGEN_NOT_POINTS = ("corpus", "servers", "rejected")


def loadgen_frame(points: dict[tuple[str, str], list[dict]]) -> pd.DataFrame:
    """One row per ok or saturated load-generator run, keyed by (cell_id, point name) as loadgen.py lays them
    out. A saturated run is still a measured point on the curve; an error run measured nothing."""
    df = pd.DataFrame([
        {"cell_id": cell, "point": point, "experiment": point.split("_")[0],
         **{k: r[k] for k in LOADGEN_COLUMNS}, "record_bytes": r["record_bytes"] or 0}
        for (cell, point), rows in points.items() for r in rows if r["status"] in ("ok", "saturated")])
    return df if df.empty else df.sort_values(["cell_id", "point", "run"]).reset_index(drop=True)


def load_loadgen(loadgen_dir: Path, reference_platform: str) -> pd.DataFrame:
    """harness/loadgen.py's rows under results/raw/<run_id>/loadgen/<cell_id>/<point>.jsonl."""
    paths = sorted(p for p in loadgen_dir.glob("*/*.jsonl") if p.parent.name not in LOADGEN_NOT_POINTS)
    return loadgen_frame({(p.parent.name, p.stem): read_rows([p], reference_platform) for p in paths})
