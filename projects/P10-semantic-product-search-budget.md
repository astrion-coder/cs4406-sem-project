# P10 — Semantic product search on a budget: where does the money go?

**Thread:** systems + ML, applied · **Team size:** 2–3 · **Compute:** CPU + one small GPU · **Data risk:** none

---

## 1. The question

> At a matched latency and dollar budget, which architecture buys the most relevance over a lexical
> baseline for e-commerce queries: a cross-encoder reranker, a late-interaction retriever, or
> LLM-based query understanding?

Every industry paper in this space reports *"we deployed X and relevance went up Y%"*. None of them
compares X against the alternatives their competitors deployed, because no company runs three
architectures in production simultaneously. A public, matched-budget comparison is therefore
something only an academic project can produce — and it is exactly what a team choosing an
architecture would want to read.

The deliverable is a **relevance-per-dollar frontier** on a public product-search collection, with
per-segment breakdowns (head/tail, typo/clean, short/long) because the aggregate number hides where
each architecture actually earns its cost.

## 2. Why this is worth answering now

From the 2024–2026 corpus:

- E-commerce / product search grew from **4.0% to 6.6%** of papers across the window, and the SIGIR
  industry track in 2025–26 is unusually candid about cost.
- The published deployments are architecturally *divergent*: Walmart deployed a BERT cross-encoder
  reranker; IKEA deployed a late-interaction (ColBERT-style) retriever; Alipay deployed generative
  retrieval and had to engineer around hallucination; other teams report LLM-based query
  understanding and prompt-scaling approaches. Same problem, four different answers, four different
  cost profiles.
- Cost is now explicitly in the titles — inference acceleration, cost-efficient copilots,
  environment-aware planning to avoid unnecessary computation. The field has stopped pretending
  serving is free.

The gap: **nobody has run these architectures against each other under a common budget on a common
public collection.** That comparison is the project.

## 3. The gap in prior work, stated precisely

1. **Incomparable evidence.** Industry-track papers report internal A/B lifts on proprietary traffic.
   The lift numbers are not comparable across companies — different catalogues, different baselines,
   different query mixes. A public collection makes them comparable for the first time.
2. **Cost is described, not controlled.** Papers describe how they made their chosen architecture
   affordable. None asks whether a different architecture would have been cheaper at the same
   relevance.
3. **Segment blindness.** Aggregate nDCG hides the fact that lexical matching is near-perfect on head
   queries and that all the value of semantic retrieval is in the tail, the fuzzy, and the
   misspelled. Greyson et al. (SIGIR 2026) make this point for music search; it has not been
   quantified as a cost-allocation argument for product search.

## 4. What we implement (the competing arms)

| Arm | Description | Where the cost sits |
|---|---|---|
| B1 | BM25 over title + attributes | Baseline. Cheap everywhere |
| B2 | BM25 + small cross-encoder rerank (top-100) | Query-time GPU/CPU, linear in k |
| B3 | Dense bi-encoder retrieval (ANN index) | Index-time encoding + memory |
| B4 | Late-interaction retrieval (ColBERT-style, PLAID/ConstBERT serving) | Large index, moderate query cost |
| B5 | LLM query rewriting/expansion → BM25 | Expensive per query, cheap index; cacheable for head queries |
| B6 | B5 with a **rewrite cache** for head queries | Tests whether caching collapses B5's cost disadvantage |

Arm **B1 is the reference and must be tuned properly** (field weights, analyzer, attribute handling).
A badly-tuned BM25 makes every other arm look good and is the most common flaw in papers of this
type. Budget a full week to tuning it, and report the tuned-vs-default gap — that number alone is a
useful contribution.

Arm **B6 exists because it may win.** If caching rewrites for the head of the query distribution
makes LLM query understanding as cheap as BM25 for 90% of traffic, that reframes the entire
comparison, and it is the kind of finding that only shows up if you build the boring arm.

## 5. Data

- **Amazon ESCI** (Shopping Queries Dataset) — public, graded relevance with four labels (Exact,
  Substitute, Complement, Irrelevant), ~2.6M query-product judgments, multilingual. **Pick one locale
  (US/English) and say so explicitly**; mixing locales is a common and silently damaging shortcut.
- **WANDS** (Wayfair) as a second collection for external validity — smaller, single-locale, human-
  annotated.
- **Typo/fuzzy segment**: ESCI does not ship a misspelling split, so construct one by injecting
  realistic keyboard-adjacency and phonetic typos into a held-out query sample, at a rate calibrated
  to published e-commerce typo rates. Document the generator; ship it in the repo.

## 6. Evaluation protocol

