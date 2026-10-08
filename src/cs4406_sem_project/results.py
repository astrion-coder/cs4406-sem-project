"""Results storage under ``results/``.

  effectiveness.parquet            one row per (config_id, eval_set): mean metrics
  effectiveness_per_query.parquet  one row per (config_id, eval_set, qid): for bootstrap tests
  latency/<config>/<run>.parquet   per-query latencies of one load-test repetition
  latency/<config>/<run>.json      that run's metadata (configs, machine snapshot, QPS, memory)
  latency_summary.parquet          one row per run: p50/p95/p99 + metadata, rebuilt from the above
"""

import json
import re

from .eval import latency_summary
from .paths import RESULTS_DIR

LATENCY_DIR = RESULTS_DIR / "latency"


def safe_name(config_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._=-]+", "__", config_id)


def _upsert(path, new, keys: list[str]):
    import pandas as pd

    if path.exists():
        old = pd.read_parquet(path)
        stale = old.set_index(keys).index.isin(new.set_index(keys).index)
        new = pd.concat([old[~stale], new], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    new.to_parquet(path, index=False)
    return new


def save_effectiveness(config, eval_set: str, per_query) -> dict:
    """Store one config's per-query metrics on one eval set and return the mean row."""
    import pandas as pd

    pq = per_query.reset_index().assign(config_id=config.config_id, eval_set=eval_set)
    _upsert(RESULTS_DIR / "effectiveness_per_query.parquet", pq, ["config_id", "eval_set", "qid"])
    row = {**config.to_dict(), "eval_set": eval_set, "n_queries": len(per_query)}
    row |= {m.lower(): float(v) for m, v in per_query.mean().items()}
    _upsert(RESULTS_DIR / "effectiveness.parquet", pd.DataFrame([row]).astype({"first_stage_params": str, "reranker_params": str}), ["config_id", "eval_set"])
    return row


def run_name(concurrency: int, cache: str, rep: int, mmap: bool) -> str:
    return f"c{concurrency}_{cache}_{'mmap' if mmap else 'ram'}_r{rep}"


def save_latency_run(config_id: str, name: str, df, meta: dict) -> None:
    out = LATENCY_DIR / safe_name(config_id)
    out.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out / f"{name}.parquet", index=False)
    (out / f"{name}.json").write_text(json.dumps(meta, indent=2, default=str))


def latency_run_exists(config_id: str, name: str) -> bool:
    return (LATENCY_DIR / safe_name(config_id) / f"{name}.parquet").exists()


def build_latency_summary():
    """Rebuild ``latency_summary.parquet`` from every stored run."""
    import pandas as pd

    rows = []
    for meta_path in sorted(LATENCY_DIR.glob("*/*.json")):
        meta = json.loads(meta_path.read_text())
        df = pd.read_parquet(meta_path.with_suffix(".parquet"))
        flat = {k: v for k, v in meta.items() if not isinstance(v, (dict, list))}
        flat["governors"] = ",".join(meta["system"]["governors"])
        flat["run"] = meta_path.stem
        rows.append(flat | latency_summary(df["latency_ns"]) | {
            f"service_{k}": v for k, v in latency_summary(df["service_ns"]).items() if k != "n"
        })
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary.to_parquet(RESULTS_DIR / "latency_summary.parquet", index=False)
    return summary
