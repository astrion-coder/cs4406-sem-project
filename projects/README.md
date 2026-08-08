# Candidate term projects — CS4406

Three shortlisted candidates for the CS4406 term project, written against the course's project
structure: a sharp question, competing implementations, ablations, an offline or simulated A/B
evaluation, and a discussion of scaling, cost and failure modes.

## The shortlist

| | Project | Thread | Team | Compute | Data risk | Industry pull |
|---|---|---|---|---|---|---|
| [P7](P7-latency-sla-pipeline.md) | Designing a two-stage pipeline to a latency SLA | Systems / efficiency | 2 | One consistent machine, GPU optional | None | Very high — search infra |
| [P10](P10-semantic-product-search-budget.md) | Semantic product search on a budget | Systems + ML, applied | 2–3 | CPU + small GPU | None | Very high — e-commerce |
| [P2](P2-nugget-rag-eval-harness.md) | A nugget-based RAG evaluation harness cheap enough for CI | Evaluation / methodology | 2–3 | CPU + LLM API budget | Low | High — RAG evaluation |

## Course deadlines these are written against

| Date | Milestone |
|---|---|
| 30 Aug | Groups declared |
| **31 Aug** | Initial proposal — first literature survey, research question, dataset/testbed, baseline, evaluation plan, success metrics, target SLA + cost envelope |
| **30 Sep** | Problem finalization — deeper literature survey, problem statement revised and finalized with the gap in prior work identified precisely, working pipeline or experimental setup |
| **20 Nov** | Final — implementations of competing solutions, ablations, offline or simulated A/B evaluation, discussion of scaling, cost and failure modes; report + demo + presentation |

Each project file contains a week-by-week schedule mapped onto these dates.

## How these three were selected

They were drawn from a survey of **3,440 papers** across the 2024, 2025 and 2026 editions of SIGIR,
SIGIR-AP, ECIR, CHIIR, ICTIR, ICMR and SPIRE, indexed via OpenAlex and tagged against 25 topic
patterns. The trend that drove the selection: between 2024 and 2026 the field shifted from building
better rankers toward **measuring, bounding and paying for** systems that already work. Benchmarks
and test collections went from 14% to 27% of papers, RAG from 4% to 12%, efficiency and latency from
17% to 23%, reranking and distillation from 5% to 11%. Recommendation fell from 22% to 19%.

All three projects are therefore **measurement or comparison studies rather than system builds**.
That is deliberate: a comparison study has a deliverable even when the interesting arm loses, which
is what makes it finishable inside one semester.

## The design principle common to all three

Each specification includes a **baseline that comparable papers omit** — no reranker at all in P7, a
properly-tuned BM25 in P10, random nugget subsampling in P2 — and requires it be built first.

The reason is scheduling, not modesty. Reviewers and the course will both ask whether the clever
method beats the trivial one at matched cost. If the trivial baseline wins, that is the result, and
it is far better to discover it in week 6 with three months left to build the study around it than in
week 15.

## Shared methodological commitments

Taken from the course's own conventions and applied across all three:

- **Temporal, never random, splits** wherever the data has a time axis.
- **Bootstrap 95% confidence intervals** on every claimed gain; the interval must exclude zero.
- **Latency reported as percentiles** (p50/p95/p99), never as a mean.
- **Serving and scale analysis** — index memory, throughput, and a back-of-envelope cost per query at
  a stated SLA.
- **Where it breaks at 10×** — an explicit projection, with numbers, for every architecture compared.

## Provenance

Every reading-list entry in these files was verified to resolve against the indexed corpus; DOIs are
taken from the metadata record rather than written from memory. Open-access status is marked where
known. Full corpus and per-project reading lists are available on request.
