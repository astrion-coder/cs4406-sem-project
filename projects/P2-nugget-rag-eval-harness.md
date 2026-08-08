# P2 — A nugget-based RAG evaluation harness cheap enough to run in CI

**Thread:** evaluation / methodology · **Team size:** 2–3 · **Compute:** CPU + LLM API budget · **Data risk:** low

---

## 1. The question

> Can a nugget bank be compressed — fewer nuggets, better chosen — without changing the **system
> ordering** it produces, and what does the compression-versus-fidelity frontier look like?

A nugget bank is the current standard instrument for evaluating RAG and report-generation systems:
decompose the ideal answer into atomic facts, then score each system by which nuggets its output
covers. It works. It is also expensive enough that teams run it once per paper rather than once per
commit — and an evaluation you cannot afford to run on every model change is a fundamentally
different product from one you can.

The deliverable is a **cost-versus-ordering-fidelity frontier**: how few nuggets can you keep, chosen
how, before the evaluation starts ranking systems differently from the full bank?

## 2. Why this is worth answering now

From the 2024–2026 corpus:

- **Benchmarks and test collections is the single fastest-growing theme in the field** — 14% of
  papers in 2024, 27% in 2026. RAG itself went 4% → 12%. The field's centre of gravity moved from
  building RAG systems to measuring them.
- Nugget-based evaluation specifically has gone from one paper to a cluster in two years: automated
  nugget extraction and recall, nugget-based response generation, report-generation evaluation
  frameworks, cross-task LLM-judge benchmarks, automated nugget generation, nugget-based diagnostics
  for LLM response arenas, and nugget coverage as a reranking signal.
- Dietz et al. (SIGIR 2026) name the problem in their title — nugget banks contain **too many
  questions** — and propose deriving concise banks. That paper is the closest prior work and the
  direct launching point for this project.

## 3. The gap in prior work, stated precisely

1. **Construction and scoring are studied separately.** Papers propose either better nugget
   extraction or better nugget assignment. The joint question — *given a fixed judging budget, what
   is the best bank size and selection method* — is not addressed.
2. **Cost is rarely reported at all.** Nugget-based evaluations report agreement with human
   judgments. They do not report dollars or seconds per topic, so there is no way to compare methods
   at matched cost.
3. **The right fidelity metric is under-used.** Papers report per-system score correlation with human
   evaluation. What an evaluation is *for* is ordering systems — and score correlation can be high
   while the ordering of the top few systems, which is the decision anyone actually makes, is
   unstable.
4. **The trivial baseline is usually absent.** If randomly keeping N nuggets preserves the ordering
   as well as a clever selection method keeping N nuggets, the clever method is not earning its
   complexity. Dietz et al.'s concise-bank work makes this comparison necessary and we make it
   central.

## 4. What we implement (the competing arms)

Given a full nugget bank per topic, produce a size-N bank by:

| Arm | Selection method | Hypothesis |
|---|---|---|
| C0 | **Full bank** | Reference — defines "correct" ordering |
| C1 | **Random subsample at size N** | The honest baseline. Everything must beat this |
| C2 | **Discriminative pruning** — drop nuggets that nearly all or nearly no systems cover | Nuggets with no variance carry no ranking signal |
| C3 | **Semantic deduplication** — embed nuggets, cluster, keep one representative per cluster | Banks contain paraphrase redundancy |
| C4 | **LLM-merged bank** — ask the judge model to consolidate overlapping nuggets | Tests whether an LLM can do C3 better than embeddings |
| C5 | **Importance-weighted** — keep nuggets marked vital vs. okay (where the collection provides this) | Uses existing metadata for free |
| C6 | **C2 + C3 combined** | Whether the effects compose |

Sizes N swept over roughly {10%, 25%, 50%, 75%} of the full bank.

**Arm C1 is the spine of the project.** Build it first, in week 4. If random subsampling to 25%
preserves system ordering perfectly, the result is that nugget banks are massively over-specified —
which is a genuinely useful finding, arrives early, and reframes the rest of the semester.

## 5. Data

- **TREC RAG 2024 / 2025** — topics, released runs, and nugget resources. This is the only collection
  purpose-built for the task. Confirm availability of the run files in **week 1**; the whole project
  depends on having many systems to order.
- **Fallback if runs are unavailable:** generate 8–12 RAG systems ourselves by crossing
  {BM25, dense, hybrid} retrieval × {2–3 generator models} × {top-3, top-10 context}. This is a
  day of work and gives full control over the system pool. Having this fallback specified is why the
  project's data risk is "low" rather than "medium".
- Optionally **Researchy Questions** for harder, multi-perspective topics as an external validity
  check.

## 6. Evaluation protocol

**Primary metric — ordering fidelity.** For each compressed bank, compute each system's nugget-recall
score, rank the systems, and compare that ranking to the full-bank ranking using:
- Kendall's τ over all systems;
- **Rank-biased overlap (RBO)** with a top-weighted setting — because getting the top-3 right matters
  more than getting the bottom-20 right;
