# Implementation Spec — *The Price of Verifiability in PIR: an independent, common-hardware re-evaluation*

**Project:** CS4406 semester project · **Spec version:** 1.0 (2026-10-26)
**Audience:** a coding LLM (or engineer) building the benchmark harness, plus the student team who own the scientific decisions.

> **How to use this document.** Sections 1–3 give the scientific context; the coding agent should read them but not change them. Sections 4–11 are the build contract. Section 13 is a prompt you can paste in front of this spec. Anything marked **[HUMAN]** is a decision or check the students must make; the coding agent must stop and ask instead of guessing.

---

## 1. Goal in one paragraph

Measure what verifiability costs in Private Information Retrieval (PIR), meaning protection against a server that returns wrong answers, by running **verifiable PIR schemes and their non-verifiable baselines side by side on the same hardware, the same database grid and the same measurement harness**. Existing overhead numbers are self-reported by each scheme's authors, each against their own baseline, on their own hardware and parameters. This project (a) reproduces those published overhead ratios, (b) extends the comparison to regimes the original papers stress less: small records, offline/setup cost, client storage, and amortised cost per query, and (c) compares *across* families, single-server lattice versus two-server DPF/Merkle. The deliverable is a one-command benchmark plus a report whose central claim is a measured statement such as *"for 1 GiB databases with 32-byte records, single-server verifiability costs X× in total bytes per query amortised over 100 queries, versus Y× for two-server authenticated PIR."*

## 2. Research questions and hypotheses

| ID | Question | Falsifiable hypothesis | Tier |
|---|---|---|---|
| **RQ1** Reproduction | Do the published overhead ratios hold on common hardware? | H1: online communication ratios match the published analytical values within ±5 % (communication is deterministic given parameters); compute ratios fall within the published ranges, up to a hardware factor. | 1 (must) |
| **RQ2** Grid | How does the verifiable/baseline overhead for each metric vary with database size *N·d* and record size *d*? | H2: relative overhead is largest for small records and small databases, where fixed verification costs dominate, and shrinks with database size. | 1 (must) |
| **RQ3** Amortised & cross-family | Including offline cost and client storage, amortised over *Q* queries and three network profiles, which scheme has the lowest total cost, and where does the ranking flip? | H3: single-server verifiable PIR beats two-server authenticated PIR on amortised bytes only beyond some break-even *Q*; below it, two-server wins. | 2 (should) |
| **RQ4** Misbehaviour | How does expected cost change when a server cheats with probability *f* under each scheme's detection-and-recovery policy? | H4: the cost of recovery (e.g. re-running preprocessing) dominates at *f* ≥ some threshold for preprocessing-based schemes. | 3 (stretch; simulated from measured component costs) |
| **RQ5** Updates | What is the cost to re-establish verifiable state after *u* record updates, assuming the implementations' full rebuild? | H5: rebuild cost per update makes preprocessing-based verifiable schemes the most expensive at high update rates. | 3 (stretch; derived) |

A negative result for any hypothesis is a valid result. The harness must not be tuned to confirm a hypothesis.

## 3. Published reference values (reproduction targets for RQ1)

These come from the papers and READMEs surveyed earlier. **[HUMAN]** Re-check each against the PDF and record the table or figure number in `docs/reproduction_targets.md` before comparing.

