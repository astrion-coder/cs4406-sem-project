# P7 — Designing a two-stage retrieval pipeline to a latency SLA

**Thread:** systems / efficiency · **Team size:** 2 · **Compute:** one consistent machine, GPU optional · **Data risk:** none

---

## 1. The question

> Under a hard p95 latency budget, what is the optimal allocation of that budget across first-stage
> retrieval, candidate depth, and reranking — and does the optimum shift when the system is under
> concurrent load?

Every deployed search system answers this question implicitly. Almost no published paper answers it
explicitly, because efficiency techniques are benchmarked one at a time against their own baseline
rather than jointly under a shared budget.

The deliverable is an **effectiveness-per-latency Pareto frontier**, measured on one machine, with the
frontier re-measured at several concurrency levels. The headline claim we are trying to establish or
refute: *the architecture that wins at 1 QPS is not the architecture that wins at 64 QPS.*

## 2. Why this is worth answering now

From the 2024–2026 corpus of 3,440 papers across SIGIR, SIGIR-AP, ECIR, CHIIR, ICTIR, ICMR and SPIRE:

- Efficiency / latency / indexing is the **second-largest theme in the field** and still growing —
  17% of papers in 2024, 23% in 2026.
- The sub-literature is unusually rich right now and, critically, **mutually incomparable**:
  superblock pruning for learned sparse retrieval, inference-free sparse representations, embedding
  compression, HNSW early termination, load-sensitive pruning for dense retrieval, single-pass
  listwise ranking. Each reports a win against its own baseline on its own hardware.
- SIGIR 2026 hosts the **fifth ReNeuIR workshop** on efficiency in neural IR — a sign the community
  has accepted efficiency as a first-class axis but has not settled how to compare across it.

The gap this project fills is not a new technique. It is the **joint allocation study** that lets
someone with a 100 ms budget decide where to spend it — and the observation of how that decision
changes under load, which single-query benchmarking cannot show.

## 3. The gap in prior work, stated precisely

Three specific things are missing from the papers listed in §10:

1. **No common budget.** Carlson et al. report pruning speedups; Nardini et al. report inference-free
   sparse retrieval quality; Teofili & Lin report HNSW traversal savings. All three are wins, all
   three are measured against different baselines on different hardware, and none of them tells you
   which to adopt first when your budget is 100 ms.
2. **No load axis.** Latency is almost always reported single-query. Calugaru et al. (SIGIR 2026) are
   an exception in making pruning aggressiveness load-sensitive — which implies, but does not
   measure, that the effectiveness/latency frontier itself moves with concurrency.
3. **No allocation view.** The literature optimizes stages independently. The practitioner's problem
   is a constrained allocation: given L ms total, choose first-stage type, candidate depth k, and
   reranker capacity jointly.

Fröbe et al.'s `lsr_benchmark` (ECIR 2026) is the closest existing artifact and should be studied
early — it may provide harness components, and understanding what it does *not* cover sharpens our
contribution statement.

## 4. What we implement (the competing arms)

Each arm is a full retrieve-then-rank pipeline. All use public checkpoints; **no model training is
required**, which is what makes this finishable in one semester.

| Arm | First stage | Reranker | Why it is in the study |
|---|---|---|---|
| A1 | BM25 (Lucene/PISA) | MiniLM cross-encoder | The baseline everyone claims to beat |
| A2 | BM25 | No reranker | The trivial arm — establishes what reranking actually buys |
| A3 | Learned sparse (SPLADE-family) | MiniLM cross-encoder | Current sparse SOTA |
| A4 | Learned sparse + superblock/block-max pruning | MiniLM cross-encoder | Tests whether pruning moves the frontier or just the point |
| A5 | Dense bi-encoder + HNSW | MiniLM cross-encoder | Dense baseline |
| A6 | Dense + compressed embeddings (PQ / Matryoshka truncation) | MiniLM cross-encoder | Memory-vs-latency-vs-quality trade |
| A7 | Any first stage | Single-pass listwise (JointRank-style) | Reranking at a different cost point |

Crossed with **candidate depth k ∈ {100, 250, 500, 1000}** and, for the pruning arms, 2–3
aggressiveness settings. That is roughly 40–60 measured configurations — enough for a frontier,
small enough to run repeatedly.

**Arm A2 is not optional.** If BM25 with no reranker sits on the frontier in the tightest latency
regime, that is the paper's most useful finding and we want it in week 6, not week 15.

## 5. Data and testbed

- **MS MARCO passage v1** (8.8M passages) with **TREC Deep Learning 2019/2020** judgments as the
  primary collection. Public, standard, and the one collection where every arm above has a released
  checkpoint.
- **BEIR subset** (3–4 datasets: NFCorpus, SciFact, TREC-COVID, FiQA) as an out-of-domain
  generalization check. Small enough to add without doubling the work.
- Query load for the concurrency experiments: sample from the MS MARCO dev query log so the query
  mix is realistic rather than uniform.

Everything is downloadable in week 1. There is no data-access risk on this project, which is the
main reason it is the safest of the three.

