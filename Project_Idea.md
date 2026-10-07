# Designing a Two-Stage Retrieval Pipeline to a Latency SLA

## 1. What is the idea?

Every deployed search system has to decide, implicitly, how to split a fixed latency budget across first-stage retrieval, candidate depth, and reranking. Almost no published work answers this explicitly — efficiency techniques (pruning, compression, early termination, single-pass reranking) are each benchmarked against their own baseline, on their own hardware, in isolation.

This project builds an **effectiveness-per-latency Pareto frontier**: for a fixed machine, we run ~40–60 full retrieve-then-rank pipeline configurations (varying first-stage type, candidate depth k, and reranker) and plot nDCG@10 against p95 latency. We then re-measure that same frontier at four concurrency levels (1, 4, 16, 64 simultaneous query streams).

The headline hypothesis: **the architecture that wins at 1 QPS is not the architecture that wins at 64 QPS.** If true, "best pipeline" is not a single answer — it's a function of load, and the field's single-query benchmarking convention hides that.

### a. Why is it important? What would improve if solved?

A practitioner with a concrete SLA (e.g. "100ms p95, 16 concurrent streams") currently has no single source that tells them where to spend that budget — they have to reconcile a dozen papers that each report a win against a different baseline. Solving this gives:

- A **reusable decision surface** (frontier per concurrency level) that maps a latency/QPS budget directly to an architecture choice, instead of a pile of point comparisons.
- Direct evidence for whether load changes the optimal architecture — which, if confirmed, means current practice (tuning for single-query latency, then deploying under concurrent load) is measuring the wrong thing.
- A cost/memory/scaling model that tells you not just what wins today, but what wins at 10x corpus size — useful for anyone planning infrastructure, not just picking a paper to implement.

### b. What is the relevant literature? Does it acknowledge this gap? Have there been prior attempts?

Many top IR conferences like SIGIR, SIGIR-AP, ECIR, CHIIR, ICTIR, ICMR, and SPIRE have recently published works related to IR efficiency. SIGIR 2026 is also hosting the fifth ReNeuIR workshop specifically on neural IR efficiency — the community has accepted efficiency as a first-class axis but has not standardized how to compare across techniques.

Three specific gaps, each with a concrete paper illustrating it:

1. **No common budget.** Carlson et al. (2025, 2026) report pruning speedups for learned sparse retrieval; Nardini et al. (2025) report inference-free sparse retrieval quality; Teofili & Lin (2025) report HNSW early-termination savings. Each is a real win, each is measured against a different baseline on different hardware — none tells you which to adopt first under a fixed budget.
2. **No load axis.** Latency is almost always reported single-query. Calugaru et al. (2026) are the one exception — they make pruning aggressiveness load-sensitive, which *implies* the frontier itself shifts with concurrency, but they don't measure the frontier directly.
3. **No allocation view.** The literature optimizes each stage independently (first-stage retrieval, candidate depth, reranker) rather than treating "given L ms, how do I split it across all three" as one constrained problem.

The closest existing artifact is Fröbe et al.'s `lsr_benchmark` (2026), which we plan to study early — both to potentially reuse harness components and to sharpen our contribution by understanding what it does *not* cover. The paper currently is paywalled, so we have to arrange something to get access to it.

No prior work we found runs this specific joint study: same machine, same budget, multiple architectures, multiple concurrency levels, one frontier per level.

## 2. What is the plan?

**Team size:** 2. 
**Compute:** one consistent machine, GPU optional. 
**Data risk:** none — all data (MS MARCO passage v1, TREC DL 19/20 judgments, BEIR subset) is public and downloadable immediately, and no model training is required (public checkpoints only).

### The competing arms

Each arm is a full retrieve-then-rank pipeline, crossed with candidate depth **k ∈ {100, 250, 500, 1000}** and, for the pruning arms, 2–3 aggressiveness settings — roughly 40–60 measured configurations total.

| Arm | First stage | Reranker | Why it is in the study |
|---|---|---|---|
| A1 | BM25 (Lucene/PISA) | MiniLM cross-encoder | The baseline everyone claims to beat |
| A2 | BM25 | No reranker | The trivial arm — establishes what reranking actually buys |
| A3 | Learned sparse (SPLADE-family) | MiniLM cross-encoder | Current sparse SOTA |
| A4 | Learned sparse + superblock/block-max pruning | MiniLM cross-encoder | Tests whether pruning moves the frontier or just the point |
| A5 | Dense bi-encoder + HNSW | MiniLM cross-encoder | Dense baseline |
| A6 | Dense + compressed embeddings (PQ / Matryoshka truncation) | MiniLM cross-encoder | Memory-vs-latency-vs-quality trade |
| A7 | Any first stage | Single-pass listwise (JointRank-style) | Reranking at a different cost point |