| Source | Claim | What we compare |
|---|---|---|
| SimplePIR README (Henzinger et al., USENIX Sec '23) | 1 GB DB of 2^33 1-bit entries: ~121 MB hint, ~242 KB online communication, ~10 GB/s/core server throughput (AWS c5n.metal) | Hint and online bytes exactly; throughput within a hardware factor |
| SimplePIR README | DoublePIR, same DB: 16 MB hint, 345 KB online, 7.4 GB/s/core | Same (optional arm) |
| VeriSimplePIR (de Castro & Lee, USENIX Sec '24) | Online communication ≈ 1.1–1.5× SimplePIR; online server compute ≈ SimplePIR | Ratio |
| VeriSimplePIR | Without the honest-digest assumption: 12–40 % slower compute, 40–50 % more total communication | Ratio |
| VeriSimplePIR | ~800 MB persistent client storage at their headline size | Client-storage bytes |
| Authenticated PIR (Colombo et al., USENIX Sec '23), two-server, 1 KiB records | Maximum overhead 3× user (client) time, 1.6× bandwidth versus unauthenticated DPF PIR | Ratio across DB sizes |
| Authenticated PIR, single-server | Fastest single-server authenticated scheme 30–100× more compute than fastest unauthenticated | Ratio |

## 4. Arms (schemes under test)

Use **only the authors' reference implementations**. The coding agent must **not** implement or modify any cryptography; only build fixes, adapters and instrumentation are allowed (see §10).

| Arm | Scheme | Setting | Verifiable? | Source repo (lang) | Entry point to wrap |
|---|---|---|---|---|---|
| **S0-go** | SimplePIR | 1 server, LWE | No | `github.com/ahenzinger/simplepir` (Go + C, MIT) | package `pir`: `RunPIR` / `RunPIRCompressed` in `pir/pir.go` show the full call sequence |
| **S0-cpp** | SimplePIR (as re-implemented in the VeriSimplePIR codebase) | 1 server, LWE | No | `github.com/leodec/verisimplepir` (C++, MIT) | `VeriSimplePIR(N, d, …, simplePIR=true, preproc=false)`; see `src/demo/bench/simplepir_bench.cpp` |
| **S1** | VeriSimplePIR, honest-digest assumption | 1 server, LWE | Yes | same | `VeriSimplePIR(N, d, …, preproc=true, honestHint=true)`; `src/demo/bench/preproc_pir_bench.cpp` |
| **S2** | VeriSimplePIR, full (no honest-digest assumption) | 1 server, LWE | Yes | same | `preproc=true, honestHint=false` |
| **S3** | Single-server authenticated PIR, LWE | 1 server | Yes | `github.com/dedis/apir-code` (Go, GPL-3.0; module path `github.com/si-co/vpir-code`) | primitive `cmp-vpir-lwe` (and `cmp-vpir-lwe-128`) in `simulations/simul.go`; `client.NewLWE`, `database.CreateRandomBinaryLWEWithLength` |
| **S4** | Single-server authenticated PIR, DDH (P-256) | 1 server | Yes | same | primitive `cmp-vpir-dh`; `database.CreateRandomEllipticWithDigest` |
| **M0** | Two-server DPF PIR (classic) | 2 servers, non-colluding | No | same | `client.NewPIR`, `server.NewPIRTwo`, `database.CreateRandomBytes`; see `pir_test.go::retrievePIRPoint` |
| **M1** | Two-server Merkle authenticated PIR | 2 servers | Yes | same | `database.CreateRandomMerkle`; see `merkle_test.go::retrieveBlocksMerkle` (verification happens inside `Reconstruct`; **confirm by reading `lib/client`**) |
| *opt.* DoublePIR | DoublePIR | 1 server | No | simplepir repo | Tier 3 |
| *opt.* M1-k | Merkle APIR with k = 3–5 servers | k servers | Yes | apir-code | Tier 3 |

**Primary comparisons (the "price" ratios):**
- **Within-family, single-server:** S1/S0-cpp and S2/S0-cpp. Same codebase and language, so implementation effects are controlled. S0-go versus S0-cpp is an *implementation-effect ablation*.
- **Within-family, two-server:** M1/M0. Same codebase.
- **Cross-family:** S1 and S2 versus S3 and S4 versus M1, all verifiable. This is the comparison no paper reports on common hardware.

## 5. Experimental grid

**Database size** (total bytes): 1 MiB, 16 MiB, 128 MiB, 1 GiB; optionally 4 GiB if RAM allows (see §7).
**Record size *d***: 32 B, 256 B, 1 KiB, 8 KiB. 1 KiB is the anchor; it matches Colombo et al.'s 1 KiB records.
Entries *N* = DB bytes / record bytes. Some arms take sizes in **bits** (apir-code's `DBBitLengths`; SimplePIR's `D` = bits per entry), so convert in exactly one place, in `harness/grid.py`.
Also run the paper-native configurations (SimplePIR 2^33 × 1 bit; apir 1 KiB / 1 MiB / 1 GiB with 8192 bits retrieved) as RQ1 anchors.

**Cells:** 4 sizes × 4 record sizes × 8 arms = 128, plus anchors. The harness must handle a scheme that rejects a parameter combination (e.g. VeriSimplePIR's `checkSISHardness()` assertion, or LWE parameter generation failing for `uint32_t`). Record it as `status="unsupported_params"` with the error message. **Never** change security parameters to force a cell to run.

**Repetitions:** online operations ≥ 30 (matches apir-code `simul.toml`); offline/preprocessing ≥ 5 for DB ≤ 128 MiB and ≥ 3 at 1 GiB+. Discard 1 warm-up run per cell and record it separately.

**Network profiles** (for modelled end-to-end latency only; nothing is sent over a real network in Tier 1):

| Profile | Down | Up | RTT |
|---|---|---|---|
| `lan` | 1 Gbps | 1 Gbps | 0.5 ms |
| `broadband` | 100 Mbps | 20 Mbps | 20 ms |
| `mobile` | 10 Mbps | 5 Mbps | 60 ms |

**Amortisation horizon** *Q* ∈ {1, 10, 100, 1 000, 10 000} queries per offline phase.

## 6. Metrics, with exact definitions

Every metric is recorded per repetition, per phase, per party. Times are wall-clock **and** process CPU time, single-threaded (§7).

| Metric | Unit | Definition / how to measure |
|---|---|---|
| `server_preproc_s` | s | Server time to build hint/digest/Merkle tree from the DB. **Must use real preprocessing, not "fake" hints.** Both upstream benchmarks skip preprocessing with random hints for speed (SimplePIR `pir_test.go`; VeriSimplePIR `Fake*` functions). |
| `server_preproc_proof_s` | s | VeriSimplePIR only: `PreprocAnswer` + `PreprocProve` time |
| `client_offline_s` | s | Client offline work: e.g. `PreprocClientMessage`, `PreprocVerify`, `PreprocRecoverZ`, `VerifyPreprocZ` for S1/S2; 0 if none |
| `offline_down_bytes` / `offline_up_bytes` | B | Hint, digest or offline messages. Measured as `len(serialize(msg))` **and** the analytical formula (VeriSimplePIR `src/demo/scripts/params.cpp`; SimplePIR `go test -run=BW`); store both and flag disagreement > 1 % |
| `client_storage_bytes` | B | Persistent client state between queries (hint, Z matrix, digest, keys). Serialised size |
| `query_gen_s` | s | Client `Query` time |
| `up_bytes` / `down_bytes` | B | Online query and answer sizes, all servers summed and per server |
| `server_answer_s` | s | Server `Answer` time; for 2-server arms record each server separately and report the max (parallel) and the sum (total work) |
| `client_recover_s` | s | Client reconstruction *excluding* verification, where the code separates them |
| `client_verify_s` | s | Client verification (`PreVerify`, Merkle-proof check, integrity check). If verification is fused with reconstruction, put the total in `client_recover_s`, set this to `null` and set `verify_fused=true` |
| `server_peak_rss_bytes`, `client_peak_rss_bytes` | B | Peak resident memory per party process (`/usr/bin/time -v` or `getrusage`) |
| `throughput_GBps` | GB/s | DB bytes / `server_answer_s` |
| `correct` | bool | Recovered record equals ground truth (always checked) |
| `detected` | bool/null | For fault-injection runs: did the client reject? |

**Derived quantities** (computed in `analysis/`, never in adapters):
- `overhead[metric] = median(verifiable) / median(baseline)` for the pairings in §4, with a **paired bootstrap 95 % CI** (10 000 resamples over repetitions).
- `latency_e2e(profile) = query_gen + up_bytes/up_bw + RTT·rounds + server_answer(max) + down_bytes/down_bw + client_recover + client_verify`. Take `rounds` from each scheme's protocol: 1 for all point queries here unless the code shows otherwise. **[HUMAN] confirm.**
- `amortised_bytes(Q) = (offline_down + offline_up)/Q + up + down`; `amortised_client_s(Q)`, `amortised_server_s(Q)` analogously.
- `break_even_Q(A, B)`: smallest Q at which arm A's amortised cost is ≤ arm B's.
- Optional `$ per 1k queries` from `configs/prices.yaml`: CPU-hour and egress-GB prices **entered by the students with a date and source URL**. The agent must not invent prices.

## 7. Testbed and environment

**Critical constraint, read first.** The team's laptop is an **Apple-silicon (arm64) Mac with 16 GB RAM**. The upstream code is x86-oriented:
- `apir-code/lib/matrix/matrix.go` compiles C with `-march=native -msse4.1 -maes -mavx2 -mavx`.
- The VeriSimplePIR `Makefile` adds `-maes -msse4`, links `-lrt`, and points the Darwin branch at an Intel-Homebrew LLVM path (`/usr/local/opt/llvm`).
- SimplePIR uses `-march=native`; its README says the flag can be removed at some cost.

Therefore:
1. **All reported timings come from one dedicated x86-64 Linux machine**: Ubuntu 22.04, ≥ 32 GB RAM (VeriSimplePIR was tested with 32 GB; 64 GB is needed for the 4 GiB cells), AVX2 + AES-NI. A cloud compute-optimised instance is fine. **[HUMAN]** Choose the machine, record its exact model, CPU, RAM, kernel and microcode in `env/hardware.md`, and use only that machine for every reported number.
2. The Mac is for development only, building and running correctness tests inside an `linux/amd64` Docker container. **Timings from emulated containers must never be reported.** The harness writes `platform` into every record, and `analysis/` refuses rows whose `platform` differs from `env/reference_platform.txt`.
3. Measurement hygiene on the reference machine: single-threaded (`GOMAXPROCS=1`, pin with `taskset -c 2`), CPU governor `performance`, turbo disabled if permitted (record whether it was), no other workloads, run each cell in a fresh process, and drop page cache between offline runs where possible.
4. Pinned toolchain in `docker/Dockerfile`: Go 1.19.x (both Go repos were tested with Go 1.17–1.19), clang++ (VeriSimplePIR tested with clang 10) with `libssl-dev`, Python 3.11 for harness and analysis.
5. Pin each upstream repo to an **exact commit SHA** via git submodules. Record the SHAs in `third_party/PINS.md` at clone time; the spec could not pre-fill them. Last upstream activity: simplepir 2023-01, verisimplepir 2023-12, apir-code 2024-11.
6. **Licence:** apir-code is GPL-3.0. Adapters that import it as a library are derived works, so release the project repo under GPL-3.0 (or keep the apir adapter in a separate GPL directory). **[HUMAN]** Confirm with the instructor.

## 8. Repository layout to build

```
vpir-bench/
├── Makefile                  # make setup | build | test | smoke | bench | analyse | figures | all
├── README.md                 # how to reproduce every figure with one command
├── docker/Dockerfile         # x86-64 Ubuntu 22.04, pinned Go/clang/Python
├── env/                      # hardware.md, reference_platform.txt, sysinfo dump script
├── third_party/              # git submodules: simplepir/, verisimplepir/, apir-code/ + PINS.md
├── patches/                  # *.patch — build-only fixes (flags, paths). Each with a README line explaining why
├── adapters/
│   ├── simplepir_go/         # Go CLI wrapping github.com/ahenzinger/simplepir/pir            → arm S0-go
│   ├── apir_go/              # Go CLI wrapping github.com/si-co/vpir-code/lib/{client,server,database} → S3,S4,M0,M1
│   └── vsp_cpp/              # C++ CLI linking libverisimplepir                                  → S0-cpp,S1,S2
├── harness/
│   ├── grid.py               # expands configs/grid.yaml into cells; single place for bit/byte conversion
│   ├── run_grid.py           # runs cells, resumable, one fresh process per (cell, phase), timeouts
│   ├── schema.json           # JSON schema for result records (§9)
│   └── validate.py           # schema + sanity checks (§11)
├── configs/
│   ├── grid.yaml             # sizes, record sizes, arms, reps (Tier 1 / 2 / 3 presets)
│   ├── network_profiles.yaml
│   └── prices.yaml           # filled in by humans, with source + date
├── results/
│   ├── raw/<run_id>/*.jsonl  # append-only, never edited
│   └── processed/*.parquet
├── analysis/
│   ├── aggregate.py          # medians, p95, bootstrap CIs, overhead ratios
│   ├── amortise.py           # amortised costs, break-even Q, network-profile latency
│   ├── reproduce.py          # RQ1 table: ours vs published, with deviation
│   └── plots.py              # all figures in §12
├── tests/                    # correctness + fault-injection + harness unit tests
└── docs/                     # reproduction_targets.md, threats_to_validity.md, report figures
```

## 9. Adapter contract (the core interface)

Each adapter is a standalone CLI. They must all behave identically so the harness is scheme-agnostic.

```
<adapter> --arm {S0-go|S0-cpp|S1|S2|S3|S4|M0|M1}
          --db-bytes <int> --record-bytes <int>
          --phase {offline|online|all}
          --reps <int> --warmup <int> --seed <int>
          --fault {none|flip_answer_bit|flip_hint_bit|wrong_record|drop_server}
          --out <path.jsonl>
```

- **Data:** a deterministic pseudorandom DB generated from `--seed` (ChaCha/AES-CTR keyed by seed). Query indices are drawn uniformly from the same seeded PRNG. The ground-truth record is computed by the adapter for the `correct` check. DB generation time is excluded from every metric.
- **Phase boundaries:** timers wrap *only* the scheme call (e.g. `Answer`), not serialisation, unless the field name says otherwise. Serialise every message to bytes and measure `len` — this is the measured communication.
- **Two-server arms:** the Tier 1 run is in-process, with both servers as separate objects in one process, timed separately. Tier 2 optionally runs each server as a separate OS process over loopback gRPC, using apir-code's `lib/proto` and `cmd/grpc`, to validate the in-process numbers.
- **Fault injection** happens **only in the adapter**, by mutating the serialised server message before the client sees it: flip one random bit, substitute another record's answer, or omit one server's answer. Never edit library code to inject faults.
- **Output:** one JSON object per (repetition, phase), one per line:

```json
{
  "run_id": "2026-11-02T10-15-00Z_ab12cd",
  "git_sha_harness": "…", "upstream_sha": "…",
  "platform": "x86_64-linux-<cpu-model>", "arm": "S1", "scheme_params": {"n": 1024, "logq": 64, "p": 991, "ell": 0, "m": 0, "honest_hint": true},
  "db_bytes": 1073741824, "record_bytes": 1024, "n_records": 1048576,
  "phase": "online", "rep": 7, "is_warmup": false, "seed": 42, "fault": "none",
  "status": "ok",
  "metrics": {"query_gen_s": 0.0, "up_bytes": 0, "server_answer_s": [0.0], "down_bytes": 0,
              "client_recover_s": 0.0, "client_verify_s": 0.0, "verify_fused": false,
              "client_storage_bytes": 0, "server_peak_rss_bytes": 0, "client_peak_rss_bytes": 0},
  "analytical": {"up_bytes": 0, "down_bytes": 0, "offline_down_bytes": 0},
  "correct": true, "detected": null, "error": null
}
```

`status` ∈ `ok | unsupported_params | oom | timeout | crash`. Failed cells are results and must be written, never silently skipped. `scheme_params` records the LWE/lattice parameters each library actually chose, so security levels can be compared in the report.

## 10. Rules for the coding agent (hard constraints)

1. **No cryptography edits.** Do not change parameters, moduli, statistical-security constants (`STAT_SEC_PARAM`, `Elem`, `BASIS`), protocol logic or verification checks in `third_party/`. Allowed changes are build fixes in `patches/`: compiler flags, include/link paths, removing `-lrt` on macOS, updating deprecated Go APIs. Each patch needs a one-line justification. If a change might affect performance (e.g. removing `-march=native`), it must apply to the **dev container only**, never to the reference machine.
2. **No fake preprocessing in reported numbers.** Upstream `Fake*` / random-hint helpers may be used only in an explicitly labelled `--fast-dev` mode, which writes `"fake_preproc": true`; analysis rejects those rows.
3. **Correctness before speed.** Every arm must pass the correctness and fault-injection tests (§11) at 1 MiB before any timing run.
4. **Do not invent numbers.** Prices, reproduction targets, hardware details and round counts come from humans or the code. If unknown, leave `null` and add a `TODO[HUMAN]`.
5. **Stop at the gates** in §12 and report status instead of pushing on.
6. When an upstream function's semantics are unclear (e.g. whether `Reconstruct` verifies), read the source and **quote the relevant lines in the PR description**. Do not assume.

## 11. Tests and acceptance criteria

| Test | Pass condition |
|---|---|
| `test_correctness[arm]` | 1 MiB DB, record sizes {32 B, 1 KiB}, 100 random queries: all `correct=true` |
| `test_fault_detection[verifiable arm, fault]` | For S1–S4 and M1, each fault in {`flip_answer_bit`, `wrong_record`} is detected (`detected=true`) in 100/100 trials, or the test documents the scheme's stated soundness error and the observed rate is consistent with it. For `flip_hint_bit` on S1/S2, offline verification rejects. |
| `test_fault_baseline[S0,M0]` | The same faults produce `correct=false` without detection; this demonstrates the threat |
| `test_comm_analytical` | Measured bytes = analytical bytes within 1 % for every cell |
| `test_schema` | Every raw record validates against `schema.json` |
| `test_resume` | Killing `run_grid.py` mid-run and restarting completes only the missing cells |
| `smoke` | `make smoke` runs every arm at 1 MiB × 1 KiB × 3 reps in < 10 min and produces all figures from those rows |
| RQ1 sanity | SimplePIR 1 GB anchor: hint ≈ 121 MB and online ≈ 242 KB (deterministic), else investigate before proceeding |

## 12. Phases, gates and figures

The course milestones in the repo (`projects/README.md`) list the **final deadline as 20 Nov**. If that applies to this run, there are about 3.5 weeks left, so build **Tier 1 only** first and treat Tiers 2–3 as stretch. **[HUMAN]** Confirm the actual deadline.

| Phase | Work | Gate (stop and report) | Tier |
|---|---|---|---|
| P0 Build (days 1–3) | Dockerfile; submodules pinned; all three upstream test suites pass (`go test` in simplepir/pir ≈ 3 min; apir-code `go test` ≈ 6 min; `./bin/demo/test/pir_test` in VeriSimplePIR) on the x86-64 machine | **G0: all three build and pass upstream tests.** If one fails after 1 day of build-fixing, escalate to humans with logs | 1 |
| P1 Adapters (days 3–8) | Three adapters; correctness + fault tests; schema | **G1: §11 correctness and fault tests green for all 8 arms** | 1 |
| P2 Harness (days 6–9) | `run_grid.py`, resumable, timeouts, RSS capture, `make smoke` | **G2: smoke run + figures from smoke data** | 1 |
| P3 RQ1 anchors (days 9–11) | Paper-native configurations; `reproduce.py` table | **G3: reproduction table reviewed by humans** | 1 |
| P4 Full grid (days 10–16) | All cells ≤ 1 GiB; 4 GiB if RAM allows | Grid complete or failures recorded with status | 1 |
| P5 Analysis (days 14–18) | Overhead ratios + CIs; amortisation; network profiles; break-even Q | All figures regenerate from `make figures` | 1–2 |
| P6 Extensions | RQ4 misbehaviour simulation (policy table from **[HUMAN]** reading of each paper's security definition: abort / switch server / re-preprocess); RQ5 rebuild cost; loopback multi-process for M0/M1 | — | 3 |

**Figures** (`analysis/plots.py`; each saved as PDF + PNG, regenerated from processed data only):
1. **F1 — Reproduction:** published versus measured overhead ratio per claim (dot plot, CIs).
2. **F2 — Overhead heatmaps:** for S1/S0-cpp, S2/S0-cpp and M1/M0, rows = DB size, cols = record size, colour = overhead, one panel per metric (online bytes, server time, client time incl. verification, client storage).
3. **F3 — Absolute cost vs DB size:** log–log lines per arm at 1 KiB records, for server answer time, online bytes and client storage.
4. **F4 — Amortised cost vs Q:** total bytes and client time per query versus Q, one line per verifiable arm; break-even points marked.
5. **F5 — Network profiles:** end-to-end modelled latency per arm at 1 GiB × 1 KiB for the three profiles (grouped bars, p50 with p95 whiskers).
6. **F6 — Cost breakdown:** stacked bars of where verification cost goes (offline proof, client offline verify, online verify, extra bytes).
7. **F7 (opt.) — Implementation effect:** S0-go versus S0-cpp on identical cells.

Tables: T1 machine + toolchain + SHAs; T2 scheme parameters and security level per arm; T3 reproduction; T4 overhead summary with CIs.

## 13. Threats to validity (the report must address these; agent creates `docs/threats_to_validity.md` stub)

- **Implementation, not scheme:** ratios across codebases (Go versus C++) mix scheme cost with engineering quality. Mitigated by within-codebase pairs (S1/S0-cpp, M1/M0) and the S0-go/S0-cpp ablation.
- **Different security levels and parameters** across libraries: report `scheme_params`; do not claim equal security unless the papers state it.
- **Fake versus real preprocessing:** enforced by rule 2.
- **Single-threaded, single machine:** results do not show multi-core scaling. Two-server timing is in-process, and network effects are modelled, not measured.
- **Trust assumptions differ:** two-server schemes assume non-colluding servers, single-server schemes do not. A cost comparison is not a security equivalence, and the report must say so whenever F4/F5 put families side by side.
- **Stale code:** upstream repos are 2023–2024 research prototypes, with warnings to that effect in their READMEs.

## 14. Definition of done

- `make all` on the reference machine rebuilds everything, runs the Tier 1 grid, and regenerates F1–F6 and T1–T4 with no manual steps.
- Every number in the report traces back to a raw JSONL record (run_id + rep).
- The report states at least one measured, scoped claim per RQ attempted, including negative or "could not run" results.

---

## Appendix A — Prompt to paste in front of this spec

> You are implementing the benchmark harness described in the attached spec (`vpir_bench_spec.md`). Work phase by phase (P0 → P5) and **stop at every gate G0–G3** to report what passed, what failed, and the exact logs. Obey §10 strictly: no edits to cryptographic code or parameters, no fake preprocessing in reported numbers, no invented values. Anything marked [HUMAN] is a question for me, not a decision for you. Before writing an adapter, read the upstream source for that scheme and summarise the exact call sequence and where verification happens, quoting file paths and line ranges. Start with P0: write the Dockerfile and submodule pins, build all three upstream repos, and run their test suites.

## Appendix B — Upstream entry points found in source (for the agent's orientation; verify against pinned SHAs)

- **SimplePIR** (`pir/`): `pir.go` (interface; `RunPIR`, `RunPIRCompressed`, `RunFakePIR`), `simple_pir.go`, `double_pir.go`, `database.go`, `params.csv` (LWE params). Benchmarks via env vars `LOG_N`, `D` (`go test -bench PirSingle`), and analytical communication via `go test -run=BW`. cgo flag `-O3 -march=native`.
- **VeriSimplePIR** (`src/lib/pir/preproc_pir.h`): class `VeriSimplePIR(N, d, allowTrivial, verbose, simplePIR, randomData, preproc, honestHint)`; online `Init`, `GenerateHint`, `Query`, `Answer`, `PreVerify`, `Recover`; offline `PreprocInit`, `PreprocGenerateHint`, `PreprocSampleC`, `PreprocClientMessage`, `PreprocAnswer`, `PreprocProve`, `PreprocVerify`, `PreprocRecoverZ`, `VerifyPreprocZ`; `Fake*` variants exist (dev only). Benches in `src/demo/bench/` (`simplepir_bench`, `preproc_pir_bench`, `pir_bench`, `batch_pir_bench`, `verify_bench`, `proof_bench`). Analytical comms in `src/demo/scripts/params.cpp`. Needs `clang++`, `libssl-dev`, `LD_LIBRARY_PATH=…/bin/lib`.
- **apir-code** (module `github.com/si-co/vpir-code`): `lib/client` (`NewPIR`, `NewLWE`, `QueryBytes`, `ReconstructBytes`), `lib/server` (`NewPIRTwo`, `NewPIR`, `AnswerBytes`), `lib/database` (`CreateRandomBytes`, `CreateRandomMerkle`, `CreateRandomBinaryLWEWithLength`, `CreateRandomEllipticWithDigest`), `lib/merkle`, `lib/fss`, `lib/monitor` (CPU timers). Simulation driver `simulations/simul.go` with TOML configs (`DBBitLengths` in **bits**, `Repetitions = 30`, `BitsToRetrieve = 8192`). Multi-server configs in `simulations/multi/` (`pir-classic`, `pir-merkle`, `NumServers = [2,4,6,8,10]`, `BlockLength = 1024`). Authors' raw results are in `simulations/final_results/*.json`, a useful extra RQ1 cross-check. cgo flags `-march=native -msse4.1 -maes -mavx2 -mavx`.