**Effectiveness:**
- nDCG@10 using ESCI's graded labels (E=1.0, S=0.1, C=0.01, I=0.0 — the standard ESCI gain mapping;
  state it explicitly).
- **Exact-match recall@100** — the metric that matters commercially, since a substitute shown where
  an exact match exists is a lost sale.
- Per-segment: head vs. tail queries (by frequency), short vs. long, clean vs. typo-injected,
  and cold-start products (few judgments).

**Efficiency:** p50/p95 query latency at matched hardware, index build time, index memory, per-query
token cost for LLM arms.

**The matched-budget protocol** — the core methodological commitment:
1. Fix a latency SLA (p95 ≤ 150 ms end-to-end, defended in the report).
2. For each arm, tune its parameters (k, model size, quantization) to the *most effective*
   configuration that meets the SLA.
3. Compare arms only at that operating point. Also report each arm's full frontier so readers can
   pick a different SLA.

**Statistics:** paired bootstrap over queries, 95% CI excluding zero for every claimed gain. Report
per-segment CIs separately — a gain that is real on tail queries and absent on head queries is a
different product decision than a uniform gain.

## 7. Ablations

1. **Field composition** — title-only vs. title+attributes vs. title+attributes+description. Cheap
   to run and often worth more than an architecture change.
2. **Typo severity sweep** — 0%, 5%, 15%, 30% of queries corrupted. Expected to be where B4/B5
   separate from B1.
3. **Catalogue size scaling** — subsample the product set to 10%, 25%, 50%, 100% and re-measure. The
   crossover catalogue size where each architecture starts winning is a headline result.
4. **Cold-start products** — restrict evaluation to products with few or no interactions.
5. **Reranker size** — for B2, sweep model capacity at fixed k against sweeping k at fixed capacity.
6. **Cache hit rate** for B6 — measured against the real ESCI query frequency distribution, not
   assumed.

## 8. Scaling, cost, and failure modes

- **Cost model:** per-query cost decomposed into index amortization, retrieval compute, rerank
  compute, and LLM tokens. Report $/1M queries per arm at the SLA operating point, using published
  cloud pricing and measured throughput.
- **Index economics:** late-interaction indexes are large. Report GB per million products for each
  arm and the monthly memory cost that implies.
- **Where it breaks at 10×:** a 10× catalogue multiplies dense and late-interaction index cost
  roughly linearly while leaving cross-encoder query cost unchanged — so B2's relative position
  *improves* with catalogue growth while B4's degrades. Quantify this rather than asserting it.