- **Top-1 flip rate** and **top-3 set difference** across bootstrap resamples of topics — the
  operationally meaningful failure.

**Secondary:** per-system absolute score error vs. full bank.

**Cost:** judge tokens, dollars, and wall-clock seconds per topic at each configuration. Every point
on the frontier carries a price tag. Project to *"cost of one CI run over 500 topics"* — the number
that decides whether this evaluation can live in a build pipeline.

**Statistics:** bootstrap over topics (10,000 resamples) for every fidelity estimate; report CIs on τ
and RBO, not point estimates. The variance across topic resamples is itself a result — it bounds how
much any nugget-based evaluation can be trusted at a given topic-set size.

**Validation against humans:** where TREC provides human nugget judgments, report agreement for the
full bank so we can state how much fidelity was already lost before compression.

## 7. Ablations

1. **Nugget granularity** — atomic fact vs. sentence-level nuggets, since the compression frontier
   may simply be recovering the right granularity.
2. **Assignment strictness** — strict entailment vs. loose "supports" judging when deciding whether a
   system output covers a nugget.
3. **Judge model swap** — a small open-weight judge vs. a frontier model. If the compression ranking
   flips with the judge, the method is judge-specific and we must say so.
4. **Gaming check** — following Dietz et al. on evaluation secrets, test whether a system with access
   to the nugget bank can inflate its score, and whether compressed banks are *easier* to game
   (fewer targets) — an important safety property of any cheap evaluation.
5. **Topic-set size** — how many topics are needed for a stable ordering at each bank size. Trading
   nuggets-per-topic against number-of-topics under a fixed total budget is the real allocation
   question and this ablation answers it.

## 8. Scaling, cost, and failure modes

- **Cost model:** tokens per topic decomposed into extraction, assignment, and aggregation. $/topic
  and $/CI-run at each frontier point.
- **Where it breaks at 10×:** 10× more systems in the pool means ordering is harder to preserve
  (more near-ties); 10× more topics means cost scales linearly while fidelity improves sub-linearly.
  Both directions should be measured on subsamples and extrapolated.
- **Failure modes:** compressed banks that systematically favour verbose outputs; nuggets whose
  removal disproportionately hurts one system family; judge drift between runs (measure by re-running
  one configuration at the end of the project and comparing); topic-level heterogeneity where some
  topics need the full bank and others need three nuggets — which suggests **per-topic adaptive bank
  sizing** as a natural extension if time allows.

## 9. Milestones

| Week | Dates | Milestone | Deliverable in this repo |
|---|---|---|---|
| 1–2 | to 30 Aug | Team declared; TREC RAG resources secured **or** fallback system pool decided | `docs/data_availability.md` |
| 2 | by **31 Aug** | **Initial proposal** — survey, question, testbed, baselines, metrics, cost envelope | `docs/proposal.md` |
| 3 | early Sep | Full-bank pipeline reproduces published nugget-recall numbers on a known run | `src/nugget/`, `results/baseline_repro.md` |
| 4 | mid Sep | **Arm C1 (random) complete**, first fidelity frontier | `results/frontier_random.csv` |
| 5–6 | by **30 Sep** | **Problem finalization** — deeper survey, gap sharpened against the pilot frontier, working pipeline | `docs/problem_statement.md` |
| 7–10 | Oct | Arms C2–C6; full size sweep; cost accounting instrumented throughout | `results/` full grid |
| 11–12 | early Nov | Ablations, especially judge-swap and gaming check; bootstrap CIs | `analysis/`, `figures/` |
| 13–14 | mid Nov | Presentation; demo (upload runs → get a cheap ordering + its confidence) | `docs/slides.pdf`, `demo/` |
| 15 | **20 Nov** | **Final report** | `docs/final_report.pdf` |

## 10. Reading list

All entries verified present in the 2024–2026 proceedings corpus; DOIs resolve.