## 6. Evaluation protocol

**Effectiveness:** nDCG@10 and Recall@1000 on TREC DL 19/20; nDCG@10 on each BEIR set.

**Efficiency:** end-to-end query latency measured **inside the process**, reported as p50 / p95 / p99.
Never report mean latency. Measure at concurrency levels **{1, 4, 16, 64}** simultaneous query
streams.

**Benchmarking discipline** — this is where projects like this usually fail, so it is written down as
a requirement:

- Fixed machine, no other load, CPU governor pinned to performance.
- 500-query warm-up discarded before every measurement run.
- 3 independent repetitions per configuration; report the median of per-repetition p95 and the
  spread across repetitions.
- Cold-cache and warm-cache measured separately and labelled.
- Report index build time and resident memory for every arm — a configuration that needs 60 GB of
  RAM is not comparable to one that needs 4 GB even at equal latency.

**Statistics:** paired bootstrap (10,000 resamples) over queries for every effectiveness difference we
claim; 95% CI must exclude zero. Latency differences are reported as distributions, not tested.

**The headline figure:** nDCG@10 against p95 latency, one point per configuration, Pareto frontier
traced, **one panel per concurrency level**. If the frontier's composition changes across panels, the
project's central claim is established.

## 7. Ablations

1. **Index residency** — in-memory vs. memory-mapped from SSD. Changes the frontier's shape.
2. **Reranking hardware** — CPU-only vs. one GPU. Determines whether a cross-encoder is even
   admissible at a given SLA.
3. **Query stratification** — split results by query length and by query difficulty (using a QPP
   score or the per-query nDCG of the BM25 baseline). Efficiency techniques usually hurt hard queries
   most, and averaging hides it.
4. **Reranker capacity** — MiniLM-L2 / L6 / L12 at fixed depth, to separate "rerank more candidates"
   from "rerank with a bigger model" as two ways to spend the same milliseconds.
5. **Batch vs. streaming** — whether the arm's advantage survives when queries arrive one at a time.

## 8. Scaling, cost, and failure modes

Required by the course rubric and genuinely the most interesting section here.

- **Cost model:** queries-per-second-per-dollar, using published cloud instance pricing for the
  measured machine class. Report $/1M queries for each frontier point.
- **Memory:** resident index size per arm; the crossover corpus size at which each arm stops fitting
  on one machine.
- **Where it breaks at 10×:** project each arm to a 100M-passage corpus. Sparse index size grows
  roughly linearly; HNSW graph memory grows super-linearly in practice; cross-encoder cost is
  invariant to corpus size but linear in k. Those three different scaling behaviours mean the ranking
  of arms at 10× is *predictably* different from the ranking at 1×, and we say so with numbers.
- **Failure modes:** tail latency under pruning (pruning improves the mean and can worsen p99);
  quality cliff when compression is too aggressive; cache-cold behaviour after a deploy; head-of-line
  blocking at high concurrency.

## 9. Milestones

| Week | Dates | Milestone | Deliverable in this repo |
|---|---|---|---|
| 1–2 | to 30 Aug | Team declared; environment and machine fixed | `env/`, `README` with hardware spec |
| 2 | by **31 Aug** | **Initial proposal** — literature survey, question, testbed, baselines, metrics, target SLA and cost envelope | `docs/proposal.md` |
| 3–4 | early Sep | MS MARCO indexed for BM25 + one dense arm; harness measures latency correctly | `src/index/`, `src/bench/` |
| 5–6 | mid Sep | Arms A1, A2, A5 running end-to-end; first frontier at concurrency 1 | `results/frontier_v0.csv`, first plot |
| 7–8 | by **30 Sep** | **Problem finalization** — deeper survey, gap statement sharpened against measured pilot data, working pipeline | `docs/problem_statement.md` |
| 9–11 | Oct | Remaining arms (A3, A4, A6, A7); concurrency sweep; BEIR out-of-domain | `results/` full grid |
| 12–13 | early Nov | Ablations; bootstrap CIs; cost model | `analysis/`, `figures/` |
| 14 | mid Nov | Presentation; demo (live query → latency breakdown) | `docs/slides.pdf`, `demo/` |
| 15 | **20 Nov** | **Final report** | `docs/final_report.pdf` |

## 10. Reading list

All entries verified present in the 2024–2026 proceedings corpus; DOIs resolve.