- **Failure modes:** query rewriting that changes intent (B5 hallucinating constraints the user never
  gave — the failure Alipay's paper addresses); dense retrieval returning semantically-similar but
  commercially-wrong products (a phone case for a phone query); cache staleness in B6 when the
  catalogue changes; degradation on queries containing model numbers and part codes, where lexical
  matching is correct and semantic matching actively harmful.

That last one deserves a dedicated experiment: **numeric and part-code queries** are where semantic
retrieval reliably fails, and quantifying that failure is more useful than another average-case gain.

## 9. Milestones

| Week | Dates | Milestone | Deliverable in this repo |
|---|---|---|---|
| 1–2 | to 30 Aug | Team declared; ESCI downloaded, US locale extracted, splits fixed | `data/`, `docs/data_card.md` |
| 2 | by **31 Aug** | **Initial proposal** — survey, question, testbed, baseline, metrics, SLA + cost envelope | `docs/proposal.md` |
| 3 | early Sep | **BM25 tuned properly** and reported; evaluation harness with bootstrap CIs | `src/lexical/`, `results/b1_tuning.csv` |
| 4–5 | mid Sep | B2 and B3 running; typo generator built; first per-segment table | `src/rerank/`, `src/dense/` |
| 6–7 | by **30 Sep** | **Problem finalization** — deeper survey, sharpened gap, working pipeline on all early arms | `docs/problem_statement.md` |
| 8–10 | Oct | B4 (late interaction), B5/B6 (LLM rewriting + cache); catalogue scaling sweep | `results/` full grid |
| 11–12 | early Nov | Cost model; ablations; WANDS external validity check | `analysis/`, `figures/` |
| 13–14 | mid Nov | Presentation; demo (query → per-arm results side by side with cost) | `docs/slides.pdf`, `demo/` |
| 15 | **20 Nov** | **Final report** | `docs/final_report.pdf` |

## 10. Reading list

All entries verified present in the 2024–2026 proceedings corpus; DOIs resolve.

- Puthenputhussery et al. (SIGIR 2025) — *Large Scale Deployment of BERT Based Cross Encoder Model for Re-Ranking in Walmart Search Engine* — [doi:10.1145/3726302.3731965](https://doi.org/10.1145/3726302.3731965) · open access
- Gill et al. (SIGIR 2025) — *From Keywords to Concepts: A Late Interaction Approach to Semantic Product Search on IKEA.com* — [doi:10.1145/3726302.3731948](https://doi.org/10.1145/3726302.3731948) · open access
- Zeng et al. (SIGIR 2025) — *Examples as the Prompt: A Scalable Approach for Efficient LLM Adaptation in E-Commerce* — [doi:10.1145/3726302.3731941](https://doi.org/10.1145/3726302.3731941) · open access
- Chen et al. (SIGIR 2026) — *Probe-then-Plan: Environment-Aware Planning for Industrial E-commerce Search* — [doi:10.1145/3805712.3808459](https://doi.org/10.1145/3805712.3808459) · open access
- Greyson et al. (SIGIR 2026) — *Surface-Form Neural Sparse Retrieval: Robust Fuzzy Matching for Industrial Music Search* — [doi:10.1145/3805712.3808414](https://doi.org/10.1145/3805712.3808414) · open access
- Shen et al. (SIGIR 2025) — *Alleviating LLM-based Generative Retrieval Hallucination in Alipay Search* — [doi:10.1145/3726302.3731951](https://doi.org/10.1145/3726302.3731951) · open access
- Jindal et al. (SIGIR 2026) — *A Cost-Efficient AI Copilot for High-Volume E-commerce Customer Support* — [doi:10.1145/3805712.3808447](https://doi.org/10.1145/3805712.3808447) · open access
- Xi et al. (SIGIR 2025) — *Efficiency Unleashed: Inference Acceleration for LLM-based Recommender Systems with Speculative Decoding* — [doi:10.1145/3726302.3729961](https://doi.org/10.1145/3726302.3729961) · open access
- Luo et al. (SIGIR 2026) — *Building a Production Shopping Agent at Scale* — [doi:10.1145/3805712.3808408](https://doi.org/10.1145/3805712.3808408) · open access
- Ling et al. (SIGIR 2026) — *Synthetic Data Powers Product Retrieval for Long-tail Knowledge-Intensive Queries in E-commerce Search* — [doi:10.1145/3805712.3808409](https://doi.org/10.1145/3805712.3808409) · open access
- Alvarez et al. (SIGIR 2026) — *SIGIR 2026 Workshop on eCommerce (ECOM26)* — [doi:10.1145/3805712.3808650](https://doi.org/10.1145/3805712.3808650) · open access
- Gaggar et al. (SIGIR 2026) — *Agentic Query Reformulation for Contextualized Hyper-Personalized Product Search* — [doi:10.1145/3805712.3808502](https://doi.org/10.1145/3805712.3808502) · open access
- Shih et al. (SIGIR 2026) — *BEATS: Bootstrapping E-commerce Attribute Taxonomies for Search through Iterative Human-AI Collaboration* — [doi:10.1145/3805712.3808520](https://doi.org/10.1145/3805712.3808520) · open access
- Xu et al. (SIGIR 2026) — *Learning to Trust: Dynamic Utilization of Retrieval-Augmented Generation for E-commerce Search Relevance* — [doi:10.1145/3805712.3808439](https://doi.org/10.1145/3805712.3808439) · open access
- Fujimaki & Kato (SIGIR 2026) — *NumColBERT: Non-Intrusive Numeracy Injection for Late-Interaction Retrieval Models* — [doi:10.1145/3805712.3809688](https://doi.org/10.1145/3805712.3809688) · open access
- Wang et al. (SIGIR 2026) — *Bridging the Gap: Generative Retrieval via Query-to-Multi-Span Framework for Effective E-commerce Search* — [doi:10.1145/3805712.3808479](https://doi.org/10.1145/3805712.3808479) · open access

## 11. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Under-tuned BM25 inflates every other arm | **High** | Dedicated tuning week; report tuned-vs-default delta |
| ESCI multilingual splits silently mixed | Medium | Lock to US locale in week 1; assert locale in the data-loading code |
| LLM token budget overruns | Medium | Cache aggressively (B6 is partly this mitigation); use a small open-weight model for the bulk sweep |
| Late-interaction index too large for available RAM | Medium | Subsample catalogue; report at the sizes that fit and extrapolate honestly |
| Result is "everything beats BM25 by a bit" | Low-medium | Per-segment analysis is the defence — the interesting structure is in the segments |

## 12. What "done" looks like

A relevance-per-dollar frontier for six architectures on a public product-search collection, with
per-segment breakdowns showing *which queries* each architecture's cost actually buys, and a stated
crossover catalogue size for each. Plus a repository someone at an e-commerce company could clone and
re-run against their own catalogue.
