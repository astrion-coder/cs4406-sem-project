"""Effectiveness metrics, paired bootstrap, and latency summaries.

Conventions (match the TREC DL / Pyserini reference numbers):
  nDCG@10 on graded judgments; Recall@1000 with relevance >= 2 counted as relevant.
"""

import numpy as np

from . import data, logs
from .config import PipelineConfig

PERCENTILES = (50, 95, 99)


def _measures():
    from ir_measures import R, nDCG

    return [nDCG @ 10, R(rel=2) @ 1000]


def per_query_metrics(run: dict[str, dict[str, float]], qrels, qids: list[str]):
    """Per-query metric table (rows = queries, columns = measures). Queries in ``qids`` that
    retrieved nothing score 0 rather than silently dropping out of the average."""
    import ir_measures
    import pandas as pd

    measures = _measures()
    rows = [(m.query_id, str(m.measure), m.value) for m in ir_measures.iter_calc(measures, qrels, run)]
    df = pd.DataFrame(rows, columns=["qid", "measure", "value"]).pivot(index="qid", columns="measure", values="value")
    return df.reindex(index=qids, columns=[str(m) for m in measures]).fillna(0.0)


def evaluate_config(config: PipelineConfig, eval_set: str, pipeline=None, mmap: bool = True):
    """Run every judged query of ``eval_set`` (a key of ``data.EVAL_SETS``) through the
    pipeline serially and return ``(per_query_df, run)``. Effectiveness does not depend on load,
    so this runs once per config, independently of the latency experiments."""
    from .pipeline import Pipeline

    pipeline = pipeline or Pipeline(config, mmap=mmap)
    dataset_id = data.EVAL_SETS[eval_set]
    queries = data.judged_queries(dataset_id)
    run = {}
    progress = logs.Progress(logs.get("eval"), f"effectiveness {config.config_id} {eval_set}", total=len(queries), unit="queries")
    for q in progress.track(queries):
        res = pipeline.run(q.text)
        run[q.qid] = {str(d): float(s) for d, s in zip(res.doc_ids, res.scores)}
    return per_query_metrics(run, data.qrels(dataset_id), [q.qid for q in queries]), run


def paired_bootstrap(a, b, n_resamples: int = 10_000, seed: int = 0, alpha: float = 0.05) -> dict:
    """Paired bootstrap over queries for mean(b) - mean(a).

    ``a`` and ``b`` are per-query scores aligned by query. The difference is claimed only when
    the (1 - alpha) percentile CI excludes zero.
    """
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.shape != b.shape or a.ndim != 1:
        raise ValueError("a and b must be 1-d and aligned by query")
    diff = b - a
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diff), size=(n_resamples, len(diff)))
    means = diff[idx].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return {
        "mean_diff": float(diff.mean()),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "significant": bool(lo > 0 or hi < 0),
        "n_queries": int(len(diff)),
    }


def latency_summary(latency_ns) -> dict:
    """p50/p95/p99 in milliseconds. Means are deliberately not reported."""
    ms = np.asarray(latency_ns, dtype=float) / 1e6
    out = {f"p{p}_ms": float(np.percentile(ms, p)) for p in PERCENTILES}
    out["n"] = int(len(ms))
    return out
