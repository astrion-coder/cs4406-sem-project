import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from cs4406_sem_project.bench import load
from cs4406_sem_project.data import Query
from cs4406_sem_project.pipeline import PipelineResult


class SleepPipeline:
    """Fake pipeline with a fixed 20 ms service time."""

    def run(self, query):
        t0 = time.perf_counter_ns()
        time.sleep(0.02)
        return PipelineResult(np.array(["1", "2"]), np.array([2.0, 1.0]), {"retrieve": time.perf_counter_ns() - t0})


def _drive(concurrency, workers, n=40):
    load._PIPELINE = SleepPipeline()
    queries = [Query(str(i), f"q{i}") for i in range(n)]
    with ThreadPoolExecutor(workers) as ex:
        return load._drive(ex, queries, concurrency, None)


def test_every_query_measured_once():
    records, _ = _drive(concurrency=4, workers=4)
    assert sorted(r["seq"] for r in records) == list(range(40))


def test_queueing_shows_up_when_concurrency_exceeds_capacity():
    no_queue, _ = _drive(concurrency=2, workers=2)
    queued, _ = _drive(concurrency=8, workers=2)
    p50 = lambda rs: np.median([r["latency_ns"] for r in rs]) / 1e6
    # 8 streams on 2 workers: each query waits for ~3 others => ~4x the service time
    assert p50(no_queue) < 30
    assert p50(queued) > 60
    assert all(r["latency_ns"] >= r["service_ns"] for r in queued)


def test_query_stream_is_seeded_and_cycles():
    qs = [Query(str(i), "") for i in range(5)]
    a = load._query_stream(qs, 12, seed=3)
    assert a == load._query_stream(qs, 12, seed=3)
    assert len(a) == 12 and a[:5] == a[5:10]
