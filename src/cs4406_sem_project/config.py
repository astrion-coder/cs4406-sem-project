"""Pipeline and serving configurations, and the experiment grid.

A ``PipelineConfig`` fully determines a ranking (so effectiveness is computed once per config).
A ``ServingConfig`` determines how queries are executed (so it only affects latency).
"""

from collections.abc import Iterator
from dataclasses import asdict, dataclass, field

DEPTHS = (100, 250, 500, 1000)


@dataclass(frozen=True)
class PipelineConfig:
    arm: str
    first_stage: str  # index name under data/indexes, e.g. "bm25"
    k: int  # candidate depth handed to the reranker (or returned, if no reranker)
    reranker: str | None = None  # HF checkpoint, None = first stage only
    first_stage_params: tuple[tuple[str, object], ...] = ()
    reranker_params: tuple[tuple[str, object], ...] = ()

    @property
    def config_id(self) -> str:
        """Stable, human-readable key used in every results table."""
        parts = [self.arm, self.first_stage, f"k{self.k}"]
        parts += [f"{key}={val}" for key, val in sorted(self.first_stage_params)]
        parts.append(self.reranker.rsplit("/", 1)[-1] if self.reranker else "norerank")
        parts += [f"{key}={val}" for key, val in sorted(self.reranker_params)]
        return "|".join(parts)

    def to_dict(self) -> dict:
        return asdict(self) | {"config_id": self.config_id}


@dataclass(frozen=True)
class ServingConfig:
    """How the pipeline is served during a load test.

    ``workers`` is the fixed serving capacity (independent of offered concurrency, as in a real
    deployment); ``threads_per_worker`` caps intra-query parallelism (BLAS/OpenMP/torch).
    """

    workers: int = 14  # physical cores on the i5-13500 measurement box
    threads_per_worker: int = 1
    backend: str = "process"  # "process" (mmap-shared index, no GIL) or "thread"


@dataclass(frozen=True)
class LoadConfig:
    concurrency: int
    warmup_queries: int = 500
    max_queries: int = 2000
    max_seconds: float = 600.0
    cache: str = "warm"  # "warm" or "cold"
    seed: int = 0
    serving: ServingConfig = field(default_factory=ServingConfig)


CONCURRENCY_LEVELS = (1, 4, 16, 64)
CROSS_ENCODER = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def grid(arms: tuple[str, ...] = ("A1", "A2")) -> Iterator[PipelineConfig]:
    """The experiment grid. Arms are added here as they are implemented (week 2+: A3-A7)."""
    for k in DEPTHS:
        if "A1" in arms:
            yield PipelineConfig("A1", "bm25", k, CROSS_ENCODER)
        if "A2" in arms:
            yield PipelineConfig("A2", "bm25", k)
