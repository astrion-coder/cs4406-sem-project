from .base import FirstStage


def load_first_stage(name: str, mmap: bool = True, **params) -> FirstStage:
    """Load a built first-stage index by name (directory under ``data/indexes``)."""
    if name.startswith("bm25"):
        from .bm25 import BM25Retriever

        return BM25Retriever.load(name, mmap=mmap, **params)
    raise ValueError(f"unknown first stage: {name}")


__all__ = ["FirstStage", "load_first_stage"]