- Pradeep et al. (SIGIR 2025) — *The Great Nugget Recall: Automating Fact Extraction and RAG Evaluation with Large Language Models* — [doi:10.1145/3726302.3730090](https://doi.org/10.1145/3726302.3730090) · open access
- Dietz et al. (SIGIR 2026) — *Too Many Questions: Deriving Concise and Effective Nugget Banks* — [doi:10.1145/3805712.3809981](https://doi.org/10.1145/3805712.3809981) · open access
- Walden et al. (SIGIR 2026) — *Auto-ARGUE: LLM-Based Report Generation Evaluation* — [doi:10.1145/3805712.3808384](https://doi.org/10.1145/3805712.3808384) · open access
- Farzi et al. (SIGIR 2026) — *Auto-Judge: A Cross-Task Benchmark for Comparing LLM Judges for Citation-Grounded RAG Systems* — [doi:10.1145/3805712.3808601](https://doi.org/10.1145/3805712.3808601) · open access
- Wallat et al. (ICTIR 2025) — *Correctness is not Faithfulness in Retrieval Augmented Generation Attributions* — [doi:10.1145/3731120.3744592](https://doi.org/10.1145/3731120.3744592)
- Dietz et al. (ECIR 2026) — *Insider Knowledge: How Much Can RAG Systems Gain from Evaluation Secrets?* — [doi:10.1007/978-3-032-21289-4_37](https://doi.org/10.1007/978-3-032-21289-4_37)
- Gienapp et al. (SIGIR 2025) — *The Viability of Crowdsourcing for RAG Evaluation* — [doi:10.1145/3726302.3730093](https://doi.org/10.1145/3726302.3730093) · open access
- Zerhoudi et al. (SIGIR 2026) — *NuggetIndex: Governed Atomic Retrieval for Maintainable RAG* — [doi:10.1145/3805712.3809687](https://doi.org/10.1145/3805712.3809687) · open access
- Li et al. (ICTIR 2026) — *DoGMaTiQ: Automated Generation of Question-and-Answer Nuggets for Report Evaluation* — [doi:10.1145/3805713.3820425](https://doi.org/10.1145/3805713.3820425) · open access
- Sharifymoghaddam et al. (ICTIR 2026) — *Search Arena Meets Nuggets: Towards Explanations and Diagnostics in the Evaluation of LLM Responses* — [doi:10.1145/3805713.3820418](https://doi.org/10.1145/3805713.3820418) · open access
- Ju et al. (ECIR 2026) — *LANCER: LLM Reranking for Nugget Coverage* — [doi:10.1007/978-3-032-21300-6_12](https://doi.org/10.1007/978-3-032-21300-6_12)
- Łajewska & Balog (SIGIR 2025) — *GINGER: Grounded Information Nugget-Based Generation of Responses* — [doi:10.1145/3726302.3730166](https://doi.org/10.1145/3726302.3730166) · open access
- Brådland et al. (SIGIR 2025) — *A New HOPE: Domain-agnostic Automatic Evaluation of Text Chunking* — [doi:10.1145/3726302.3729882](https://doi.org/10.1145/3726302.3729882) · open access
- Bennani & Moslonka (ECIR 2026) — *A Systematic Analysis of Chunking Strategies for Reliable Question Answering* — [doi:10.1007/978-3-032-21321-1_9](https://doi.org/10.1007/978-3-032-21321-1_9)
- Zhou et al. (SIGIR 2026) — *Beyond Chunk-Then-Embed: A Comprehensive Taxonomy and Evaluation of Document Chunking Strategies for Information Retrieval* — [doi:10.1145/3805712.3808575](https://doi.org/10.1145/3805712.3808575) · open access
- Macdonald et al. (SIGIR 2025) — *Constructing and Evaluating Declarative RAG Pipelines in PyTerrier* — [doi:10.1145/3726302.3730150](https://doi.org/10.1145/3726302.3730150) · open access
- Krayko et al. (ECIR 2025) — *RURAGE: Robust Universal RAG Evaluator for Fast and Affordable QA Performance Testing* — [doi:10.1007/978-3-031-88720-8_23](https://doi.org/10.1007/978-3-031-88720-8_23)
- Carmel et al. (SIGIR 2026) — *LiveRAG: A Diverse Q&A Dataset with Varying Difficulty Level for RAG Evaluation* — [doi:10.1145/3805712.3808585](https://doi.org/10.1145/3805712.3808585) · open access
- Abbasiantaeb et al. (SIGIR 2025) — *Conversational Gold: Evaluating Personalized Conversational Search System Using Gold Nuggets* — [doi:10.1145/3726302.3730316](https://doi.org/10.1145/3726302.3730316) · open access
- Dietz et al. (ECIR 2026) — *Incorporating Q&A Nuggets Into Retrieval-Augmented Generation* — [doi:10.1007/978-3-032-21300-6_20](https://doi.org/10.1007/978-3-032-21300-6_20)

## 11. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| TREC RAG run files not obtainable | Medium | Fallback system pool (§5) decided in week 1, not week 8 |
| LLM API cost overruns | Medium | Small open-weight judge for the bulk sweep; frontier model only for the judge-swap ablation |
| Too few systems to order meaningfully | Medium | Need ≥10 systems for τ to be informative; generate more if the pool is thin |
| Judge non-determinism swamps the effect | Medium | Fix temperature to 0, seed where possible, and measure run-to-run variance explicitly as a floor on detectable differences |
| Result is "random works fine" | Low-medium | That is a strong, publishable finding — write it up as the headline rather than hunting for a win |

## 12. What "done" looks like

A frontier plot with judging cost per topic on one axis and top-weighted ordering fidelity on the
other, one curve per selection method, with the random baseline plotted and bootstrap CIs on every
point — plus a repository that takes a set of runs and a budget and returns a system ordering with a
stated confidence.
