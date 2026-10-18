# CS4406 — Two-Stage Retrieval Pipeline to a Latency SLA (P7)

The project plan is in [Project_Idea.md](./Project_Idea.md). Code lives in `src/cs4406_sem_project/`, measurement notebooks in `notebooks/`.

## Numeric claims

Every number stated about this project must appear here with the notebook and cell that produced it. "Cell N" counts every cell in the notebook from 1, Markdown included; the execution count shown by Jupyter is given as `[exec M]`. Re-run with:
`uv run jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=-1 <notebook>`
(set the CPU governor to `performance` first).

Measurement machine: Intel i5-13500 (14 cores / 20 threads), 15 GB RAM, governor `performance`, Python 3.12.14, bm25s 0.3.13 ([10_harness_validation.ipynb](./notebooks/10_harness_validation.ipynb), cell 2 [exec 1]).

| Claim | Value | Source |
|---|---|---|
| BM25 index covers the full MS MARCO passage collection | 8,841,823 passages | `10_harness_validation.ipynb`, cell 4 [exec 2] |
| BM25 index build time / size on disk | 4.6 min / 2.43 GB | `10_harness_validation.ipynb`, cell 4 [exec 2] |
| BM25 (k1=0.9, b=0.4) nDCG@10, TREC DL 2019 (43 queries) | 0.5105 (Pyserini reference 0.5058) | `10_harness_validation.ipynb`, cell 6 [exec 3] |
| BM25 R@1000 (rel ≥ 2), TREC DL 2019 | 0.7507 (Pyserini 0.7501) | `10_harness_validation.ipynb`, cell 6 [exec 3] |
| BM25 nDCG@10, TREC DL 2020 (54 queries) | 0.4828 (Pyserini 0.4796) | `10_harness_validation.ipynb`, cell 6 [exec 3] |
| BM25 R@1000 (rel ≥ 2), TREC DL 2020 | 0.7889 (Pyserini 0.7863) | `10_harness_validation.ipynb`, cell 6 [exec 3] |
| All four effectiveness numbers within ±0.02 of Pyserini | max deviation 0.0047 | `10_harness_validation.ipynb`, cell 6 [exec 3] |
| Single-stream BM25 latency (A2, k=1000, 1 worker) | p50 ≈ 13.1–13.4 ms, p95 ≈ 39.1–40.0 ms over 3 reps of 2,000 queries | `10_harness_validation.ipynb`, cell 8 [exec 4] |
| Candidate depth barely affects BM25 latency | k=100 p50 12.5–12.8 ms vs k=1000 13.1–13.4 ms | `10_harness_validation.ipynb`, cell 8 [exec 4] |
| Latency is stable across repetitions | p95 spread 4.5% (k=100), 2.4% (k=1000) | `10_harness_validation.ipynb`, cell 9 [exec 5] |
| Rankings are deterministic across repetitions | identical top-10 for every query, k=100 and k=1000 | `10_harness_validation.ipynb`, cell 9 [exec 5] |
| With 4 workers, throughput saturates at ~130 QPS | 128.6 QPS at c=4, 132.2 at c=8, 132.0 at c=16 | `10_harness_validation.ipynb`, cell 11 [exec 6] |
| Queueing appears only once concurrency exceeds the 4 workers | queue p50 0.3 ms at c≤4, 28.7 ms at c=8, 88.8 ms at c=16 | `10_harness_validation.ipynb`, cell 11 [exec 6] |
| Per-query service time rises with concurrency even below capacity | service p50 13.2 ms (c=1) → 17.2 (c=2) → 24.4 (c=4) | `10_harness_validation.ipynb`, cell 11 [exec 6] |

# Just for test