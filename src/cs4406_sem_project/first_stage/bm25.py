"""BM25 first stage (arms A1, A2) on top of ``bm25s``.

Parameters default to Pyserini's MS MARCO passage settings (k1=0.9, b=0.4) with Lucene's
BM25 variant, so effectiveness can be validated against the Pyserini reference run.

The index is built here rather than with ``bm25s.BM25.index``: bm25s keeps one Python id list
per document plus several full-size intermediate arrays, which peaked above 12 GB on MS MARCO
and was OOM-killed on the 15 GB measurement machine. ``build`` instead tokenizes in chunks,
keeps only compact (doc, term, tf) arrays, and fills the CSC score matrix with a counting sort.
It reproduces bm25s' arithmetic exactly (see ``_scores``), writes bm25s' on-disk format, and is
loaded and queried by bm25s unchanged. Peak memory is ~6 GB for MS MARCO.
"""

import itertools
import json
import threading
import time
from pathlib import Path

import numpy as np

from .. import data, logs
from ..paths import index_path

log = logs.get("bm25")

STEMMER_LANG = "english"
STOPWORDS = "en"
CHUNK_DOCS = 1_000_000


def _make_tokenizer():
    import bm25s
    import Stemmer

    return bm25s.tokenization.Tokenizer(
        stemmer=Stemmer.Stemmer(STEMMER_LANG).stemWord, stopwords=STOPWORDS
    )