**Arm A2 is not optional.** If BM25 with no reranker sits on the frontier in the tightest latency regime, that is the study's most useful finding, and we want it established early rather than discovered late.

### a. Threads, deliverables, and how they justify the team size / resource fit

The work splits into a small number of concrete threads, each tied to specific papers or components and each ending in a defined deliverable:

1. **Measurement harness.** Build the latency measurement harness first, before any arm is added, and validate it against a published BM25 latency number before trusting any further result. Study Fröbe et al.'s `lsr_benchmark` in this thread for reusable components. *Deliverable: a validated harness that reproduces a known BM25 latency figure, plus an indexed MS MARCO collection for BM25 and one dense arm.*
2. **Core arms — A1, A2, A5.** BM25+MiniLM cross-encoder (the baseline), BM25 with no reranker (isolates what reranking actually buys), and a dense bi-encoder + HNSW baseline. This is the go/no-go gate for the rest of the project. *Deliverable: A1/A2/A5 running end-to-end, first Pareto frontier at concurrency 1.*
3. **Remaining arms and concurrency sweep**, split across the two members so the sparse/pruning line and the dense/compression/reranking line run in parallel — the concrete reason two people are needed rather than one, since the arms are independent enough to parallelize but each requires enough setup that serial execution wouldn't fit the semester. Adds learned-sparse (per Nardini et al.), pruning on top of it (per Carlson et al.), embedding compression (per Caspari et al.'s CoRECT framework), and single-pass listwise reranking (Dedov's JointRank). *Deliverable: full arm grid (A1–A7) measured across all four concurrency levels, plus the BEIR out-of-domain check.*
4. **Ablations and cost model.** Index residency (in-memory vs. mmap), CPU-only vs. GPU reranking, query stratification by difficulty, reranker capacity vs. depth, batch-vs-streaming. *Deliverable: ablation results, bootstrap confidence intervals for every effectiveness claim, and the cost/scaling analysis.*
5. **Synthesis.** *Deliverable: the final frontier figure (one panel per concurrency level) and a report whose central claim is a measured statement of the form "at a 50 ms p95 budget and 16 concurrent streams, arm X dominates; at 200 ms and 1 stream, arm Y does."*

**Skills required:** standard IR/ML engineering (indexing with Lucene/PISA or equivalent, running public sparse/dense retrieval and cross-encoder checkpoints, basic statistics for paired bootstrap CIs) — no research-level modeling or novel training is needed, since the contribution is the joint measurement, not a new technique. Both team members' existing systems/ML engineering background covers this.

**Hardware required:** one dedicated, consistent machine (CPU governor pinned, no other load) for the duration of the measurement runs, with GPU access needed only for the reranking-hardware ablation — probably possible natively in our local machine (Apple MacBook Pro with M1 Pro). If not possible, IIITH's Ada/Lovelace Clusters will be more than enough we believe.

**Fallback if scope is too large:** if A1/A2/A5 are not solid by the core-arms gate, drop A6/A7 (compression, listwise reranking) rather than run every arm poorly.

## References

- Carlson et al. (SIGIR 2025) — *Dynamic Superblock Pruning for Fast Learned Sparse Retrieval* — https://doi.org/10.1145/3726302.3730183
- Carlson et al. (SIGIR 2026) — *Efficient Sparse Retrieval with Lightweight Superblock Pruning* — https://doi.org/10.1145/3805712.3809739
- Nardini et al. (SIGIR 2025) — *Effective Inference-Free Retrieval for Learned Sparse Representations* — https://doi.org/10.1145/3726302.3730185
- Caspari et al. (ECIR 2026) — *CoRECT: A Framework for Evaluating Embedding Compression Techniques at Scale* — https://doi.org/10.1007/978-3-032-21321-1_48
- Teofili & Lin (ECIR 2025) — *Patience in Proximity: A Simple Early Termination Strategy for HNSW Graph Traversal in Approximate k-Nearest Neighbor Search* — https://doi.org/10.1007/978-3-031-88714-7_39
- Calugaru et al. (SIGIR 2026) — *Load-sensitive Selective Pruning in Dense Retrieval* — https://doi.org/10.1145/3805712.3809875
- Dedov (ICTIR 2025) — *JointRank: Rank Large Set with Single Pass* — https://doi.org/10.1145/3731120.3744587
- Fröbe et al. (ECIR 2026) — *Evaluating the Efficiency and Effectiveness of Learned Sparse Retrieval with the lsr_benchmark* — https://doi.org/10.1007/978-3-032-21321-1_57
- Fröbe et al. (SIGIR 2026) — *ReNeuIR at SIGIR 2026: The Fifth Workshop on Reaching Efficiency in Neural Information Retrieval* — https://doi.org/10.1145/3805712.3808657