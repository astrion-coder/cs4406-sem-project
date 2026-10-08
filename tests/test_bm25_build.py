"""The chunked BM25 builder must produce exactly the index bm25s itself would build."""

import random

import numpy as np
import pytest

bm25s = pytest.importorskip("bm25s")

from cs4406_sem_project.first_stage import bm25  # noqa: E402

WORDS = "retrieval latency budget rerank sparse dense index query passage score running runs ran".split()


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    rng = random.Random(0)
    docs = [(str(i), " ".join(rng.choices(WORDS, k=rng.randint(0, 30)))) for i in range(500)]
    monkeypatch.setattr(bm25.data, "iter_docs", lambda dataset_id: iter(docs))
    monkeypatch.setattr(bm25.data, "docs_count", lambda dataset_id: len(docs))
    monkeypatch.setattr(bm25, "index_path", lambda name: tmp_path / name)
    monkeypatch.setattr(bm25, "CHUNK_DOCS", 64)  # force several chunks
    return docs


def test_matches_bm25s(corpus, tmp_path):
    bm25.build("fake", name="ours")

    tokenizer = bm25._make_tokenizer()
    ids = tokenizer.tokenize([t for _, t in corpus], return_as="ids", show_progress=False)
    ref = bm25s.BM25(k1=0.9, b=0.4, method="lucene")
    ref.index((ids, tokenizer.get_vocab_dict()), show_progress=False)
    ref.save(tmp_path / "ref", show_progress=False)

    for f in ("data.csc.index.npy", "indices.csc.index.npy", "indptr.csc.index.npy"):
        assert np.array_equal(np.load(tmp_path / "ours" / f), np.load(tmp_path / "ref" / f)), f
    assert (tmp_path / "ours" / "vocab.index.json").read_text() == (tmp_path / "ref" / "vocab.index.json").read_text()


def test_retrieval_roundtrip(corpus):
    bm25.build("fake", name="ours")
    retriever = bm25.BM25Retriever.load("ours")
    doc_ids, scores = retriever.retrieve("sparse latency", 5)
    assert len(doc_ids) == 5 and np.all(np.diff(scores) <= 0)
    assert len(retriever.retrieve("unseenword", 5)[0]) == 0