def _chunk_postings(chunk: list[list[int]], vocab_bound: int):
    """(doc_local, term, tf) for every distinct term of every doc in ``chunk``, sorted by
    (doc, term); plus each doc's length in tokens (bm25s' ``len(token_ids)``)."""
    lengths = np.fromiter((len(ids) for ids in chunk), dtype=np.int64, count=len(chunk))
    terms = np.fromiter(itertools.chain.from_iterable(chunk), dtype=np.int64, count=int(lengths.sum()))
    docs = np.repeat(np.arange(len(chunk), dtype=np.int64), lengths)
    keys, tf = np.unique(docs * vocab_bound + terms, return_counts=True)
    if tf.max(initial=0) > np.iinfo(np.uint16).max:
        raise ValueError("term frequency exceeds uint16")
    return (keys // vocab_bound).astype(np.int32), (keys % vocab_bound).astype(np.int32), tf.astype(np.uint16), lengths


def _scores(tf, doc_len, idf, k1: float, b: float, avg_doc_len: np.float64) -> np.ndarray:
    """bm25s' Lucene scoring, with the same dtypes so results are bit-identical: tf as float32,
    length normalisation in float64, idf stored as float32, product cast to float32."""
    tf = tf.astype(np.float32).astype(np.float64)
    tfc = tf / (k1 * ((1 - b) + b * doc_len.astype(np.float64) / avg_doc_len) + tf)
    return (idf.astype(np.float64) * tfc).astype(np.float32)


def build(
    dataset_id: str = data.CORPUS,
    name: str = "bm25",
    k1: float = 0.9,
    b: float = 0.4,
    method: str = "lucene",
    limit: int | None = None,
) -> Path:
    """Build and save a BM25 index. ``limit`` indexes only the first N docs (for smoke tests
    and the corpus-size scaling study)."""
    import bm25s

    if method != "lucene":
        raise ValueError("only the Lucene BM25 variant is implemented")
    out = index_path(name)
    out.mkdir(parents=True, exist_ok=True)
    tokenizer = _make_tokenizer()
    doc_ids: list[str] = []

    def texts():
        for i, (doc_id, text) in enumerate(data.iter_docs(dataset_id)):
            if limit is not None and i >= limit:
                return
            doc_ids.append(doc_id)
            yield text

    total = limit or data.docs_count(dataset_id)
    t0 = time.perf_counter()
    log.info(f"building BM25 index {name!r} over {dataset_id} (k1={k1}, b={b}, {method}) -> {out}")

    # 1. Tokenize in chunks, keeping only compact postings per chunk. Keys doc_local * 2^31 + term
    # fit in int64 (doc_local < CHUNK_DOCS, term ids < vocabulary size).
    vocab_bound = 1 << 31
    chunks, lengths, n_seen = [], [], 0
    progress = logs.Progress(log, "tokenize", total=total, unit="docs")
    stream = progress.track(tokenizer.streaming_tokenize(texts()))
    while batch := list(itertools.islice(stream, CHUNK_DOCS)):
        doc_local, term, tf, lens = _chunk_postings(batch, vocab_bound)
        chunks.append((n_seen, doc_local, term, tf))
        lengths.append(lens)
        n_seen += len(batch)
        del batch
    t_tokenize = time.perf_counter() - t0
    vocab = dict(tokenizer.get_vocab_dict())
    n_vocab, n_docs = len(vocab), len(doc_ids)
    assert n_docs == n_seen
    doc_len = np.concatenate(lengths)
    del lengths
    nnz = sum(len(c[2]) for c in chunks)
    log.info(f"vocabulary: {n_vocab:,} terms; postings: {nnz:,}; {logs.memory()}")

    # 2. Document frequencies and idf (bm25s: idf = log(1 + (N - df + 0.5) / (df + 0.5))).
    df = np.zeros(n_vocab, dtype=np.int64)
    for _, _, term, _ in chunks:
        df += np.bincount(term, minlength=n_vocab)
    idf = np.zeros(n_vocab, dtype=np.float32)
    nz = df > 0
    idf[nz] = np.log(1 + (n_docs - df[nz] + 0.5) / (df[nz] + 0.5))
    avg_doc_len = doc_len.mean()

    # 3. Counting sort into CSC (columns = terms, rows = docs ascending within a column).
    indptr = np.zeros(n_vocab + 1, dtype=np.int64)
    np.cumsum(df, out=indptr[1:])
    heads = indptr[:-1].copy()
    data_arr = np.empty(nnz, dtype=np.float32)
    indices = np.empty(nnz, dtype=np.int32)
    progress = logs.Progress(log, "score matrix", total=len(chunks), unit="chunks", interval=0)
    while chunks:
        offset, doc_local, term, tf = chunks.pop(0)
        doc = doc_local + np.int32(offset)
        scores = _scores(tf, doc_len[doc], idf[term], k1, b, avg_doc_len)
        order = np.argsort(term, kind="stable")  # keeps docs ascending within each term
        term, doc, scores = term[order], doc[order], scores[order]
        uniq, starts, counts = np.unique(term, return_index=True, return_counts=True)
        rank = np.arange(len(term), dtype=np.int64) - np.repeat(starts, counts)
        dest = heads[term] + rank
        data_arr[dest] = scores
        indices[dest] = doc
        heads[uniq] += counts
        del doc_local, term, tf, doc, scores, order, rank, dest
        progress.update()
    progress.finish()
    assert np.array_equal(heads, indptr[1:]), "CSC fill is inconsistent"

    # 4. Save in bm25s' format via a BM25 object (vocab gets bm25s' trailing empty token).
    retriever = bm25s.BM25(k1=k1, b=b, method=method)
    retriever.scores = {"data": data_arr, "indices": indices, "indptr": indptr, "num_docs": n_docs}
    if "" not in vocab:
        vocab[""] = max(vocab.values()) + 1
    retriever.vocab_dict = vocab
    retriever.nonoccurrence_array = None
    t_total = time.perf_counter() - t0

    with logs.heartbeat(log, "save index"):
        retriever.save(out, show_progress=False)
        tokenizer.save_vocab(save_dir=out)
        tokenizer.save_stopwords(save_dir=out)
        np.save(out / "doc_ids.npy", np.asarray(doc_ids))
    meta = {
        "dataset": dataset_id,
        "num_docs": n_docs,
        "num_terms": n_vocab,
        "num_postings": int(nnz),
        "avg_doc_len": float(avg_doc_len),
        "k1": k1,
        "b": b,
        "method": method,
        "tokenize_seconds": t_tokenize,
        "build_seconds": t_total,
    }
    (out / "build_meta.json").write_text(json.dumps(meta, indent=2))
    size = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
    log.info(f"index {name!r} done: {n_docs:,} docs, {size / 1e9:.2f} GB on disk, "
             f"total {logs.fmt_duration(time.perf_counter() - t0)}")
    return out


class BM25Retriever:
    def __init__(self, name: str, retriever, tokenizer, doc_ids: np.ndarray, path: Path):
        self.name = name
        self._retriever = retriever
        self._tokenizer = tokenizer
        self._doc_ids = doc_ids
        self._path = path
        # The tokenizer maps unknown words to the empty token "", which is also the token that
        # documents with no indexable words are stored under. Dropped from queries so an
        # all-unknown query returns nothing instead of matching those empty documents.
        self._empty_id = tokenizer.get_vocab_dict().get("")
        self._local = threading.local()

    @classmethod
    def load(cls, name: str = "bm25", mmap: bool = True) -> "BM25Retriever":
        import bm25s

        path = index_path(name)
        retriever = bm25s.BM25.load(path, mmap=mmap, load_corpus=False, show_progress=False)
        tokenizer = _make_tokenizer()
        tokenizer.load_vocab(save_dir=path)
        tokenizer.load_stopwords(save_dir=path)
        doc_ids = np.load(path / "doc_ids.npy", mmap_mode="r" if mmap else None)
        return cls(name, retriever, tokenizer, doc_ids, path)

    def retrieve(self, query: str, k: int) -> tuple[np.ndarray, np.ndarray]:
        """Top-k by BM25. Scores are accumulated exactly as bm25s does (float32 ``np.add.at``
        over the query terms' postings, in query-term order), but top-k is selected among the
        documents that matched instead of bm25s' argpartition over all 8.8M documents, which
        alone cost ~350 ms per query. Ties are broken by internal doc index, so rankings are
        deterministic."""
        q = self._tokenizer.tokenize([query], update_vocab=False, return_as="ids", show_progress=False)
        terms = [t for t in q[0] if t != self._empty_id] if q else []
        if not terms:  # no query term is in the vocabulary
            return np.empty(0, dtype=self._doc_ids.dtype), np.empty(0, dtype=np.float32)
        idx, scores = self._top_k(terms, k)
        return self._doc_ids[idx], scores

    def _top_k(self, terms: list[int], k: int) -> tuple[np.ndarray, np.ndarray]:
        m = self._retriever.scores
        indptr, indices, values = m["indptr"], m["indices"], m["data"]
        cand = np.concatenate([indices[indptr[t]:indptr[t + 1]] for t in terms])
        if len(cand) == 0:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float32)
        vals = np.concatenate([values[indptr[t]:indptr[t + 1]] for t in terms])
        buf = self._buffer()
        np.add.at(buf, cand, vals)
        cand_scores = buf[cand]
        buf[cand] = 0  # reset only what was touched, ready for the next query
        # A doc appears at most len(terms) times in cand, all copies with the same score, so
        # the k * len(terms) highest entries contain the k best distinct docs.
        kk = min(len(cand), k * len(terms))
        if kk < len(cand):
            top = np.argpartition(-cand_scores, kk - 1)[:kk]
            cand, cand_scores = cand[top], cand_scores[top]
        cand, first = np.unique(cand, return_index=True)
        cand_scores = cand_scores[first]
        order = np.lexsort((cand, -cand_scores))[:k]
        keep = cand_scores[order] > 0
        return cand[order][keep], cand_scores[order][keep]

    def _buffer(self) -> np.ndarray:
        """Per-thread dense float32 accumulator over all docs (35 MB for MS MARCO)."""
        buf = getattr(self._local, "buf", None)
        if buf is None:
            buf = self._local.buf = np.zeros(self._retriever.scores["num_docs"], dtype=np.float32)
        return buf

    def index_files(self) -> list[Path]:
        return [p for p in self._path.iterdir() if p.is_file()]

    def index_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.index_files())
