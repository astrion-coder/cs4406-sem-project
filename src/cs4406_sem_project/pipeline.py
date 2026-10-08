"""Retrieve-then-rerank pipeline with per-stage timing."""

import time
from dataclasses import dataclass

import numpy as np

from .config import PipelineConfig
from .first_stage import load_first_stage


@dataclass
class PipelineResult:
    doc_ids: np.ndarray
    scores: np.ndarray
    stage_ns: dict[str, int]  # wall time per stage, measured in-process

    @property
    def total_ns(self) -> int:
        return sum(self.stage_ns.values())


class Pipeline:
    def __init__(self, config: PipelineConfig, mmap: bool = True):
        self.config = config
        self.first_stage = load_first_stage(config.first_stage, mmap=mmap, **dict(config.first_stage_params))
        if config.reranker is not None:
            raise NotImplementedError("rerankers land in week 2 (rerank/cross_encoder.py)")
        self.reranker = None

    def run(self, query: str) -> PipelineResult:
        t0 = time.perf_counter_ns()
        doc_ids, scores = self.first_stage.retrieve(query, self.config.k)
        t1 = time.perf_counter_ns()
        stage_ns = {"retrieve": t1 - t0}
        return PipelineResult(doc_ids, scores, stage_ns)
