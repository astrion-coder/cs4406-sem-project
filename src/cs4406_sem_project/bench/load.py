"""Closed-loop load driver.

``concurrency`` client streams each submit one query, wait for its answer, then submit the next.
Queries are served by a fixed pool of ``workers`` (the serving capacity), so when concurrency
exceeds capacity, queries queue — and that queueing is part of the measured latency, exactly
as it would be for a user of a deployed system.

Per query we record:
  latency_ns  client submit -> client receives answer (the number the SLA is about)
  queue_ns    client submit -> a worker starts the query
  service_ns  time the worker spent in Pipeline.run (sum of stages)
  stage_*_ns  per-stage breakdown inside the worker

``time.perf_counter_ns`` is CLOCK_MONOTONIC on Linux, so timestamps are comparable across the
worker processes and the client process.
"""

import hashlib
import os
import random
import threading
import time
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict
from multiprocessing import get_context

from ..config import LoadConfig, PipelineConfig
from ..data import Query
from ..paths import index_path
from .. import logs
from . import sysinfo

log = logs.get("load")

_PIPELINE = None  # the worker's pipeline (one per process, or one shared by all threads)


def _init_worker(config: PipelineConfig, mmap: bool) -> None:
    global _PIPELINE
    from ..pipeline import Pipeline

    _PIPELINE = Pipeline(config, mmap=mmap)


def _ready(delay_s: float) -> int:
    time.sleep(delay_s)  # holds this worker so the other startup pings land on other workers
    return os.getpid()


def _serve(text: str) -> tuple[int, int, dict[str, int], int, str]:
    start = time.perf_counter_ns()
    result = _PIPELINE.run(text)
    end = time.perf_counter_ns()
    top10 = hashlib.blake2b(",".join(map(str, result.doc_ids[:10])).encode(), digest_size=8).hexdigest()
    return start, end, result.stage_ns, len(result.doc_ids), top10


def _drive(executor: Executor, queries: list[Query], concurrency: int, max_seconds: float | None,
           progress=None) -> tuple[list[dict], float]:
    next_i = 0
    lock = threading.Lock()
    records: list[dict] = []
    deadline = None if max_seconds is None else time.perf_counter() + max_seconds

    def client(stream: int) -> None:
        nonlocal next_i
        while True:
            with lock:
                i = next_i
                next_i += 1
            if i >= len(queries) or (deadline is not None and time.perf_counter() > deadline):
                return
            q = queries[i]
            submit = time.perf_counter_ns()
            start, end, stage_ns, n_results, top10 = executor.submit(_serve, q.text).result()
            done = time.perf_counter_ns()
            rec = {
                "qid": q.qid,
                "stream": stream,
                "seq": i,
                "latency_ns": done - submit,
                "queue_ns": start - submit,
                "service_ns": end - start,
                "n_results": n_results,
                "top10_hash": top10,
            }
            rec |= {f"stage_{name}_ns": ns for name, ns in stage_ns.items()}
            with lock:
                records.append(rec)
                if progress is not None:  # logs at most every 30 s; negligible cost
                    progress.update()

    t0 = time.perf_counter()
    threads = [threading.Thread(target=client, args=(s,)) for s in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return records, time.perf_counter() - t0


def _query_stream(queries: list[Query], n: int, seed: int) -> list[Query]:
    """``n`` queries in a seeded random order, cycling through the pool if n exceeds it."""
    order = list(queries)
    random.Random(seed).shuffle(order)
    return [order[i % len(order)] for i in range(n)]


def run_load(
    config: PipelineConfig,
    load: LoadConfig,
    queries: list[Query],
    mmap: bool = True,
    allow_unpinned: bool = False,
):
    """Run one load test (one repetition) and return ``(per_query_df, run_meta)``."""
    import pandas as pd

    sysinfo.check_governor(allow_unpinned)
    serving = load.serving
    sysinfo.apply_thread_limits(serving.threads_per_worker)

    stream = _query_stream(queries, load.warmup_queries + load.max_queries, load.seed)
    warmup, measured = stream[: load.warmup_queries], stream[load.warmup_queries :]

    if load.cache == "cold":
        files = [p for p in index_path(config.first_stage).rglob("*") if p.is_file()]
        sysinfo.drop_page_cache(files)
        warmup = []  # cold = first queries after a deploy; warming up would defeat the point

    if serving.backend == "process":
        executor: Executor = ProcessPoolExecutor(
            max_workers=serving.workers,
            mp_context=get_context("spawn"),
            initializer=_init_worker,
            initargs=(config, mmap),
        )
        # Start (and initialize) every worker before any timing starts. Workers that finish
        # loading early can take several pings, so ping in rounds until all have answered.
        seen: set[int] = set()
        for _ in range(20):
            seen |= set(executor.map(_ready, [1.0] * serving.workers))
            if len(seen) == serving.workers:
                break
        else:
            raise RuntimeError(f"only {len(seen)}/{serving.workers} workers answered")
        worker_pids = sorted(seen)
    elif serving.backend == "thread":
        _init_worker(config, mmap)
        executor = ThreadPoolExecutor(max_workers=serving.workers)
        worker_pids = [os.getpid()]
    else:
        raise ValueError(f"unknown backend {serving.backend}")

    try:
        label = f"{config.config_id} c={load.concurrency} {load.cache}"
        if warmup:
            p = logs.Progress(log, f"warm-up {label}", total=len(warmup), unit="queries")
            _drive(executor, warmup, load.concurrency, None, p)
            p.finish()
        p = logs.Progress(log, f"measure {label}", total=len(measured), unit="queries")
        records, wall_s = _drive(executor, measured, load.concurrency, load.max_seconds, p)
        p.finish(f"wall {wall_s:.0f} s (cap {load.max_seconds:.0f} s)")
        pss = sysinfo.pss_bytes(worker_pids)
    finally:
        executor.shutdown(wait=True)

    df = pd.DataFrame.from_records(records).sort_values("seq", ignore_index=True)
    meta = {
        **config.to_dict(),
        **{f"load_{k}": v for k, v in asdict(load).items() if k != "serving"},
        **{f"serving_{k}": v for k, v in asdict(serving).items()},
        "mmap": mmap,
        "measured_queries": len(df),
        "wall_seconds": wall_s,
        "achieved_qps": len(df) / wall_s if wall_s else float("nan"),
        "workers_pss_bytes": pss,
        "system": sysinfo.snapshot(),
    }
    return df, meta