- Carlson et al. (SIGIR 2025) — *Dynamic Superblock Pruning for Fast Learned Sparse Retrieval* — [doi:10.1145/3726302.3730183](https://doi.org/10.1145/3726302.3730183) · open access
- Carlson et al. (SIGIR 2026) — *Efficient Sparse Retrieval with Lightweight Superblock Pruning* — [doi:10.1145/3805712.3809739](https://doi.org/10.1145/3805712.3809739) · open access
- Nardini et al. (SIGIR 2025) — *Effective Inference-Free Retrieval for Learned Sparse Representations* — [doi:10.1145/3726302.3730185](https://doi.org/10.1145/3726302.3730185) · open access
- Caspari et al. (ECIR 2026) — *CoRECT: A Framework for Evaluating Embedding Compression Techniques at Scale* — [doi:10.1007/978-3-032-21321-1_48](https://doi.org/10.1007/978-3-032-21321-1_48)
- Teofili & Lin (ECIR 2025) — *Patience in Proximity: A Simple Early Termination Strategy for HNSW Graph Traversal in Approximate k-Nearest Neighbor Search* — [doi:10.1007/978-3-031-88714-7_39](https://doi.org/10.1007/978-3-031-88714-7_39)
- Munyampirwa et al. (ECIR 2026) — *Down with the Hierarchy: The ‘H’ in HNSW Stands for “Hubs”* — [doi:10.1007/978-3-032-21324-2_3](https://doi.org/10.1007/978-3-032-21324-2_3)
- Calugaru et al. (SIGIR 2026) — *Load-sensitive Selective Pruning in Dense Retrieval* — [doi:10.1145/3805712.3809875](https://doi.org/10.1145/3805712.3809875) · open access
- Rathee et al. (SIGIR 2025) — *Breaking the Lens of the Telescope: Online Relevance Estimation over Large Retrieval Sets* — [doi:10.1145/3726302.3729910](https://doi.org/10.1145/3726302.3729910) · open access
- Dedov (ICTIR 2025) — *JointRank: Rank Large Set with Single Pass* — [doi:10.1145/3731120.3744587](https://doi.org/10.1145/3731120.3744587) · open access
- Fröbe et al. (ECIR 2026) — *Evaluating the Efficiency and Effectiveness of Learned Sparse Retrieval with the lsr_benchmark* — [doi:10.1007/978-3-032-21321-1_57](https://doi.org/10.1007/978-3-032-21321-1_57)
- Martinico et al. (SIGIR 2025) — *Efficient Approximate Nearest Neighbor Search on a Raspberry Pi* — [doi:10.1145/3726302.3730268](https://doi.org/10.1145/3726302.3730268) · open access
- Alkawaas & MacAvaney (SIGIR 2026) — *The Matryoshka Hypencoder* — [doi:10.1145/3805712.3809980](https://doi.org/10.1145/3805712.3809980) · open access
- Yang et al. (ECIR 2026) — *RoutIR: Fast Serving of Retrieval Pipelines for Retrieval-Augmented Generation* — [doi:10.1007/978-3-032-21321-1_60](https://doi.org/10.1007/978-3-032-21321-1_60)
- Chandra et al. (ECIR 2026) — *LURE-RAG: Lightweight Utility-Driven Reranking for Efficient RAG* — [doi:10.1007/978-3-032-21289-4_36](https://doi.org/10.1007/978-3-032-21289-4_36) · open access
- Zhuang et al. (SIGIR 2026) — *Layer-wise Token Compression for Efficient Document Reranking* — [doi:10.1145/3805712.3809871](https://doi.org/10.1145/3805712.3809871) · open access
- Yang et al. (SIGIR 2026) — *ColBERTSaR: Sparsified ColBERT Index via Product Quantization* — [doi:10.1145/3805712.3809920](https://doi.org/10.1145/3805712.3809920) · open access
- Kankanampati et al. (SIGIR 2026) — *A Voronoi Cell Formulation for Principled Token Pruning in Late-Interaction Retrieval Models* — [doi:10.1145/3805712.3809726](https://doi.org/10.1145/3805712.3809726) · open access
- Fröbe et al. (SIGIR 2026) — *ReNeuIR at SIGIR 2026: The Fifth Workshop on Reaching Efficiency in Neural Information Retrieval* — [doi:10.1145/3805712.3808657](https://doi.org/10.1145/3805712.3808657) · open access
- Ghosh et al. (SIGIR 2026) — *Reproduction Beyond Benchmarks: ConstBERT and ColBERT-v2 Across Backends and Query Distributions* — [doi:10.1145/3805712.3808561](https://doi.org/10.1145/3805712.3808561) · open access
- Bao et al. (SIGIR 2026) — *HybridSparse: An End-to-End Hybrid Framework for Efficient Large-Scale Retrieval* — [doi:10.1145/3805712.3808415](https://doi.org/10.1145/3805712.3808415) · open access

## 11. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Sloppy benchmarking invalidates results | **High** — this is the real risk | Write the measurement harness first, in week 3, and validate it reproduces a published BM25 latency number before adding arms |
| Too many arms, none finished well | Medium | A1/A2/A5 by week 6 is the gate; drop A6/A7 rather than run everything badly |
| Machine contention (shared lab hardware) | Medium | Secure a dedicated machine in week 1; if impossible, use a cloud instance and say so |
| Cross-encoder too slow on CPU to admit at any SLA | Low | That is a finding, not a failure — report it |

## 12. What "done" looks like

A repository that, on one command, rebuilds every index, runs every configuration at every
concurrency level, and regenerates the frontier figure — plus a report whose central claim is a
measured statement of the form *"at a 50 ms p95 budget and 16 concurrent streams, arm X dominates;
at 200 ms and 1 stream, arm Y does."*
