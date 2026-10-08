"""Dataset access via ir_datasets.

MS MARCO passage doc ids are the strings "0".."8841822" in collection order, so for that
collection the index position of a passage equals ``int(doc_id)``. Indexes still store an
explicit doc-id array so BEIR collections (arbitrary string ids) work the same way.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from . import paths  # noqa: F401  (sets IR_DATASETS_HOME before ir_datasets is imported)

CORPUS = "msmarco-passage"

# Topic sets with judgments, used for effectiveness.
EVAL_SETS = {
    "dl19": "msmarco-passage/trec-dl-2019/judged",
    "dl20": "msmarco-passage/trec-dl-2020/judged",
}

# Query pool for the latency / load experiments (6,980 real MS MARCO dev queries).
LOAD_QUERIES = "msmarco-passage/dev/small"

BEIR_SETS = {
    "nfcorpus": "beir/nfcorpus/test",
    "scifact": "beir/scifact/test",
    "trec-covid": "beir/trec-covid",
    "fiqa": "beir/fiqa/test",
}


@dataclass(frozen=True)
class Query:
    qid: str
    text: str


def _load(dataset_id: str):
    import ir_datasets

    return ir_datasets.load(dataset_id)


def iter_docs(dataset_id: str = CORPUS) -> Iterator[tuple[str, str]]:
    """Yield ``(doc_id, text)`` in collection order. BEIR docs have a title; it is prepended."""
    for doc in _load(dataset_id).docs_iter():
        title = getattr(doc, "title", "")
        yield doc.doc_id, f"{title} {doc.text}" if title else doc.text


def docs_count(dataset_id: str = CORPUS) -> int:
    return _load(dataset_id).docs_count()


def queries(dataset_id: str) -> list[Query]:
    return [Query(q.query_id, q.text) for q in _load(dataset_id).queries_iter()]


def qrels(dataset_id: str) -> list:
    """Raw ir_datasets qrels, consumable directly by ir_measures."""
    return list(_load(dataset_id).qrels_iter())


def judged_queries(dataset_id: str) -> list[Query]:
    """Queries that have at least one judgment (the ``/judged`` subsets already are)."""
    judged = {q.query_id for q in _load(dataset_id).qrels_iter()}
    return [q for q in queries(dataset_id) if q.qid in judged]


def docstore(dataset_id: str = CORPUS):
    """Random-access passage text store (used to fetch candidate text for reranking)."""
    return _load(dataset_id).docs_store()
