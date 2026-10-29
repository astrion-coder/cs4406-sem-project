# Implementation Spec — *The Price of Verifiability in PIR: an independent, common-hardware re-evaluation*

**Project:** CS4406 semester project · **Spec version:** 1.2 (2026-10-29). v1.2 corrects M0 (classic XOR two-server PIR, not DPF) and the Colombo et al. targets after checking the paper. v1.1 added query-volume, network, concurrency, multicore and measured-recovery experiments (RQ3a/3b, RQ4, RQ6, RQ7) and the prior-coverage table (§2.1).
**Audience:** a coding LLM (or engineer) building the benchmark harness, plus the student team who own the scientific decisions.

> **How to use this document.** Sections 1–3 give the scientific context; the coding agent should read them but not change them. Sections 4–11 are the build contract. Section 13 is a prompt you can paste in front of this spec. Anything marked **[HUMAN]** is a decision or check the students must make; the coding agent must stop and ask instead of guessing.

---

## 1. Goal in one paragraph

Measure what verifiability costs in Private Information Retrieval (PIR), meaning protection against a server that returns wrong answers, by running **verifiable PIR schemes and their non-verifiable baselines side by side on the same hardware, the same database grid and the same measurement harness**. Existing overhead numbers are self-reported by each scheme's authors, each against their own baseline, on their own hardware and parameters. This project (a) reproduces those published overhead ratios, (b) extends the comparison to regimes the original papers stress less: small records, offline/setup cost, client storage, and amortised cost per query, and (c) compares *across* families, single-server lattice versus two-server Merkle (on classic XOR PIR). The deliverable is a one-command benchmark plus a report whose central claim is a measured statement such as *"for 1 GiB databases with 32-byte records, single-server verifiability costs X× in total bytes per query amortised over 100 queries, versus Y× for two-server authenticated PIR."*

## 2. Research questions and hypotheses

| ID | Question | Falsifiable hypothesis | Tier |
|---|---|---|---|
| **RQ1** Reproduction | Do the published overhead ratios hold on common hardware? | H1: online communication ratios match the published analytical values within ±5 % (communication is deterministic given parameters); compute ratios fall within the published ranges, up to a hardware factor. | 1 (must) |
| **RQ2** Grid | How does the verifiable/baseline overhead for each metric vary with database size *N·d* and record size *d*? | H2: relative overhead is largest for small records and small databases, where fixed verification costs dominate, and shrinks with database size. | 1 (must) |
| **RQ3a** Query volume | Is the setup (offline) cost worth paying for 1, 10, 100, 1 000 (and 10 000) queries? Including offline cost and client storage, which scheme has the lowest total cost per query, and where does the ranking flip? | H3a: single-server verifiable PIR beats two-server authenticated PIR on amortised bytes only beyond some break-even *Q*; below it, two-server wins. | 2 (should) |
| **RQ3b** Network | Does the extra communication of verifiable schemes noticeably increase response time on slow networks? | H3b: on the `mobile` profile, end-to-end latency overhead approaches the byte overhead; on `lan` it approaches the compute overhead. | 2 (should) |
| **RQ4** Failure & recovery (measured) | Are corrupted responses detected, and how much extra time, communication and repeated preprocessing does it take to recover a correct answer after a rejection? How does expected cost change when a server cheats with probability *f*? | H4: recovery is cheap (≈ one extra query) for schemes that recover by retrying or switching server, and expensive (≈ a full offline phase) for schemes whose recovery requires re-running preprocessing. | 2 (should) |
| **RQ5** Updates | What is the cost to re-establish verifiable state after *u* record updates, assuming the implementations' full rebuild? | H5: rebuild cost per update makes preprocessing-based verifiable schemes the most expensive at high update rates. | 3 (stretch; derived) |
| **RQ6** Concurrent clients | How do server throughput (queries/s) and response-time percentiles change as the number of concurrent clients grows, and does the verifiable/baseline gap widen or narrow under load? | H6: the server is memory-bandwidth-bound, so throughput saturates at a core count below the number of physical cores, and the verifiable/baseline throughput ratio stays close to the single-query server-time ratio. | 2–3 |
| **RQ7** Hardware / multicore | Does the relative overhead change on a multicore server (1→N cores) and, optionally, on a second CPU type? | H7: the relative overhead is stable within ±20 % across core counts for schemes whose verification cost is client-side; for schemes with extra server-side work it changes with core count. | 2–3 |

A negative result for any hypothesis is a valid result. The harness must not be tuned to confirm a hypothesis.

### 2.1 Prior coverage vs. this project's contribution

This table is based on the authors' code, configs and READMEs and on earlier targeted searches, not on a full reading of all three papers. **[HUMAN]** Confirm each "partly" row against the PDFs before writing the novelty claim.

| Aspect | What the authors did | Status here |
|---|---|---|
| SimplePIR baseline numbers | Reported for the paper's 1 GB headline configuration | Reproduce (RQ1) |
| VeriSimplePIR vs SimplePIR (incl. offline cost, client storage) | Reported by the VeriSimplePIR authors | Reproduce (RQ1) |
| Two-server classic (XOR) PIR vs Merkle APIR, 1 KiB records, DB up to 10 GiB; 2–5 servers at 1 GiB; single-server LWE/LWE+/DDH vs SimplePIR (1-bit retrieval, 1 KiB–1 GiB DBs) | Reported by Colombo et al. (§7.1, §7.3, Figs 3, 4, 6; 30 runs, median, one core per machine) | Reproduce (RQ1) |
| All verifiable families on one machine and one harness | Each paper compares against its own baseline. Cross-paper comparisons, if any, may reuse other papers' numbers | **New** (RQ2/RQ3a) |
| Record-size sweep; small databases for the lattice schemes | Each paper fixes its own record size | **New** (RQ2) |
| Query volume (1–1 000 queries per setup) | Papers report offline and online cost separately | **Partly new**: side-by-side amortised cost and break-even *Q* across families (RQ3a) |
| Network conditions | Colombo et al. ran multi-server experiments on separate machines over **10 GbE only**; single-server schemes ran client and server on one machine. apir-code ships `simulations/real/` scripts | **Partly new**: the same three network profiles for all schemes, modelled and validated with emulated links (RQ3b) |
| Many concurrent clients | Not found in any of the three codebases' benchmarks; the published metrics are single-query latency and per-core throughput | **New** (RQ6) |
| Multicore server | Colombo et al. ran all experiments on **one core per machine** and only *estimate* (not measure) that a multithreaded Merkle-APIR deployment needs 12 cores vs 11 for unauthenticated PIR (§7.4). apir-code servers nonetheless take a `cores` argument defaulting to `runtime.NumCPU()`; the SimplePIR and VeriSimplePIR library files scanned contain no threading | **Partly new**: overhead ratio vs core count under identical conditions (RQ7) |
| Detection of corrupted responses | Security proofs; upstream tests check correctness of honest runs | **New as measurement** (fault-injection tests, RQ4) |
| Cost of recovery after a rejection (retry / switch / re-preprocess) | Not measured | **New** (RQ4) |
| Update cost across schemes | Not in these three; 2025–26 preprints (TAPIR, LWE-AuthIncPIR) start to study it | **New for cross-scheme comparison** (RQ5) |


## 3. Published reference values (reproduction targets for RQ1)

These come from the papers and READMEs surveyed earlier. **[HUMAN]** Re-check each against the PDF and record the table or figure number in `docs/reproduction_targets.md` before comparing.

| Source | Claim | What we compare |
|---|---|---|
| SimplePIR README (Henzinger et al., USENIX Sec '23) | 1 GB DB of 2^33 1-bit entries: ~121 MB hint, ~242 KB online communication, ~10 GB/s/core server throughput (AWS c5n.metal) | Hint and online bytes exactly; throughput within a hardware factor |
| SimplePIR README | DoublePIR, same DB: 16 MB hint, 345 KB online, 7.4 GB/s/core | Same (optional arm) |
| VeriSimplePIR (de Castro & Lee, USENIX Sec '24) | Online communication ≈ 1.1–1.5× SimplePIR; online server compute ≈ SimplePIR | Ratio |
| VeriSimplePIR | Without the honest-digest assumption: 12–40 % slower compute, 40–50 % more total communication | Ratio |
| VeriSimplePIR | ~800 MB persistent client storage at their headline size | Client-storage bytes |
| Authenticated PIR (Colombo et al., USENIX Sec '23, §7.1, Fig 3), two servers, 1 KiB records, DB up to 10 GiB | Maximum measured overhead **2.9× user time, 1.8× bandwidth** versus classic unauthenticated two-server PIR; overheads grow with DB size (O(λ log N) Merkle proof per record). The introduction rounds this to "less than 2.7× computational" | Ratio across DB sizes; trend with N |
| Colombo et al. §7.1, Fig 4 | Same comparison for 2–5 servers at 1 GiB | Ratio vs server count (Tier 3, M1-k) |
| Colombo et al. §7.3, Fig 6, single server, 1-bit retrieval, DB 1 KiB–1 GiB | SimplePIR (default 2048-bit records) is **30–100× faster than LWE+** (integrity-amplified LWE, t = 7, 15 repetitions); LWE+ beats LWE at 1 KiB/1 MiB but is 1.4× slower at 1 GiB; **DDH takes over an hour at 1 GiB** | Ratios; LWE/LWE+ crossover |
| Colombo et al. §7.4 (Keyd, 3.56 M PGP keys, ≈ 3 GiB) | Authenticated lookup 1.11 s vs 1.10 s unauthenticated (client-perceived, two servers) | Optional sanity point if a comparable DB is built |

The setup Colombo et al. used for all of these: 2× Intel Xeon E5-2680 v3 (Haswell, 2.5 GHz), 256 GB RAM, Ubuntu 20.04, Go 1.17.5, 10 GbE. Client and servers ran on separate machines for multi-server schemes and on one machine for single-server schemes. One core per machine, 30 runs, median reported. Our ratios should match within CIs; absolute times will differ by hardware.

## 4. Arms (schemes under test)

Use **only the authors' reference implementations**. The coding agent must **not** implement or modify any cryptography; only build fixes, adapters and instrumentation are allowed (see §10).

| Arm | Scheme | Setting | Verifiable? | Source repo (lang) | Entry point to wrap |
|---|---|---|---|---|---|
| **S0-go** | SimplePIR | 1 server, LWE | No | `github.com/ahenzinger/simplepir` (Go + C, MIT) | package `pir`: `RunPIR` / `RunPIRCompressed` in `pir/pir.go` show the full call sequence |
| **S0-cpp** | SimplePIR (as re-implemented in the VeriSimplePIR codebase) | 1 server, LWE | No | `github.com/leodec/verisimplepir` (C++, MIT) | `VeriSimplePIR(N, d, …, simplePIR=true, preproc=false)`; see `src/demo/bench/simplepir_bench.cpp` |
| **S1** | VeriSimplePIR, honest-digest assumption | 1 server, LWE | Yes | same | `VeriSimplePIR(N, d, …, preproc=true, honestHint=true)`; `src/demo/bench/preproc_pir_bench.cpp` |
| **S2** | VeriSimplePIR, full (no honest-digest assumption) | 1 server, LWE | Yes | same | `preproc=true, honestHint=false` |
| **S3** | Single-server authenticated PIR, LWE (paper's "LWE"; `cmp-vpir-lwe-128` may correspond to the 128-bit-modulus variant. **[HUMAN]** confirm which primitive implements integrity-amplified "LWE+" before running) | 1 server | Yes | `github.com/dedis/apir-code` (Go, GPL-3.0; module path `github.com/si-co/vpir-code`) | primitive `cmp-vpir-lwe` (and `cmp-vpir-lwe-128`) in `simulations/simul.go`; `client.NewLWE`, `database.CreateRandomBinaryLWEWithLength` |
| **S4** | Single-server authenticated PIR, DDH (P-256). Paper: > 1 h per query at 1 GiB, so run S4 only on DB ≤ 128 MiB, and record larger cells as `status=timeout` after a 30 min cap | 1 server | Yes | same | primitive `cmp-vpir-dh`; `database.CreateRandomEllipticWithDigest` |
| **M0** | Two-server classic PIR (Chor-style XOR over the binary field, `fastxor`; the same scheme Merkle APIR is built on, §6 of the paper) | 2 servers, non-colluding | No | same | `client.NewPIR`, `server.NewPIRTwo`, `database.CreateRandomBytes`; see `pir_test.go::retrievePIRPoint` |
| **M1** | Two-server Merkle authenticated PIR | 2 servers | Yes | same | `database.CreateRandomMerkle`; see `merkle_test.go::retrieveBlocksMerkle` (verification happens inside `Reconstruct`; **confirm by reading `lib/client`**) |
| *opt.* DoublePIR | DoublePIR | 1 server | No | simplepir repo | Tier 3 |
| *opt.* M1-k | Merkle APIR with k = 3–5 servers | k servers | Yes | apir-code | Tier 3 |

**No arm requires new cryptographic code.** A DPF-based two-server baseline (e.g. `google/distributed_point_functions`) is deliberately out of scope: no published authenticated DPF point-query scheme exists to pair it with, and Colombo et al. use DPF/FSS only for predicate queries.

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

**Amortisation horizon** *Q* ∈ {1, 10, 100, 1 000, 10 000} queries per offline phase (RQ3a). This is computed from the measured offline and online components. **Validate it** by running one end-to-end session of *Q* = 100 consecutive queries per arm at the focus cells and comparing measured against computed totals; disagreement > 5 % must be explained.

### 5.1 Extension sub-grids (RQ3b, RQ4, RQ6, RQ7)

The extensions run only on **focus cells**, to keep runtime bounded: DB ∈ {128 MiB, 1 GiB} × record ∈ {32 B, 1 KiB}, all 8 arms where they run. **[HUMAN]** May shrink this to 1 GiB × 1 KiB if time is short.

| Experiment | Axis | Values | Reps |
|---|---|---|---|
| RQ3b network (measured) | Emulated link via Linux `tc netem` on a veth pair / network namespace between client and server processes | the three profiles in the table above | 30 per profile |
| RQ6 concurrency, closed loop | concurrent clients *C* | 1, 2, 4, 8, 16, 32, 64 (stop when p95 latency > 10× the *C*=1 value) | 60 s steady state after 10 s warm-up, 3 runs |
| RQ6 concurrency, open loop | Poisson arrival rate λ | 10 %, 25 %, 50 %, 75 %, 90 %, 100 %, 110 % of the saturation throughput measured in closed loop | 60 s, 3 runs |
| RQ7 multicore | server worker threads/cores *W* | 1, 2, 4, 8, 16 (up to the physical cores of the reference machine) | 30 |
| RQ7 second hardware (opt.) | CPU type | a second x86-64 generation/vendor; arm64 only if builds succeed *without* changing optimisation flags beyond those documented in `patches/` | 30 |
| RQ4 faults | fault type × recovery policy | faults: `flip_answer_bit`, `wrong_record`, `stale_record`, `flip_hint_bit` (offline), `bad_proof` (offline, S2), `drop_server`; policies in §6.1 | 100 trials per (arm, fault, policy) |
| RQ4 cheating rate | per-query cheat probability *f* | 0, 0.001, 0.01, 0.1, 0.5, 1.0 | 1 000 queries per value, seeded |

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
| `throughput_qps` | queries/s | RQ6/RQ7: completed correct queries per second during the steady-state window |
| `lat_p50_s`, `lat_p95_s`, `lat_p99_s` | s | RQ6/RQ7/RQ3b: end-to-end client-observed latency percentiles (query generation → verified record) |
| `server_cpu_util`, `server_mem_bw_GBps` | %, GB/s | RQ6/RQ7: from `/proc` or `perf stat`; memory bandwidth is optional (needs uncore counters) |
| `offered_load_qps` | queries/s | open-loop runs only |
| `time_to_correct_s` | s | RQ4: wall time from the first query attempt to a verified correct record, or to abort |
| `recovery_extra_bytes`, `recovery_extra_rounds` | B, count | RQ4: bytes and round trips beyond a fault-free query |
| `re_preproc_s`, `re_preproc_bytes` | s, B | RQ4: cost of any repeated offline phase triggered by recovery |
| `attempts`, `outcome` | count, enum | RQ4: `outcome` ∈ `recovered | aborted | silent_wrong` |
| `false_reject` | bool | RQ4 control: an honest response rejected (should never happen) |

### 6.1 Recovery policies (RQ4)

| Policy | Action after rejection | Applies to |
|---|---|---|
| `abort` | Stop and report failure | all verifiable arms |
| `retry_same(k)` | Re-issue a fresh query to the same server(s), up to *k* = 3 times | arms whose security definition permits it (**[HUMAN]**: check selective-failure implications in each paper before enabling) |
| `switch_replica` | Re-issue to an honest replica (single-server) or a different server pair (two-server) | all verifiable arms; the replica is modelled as honest |
| `re_preprocess` | Discard client state, re-run the full offline phase (hint download + verification), then re-query | S1, S2 (and any arm whose offline state can be corrupted) |

Expected cost under cheating rate *f* (RQ4) is reported **both** measured (the *f* sweep above) and computed from the measured component costs, and the two must agree within CIs.

**Derived quantities** (computed in `analysis/`, never in adapters). Also: `overhead_under_load(C) = throughput_baseline(C)/throughput_verifiable(C)`, `overhead_vs_cores(W)` analogously, `latency_overhead(profile)`, and `recovery_cost_ratio = time_to_correct / fault-free latency`.
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
1. **All reported timings come from one dedicated x86-64 Linux machine**: Ubuntu 22.04, ≥ 16 physical cores (for RQ6/RQ7), SMT state recorded, ≥ 32 GB RAM (VeriSimplePIR was tested with 32 GB; 64 GB is needed for the 4 GiB cells), AVX2 + AES-NI. A cloud compute-optimised instance is fine. **[HUMAN]** Choose the machine, record its exact model, CPU, RAM, kernel and microcode in `env/hardware.md`, and use only that machine for every reported number.
2. The Mac is for development only, building and running correctness tests inside an `linux/amd64` Docker container. **Timings from emulated containers must never be reported.** The harness writes `platform` into every record, and `analysis/` refuses rows whose `platform` differs from `env/reference_platform.txt`.
3. Measurement hygiene on the reference machine: single-threaded for the main grid and RQ1–RQ5 (`GOMAXPROCS=1`, pin with `taskset -c 2`, and pass `cores=1` to apir-code servers, whose default is `runtime.NumCPU()`); RQ6/RQ7 vary threads explicitly and pin the load generator to cores disjoint from the server's (or to a second machine on the same switch, preferred); CPU governor `performance`, turbo disabled if permitted (record whether it was), no other workloads, run each cell in a fresh process, and drop page cache between offline runs where possible.
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
│   ├── loadgen/              # Go closed/open-loop load generator (RQ6); Python is too slow to drive saturation
│   ├── netem.sh              # creates netns + veth, applies tc netem profiles, verifies with ping/iperf3 (RQ3b)
│   ├── recovery.py           # drives fault × policy trials and the cheating-rate sweep (RQ4)
│   ├── schema.json           # JSON schema for result records (§9)
│   └── validate.py           # schema + sanity checks (§11)
├── configs/
│   ├── grid.yaml             # sizes, record sizes, arms, reps (Tier 1 / 2 / 3 presets)
│   ├── network_profiles.yaml
│   ├── prices.yaml           # filled in by humans, with source + date
│   ├── load.yaml             # concurrency levels, arrival rates, worker counts (RQ6/RQ7)
│   └── recovery_policies.yaml# which policies are enabled per arm, after [HUMAN] review (RQ4)
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
          --fault {none|flip_answer_bit|flip_hint_bit|wrong_record|stale_record|bad_proof|drop_server}
          --fault-prob <float>            # RQ4 cheating-rate sweep; default 1.0 when --fault != none
          --recovery {abort|retry_same:K|switch_replica|re_preprocess}
          --out <path.jsonl>

<adapter> --mode serve --arm <arm> --db-bytes … --record-bytes … --workers <W> --listen <host:port> [--server-id 0|1]
<adapter> --mode client --arm <arm> --connect <host:port>[,<host:port>] --session-queries <Q> --out <path.jsonl>
```

**Serve mode (RQ3b, RQ6, RQ7).** A thin length-prefixed TCP (or gRPC) wrapper around the library's `Answer` call. The DB is loaded **once** and shared read-only by *W* worker threads. Before using threads, the agent must establish whether each library's answer function is thread-safe (read the source and quote it). If it is not, use *W* worker processes over a shared `mmap` of the DB, or serialise and document it. Inter-query parallelism (different queries on different workers) is the default for every arm. Intra-query parallelism is used only where the library provides it natively (apir-code `cores`); record which mode each arm used in `scheme_params.parallelism`. For two-server arms, run two server processes on disjoint core sets; the client waits for both. apir-code already has gRPC servers in `cmd/grpc`, which may be reused.

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
6. **Serving and concurrency wrappers must not change answers.** Batching several clients' queries into one matrix–matrix product, or row-sharding a single answer, is allowed only if the library exposes it, or (Tier 3) if the wrapper's answers are bit-identical to the library's sequential answers in tests. Every such optimisation must apply equally to the verifiable arm and its baseline.
7. **Recovery policies** are enabled per arm only after the humans fill in `configs/recovery_policies.yaml`. Some retry policies may weaken selective-failure security, and the agent must not decide this.
8. When an upstream function's semantics are unclear (e.g. whether `Reconstruct` verifies), read the source and **quote the relevant lines in the PR description**. Do not assume.

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
| `test_concurrent_correctness[arm]` | Serve mode, *C* = 16 clients, 1 000 queries: every answer correct and byte-identical to the sequential bench-mode answer for the same query seed |
| `test_loadgen_not_bottleneck` | Load-generator CPU < 70 % at the highest *C* used; otherwise add load-generator cores or a second machine |
| `test_netem_calibration` | Measured RTT within ±10 % and bandwidth within ±10 % of each profile (ping, iperf3) before any RQ3b run |
| `test_recovery[arm, fault, policy]` | `recovered` within the policy's attempt budget when the replica is honest; `aborted` (never `silent_wrong`) when all servers cheat; 0 false rejects across 1 000 honest queries |
| RQ1 sanity | SimplePIR 1 GB anchor: hint ≈ 121 MB and online ≈ 242 KB (deterministic), else investigate before proceeding |

## 12. Phases, gates and figures

The course milestones in the repo (`projects/README.md`) list the **final deadline as 20 Nov**. If that applies to this run, there are about 3.5 weeks left. Build **Tier 1 first**, then add Tier 2 phases in the order P7 (recovery) → P6 (volume/network) → P8 (multicore) → P9 (concurrency); P8–P9 share the serve-mode wrapper. With a 2–3-person team, P6–P7 and P8–P9 can run in parallel after G3. **[HUMAN]** Confirm the actual deadline.

| Phase | Work | Gate (stop and report) | Tier |
|---|---|---|---|
| P0 Build (days 1–3) | Dockerfile; submodules pinned; all three upstream test suites pass (`go test` in simplepir/pir ≈ 3 min; apir-code `go test` ≈ 6 min; `./bin/demo/test/pir_test` in VeriSimplePIR) on the x86-64 machine | **G0: all three build and pass upstream tests.** If one fails after 1 day of build-fixing, escalate to humans with logs | 1 |
| P1 Adapters (days 3–8) | Three adapters; correctness + fault tests; schema | **G1: §11 correctness and fault tests green for all 8 arms** | 1 |
| P2 Harness (days 6–9) | `run_grid.py`, resumable, timeouts, RSS capture, `make smoke` | **G2: smoke run + figures from smoke data** | 1 |
| P3 RQ1 anchors (days 9–11) | Paper-native configurations; `reproduce.py` table | **G3: reproduction table reviewed by humans** | 1 |
| P4 Full grid (days 10–16) | All cells ≤ 1 GiB; 4 GiB if RAM allows | Grid complete or failures recorded with status | 1 |
| P5 Analysis (days 14–18) | Overhead ratios + CIs; amortisation; network profiles; break-even Q | All figures regenerate from `make figures` | 1–2 |
| P6 Query volume + network (≈ 2 days) | RQ3a session-validation runs; RQ3b netem runs at focus cells | **G4: netem calibration test green** | 2 |
| P7 Failure & recovery (≈ 3 days) | Fault × policy trials; cheating-rate sweep; measured vs computed expected cost | **G5: humans have approved `recovery_policies.yaml`** before any run | 2 |
| P8 Serve mode + multicore (≈ 3 days) | Serve-mode wrappers; thread-safety findings; RQ7 core sweep | **G6: concurrent-correctness test green** | 2 |
| P9 Concurrency (≈ 3 days) | Load generator; closed- and open-loop RQ6 runs | **G7: load generator verified not to be the bottleneck** | 2–3 |
| P10 Stretch | RQ5 rebuild cost; second-hardware RQ7 runs; intra-query sharding wrapper | — | 3 |

**Figures** (`analysis/plots.py`; each saved as PDF + PNG, regenerated from processed data only):
1. **F1 — Reproduction:** published versus measured overhead ratio per claim (dot plot, CIs).
2. **F2 — Overhead heatmaps:** for S1/S0-cpp, S2/S0-cpp and M1/M0, rows = DB size, cols = record size, colour = overhead, one panel per metric (online bytes, server time, client time incl. verification, client storage).
3. **F3 — Absolute cost vs DB size:** log–log lines per arm at 1 KiB records, for server answer time, online bytes and client storage.
4. **F4 — Amortised cost vs Q:** total bytes and client time per query versus Q, one line per verifiable arm; break-even points marked.
5. **F5 — Network profiles:** end-to-end modelled latency per arm at 1 GiB × 1 KiB for the three profiles (grouped bars, p50 with p95 whiskers).
6. **F6 — Cost breakdown:** stacked bars of where verification cost goes (offline proof, client offline verify, online verify, extra bytes).
7. **F7 (opt.) — Implementation effect:** S0-go versus S0-cpp on identical cells.
8. **F8 — Is setup worth it?** Measured vs computed total cost for *Q* ∈ {1, 10, 100, 1 000}, per arm; break-even *Q* annotated (complements F4).
9. **F9 — Network:** emulated vs modelled end-to-end latency per profile, and latency overhead vs byte overhead.
10. **F10 — Throughput–latency curves:** p50/p95 latency vs achieved throughput for each arm as *C* (closed loop) or λ (open loop) increases; saturation point marked.
11. **F11 — Overhead vs cores:** throughput and overhead ratio vs *W* for each verifiable/baseline pair, with ideal-scaling reference line.
12. **F12 — Recovery cost:** time-to-correct-answer and extra bytes by fault type × policy (grouped bars, CIs); detection-rate table alongside.
13. **F13 — Expected cost vs cheating rate *f*:** measured points over computed curve, per arm and policy.

Tables: T1 machine + toolchain + SHAs; T2 scheme parameters and security level per arm; T3 reproduction; T4 overhead summary with CIs.

## 13. Threats to validity (the report must address these; agent creates `docs/threats_to_validity.md` stub)

- **Implementation, not scheme:** ratios across codebases (Go versus C++) mix scheme cost with engineering quality. Mitigated by within-codebase pairs (S1/S0-cpp, M1/M0) and the S0-go/S0-cpp ablation.
- **Different security levels and parameters** across libraries: report `scheme_params`; do not claim equal security unless the papers state it.
- **Fake versus real preprocessing:** enforced by rule 2.
- **Single-threaded, single machine:** results do not show multi-core scaling. Two-server timing is in-process, and network effects are modelled, not measured.
- **Trust assumptions differ:** two-server schemes assume non-colluding servers, single-server schemes do not. A cost comparison is not a security equivalence, and the report must say so whenever F4/F5 put families side by side.
- **Emulated networks and loopback:** `netem` reproduces bandwidth and delay but not real-world jitter, loss patterns or TCP behaviour on long paths. Results are for the stated profiles only.
- **Serving wrappers are ours:** throughput depends on our thin server code (threading, serialisation, socket handling), not only on the schemes. Identical wrappers for every arm keep comparisons fair but do not reflect a production deployment.
- **Memory-bandwidth contention** dominates multicore and concurrency results for matrix–vector schemes and is machine-specific. Report memory bandwidth (e.g. STREAM) in T1.
- **Recovery policies are modelling choices:** the honest-replica assumption and the retry budget drive RQ4 results. Retry-based recovery may interact with selective-failure security; the report discusses each enabled policy against the scheme's security definition.
- **Stale code:** upstream repos are 2023–2024 research prototypes, with warnings to that effect in their READMEs.

## 14. Definition of done

- `make all` on the reference machine rebuilds everything, runs the Tier 1 grid, and regenerates F1–F6 and T1–T4 with no manual steps.
- Every number in the report traces back to a raw JSONL record (run_id + rep).
- For each Tier 2 phase attempted, its gate passed and its figure (F8–F13) regenerates from `make figures`.
- The report states at least one measured, scoped claim per RQ attempted, including negative or "could not run" results.

---

## Appendix A — Prompt to paste in front of this spec

> You are implementing the benchmark harness described in the attached spec (`vpir_bench_spec.md`). Work phase by phase (P0 → P5, then the Tier 2 phases P6–P9 in the order given in §12) and **stop at every gate G0–G7** to report what passed, what failed, and the exact logs. Obey §10 strictly: no edits to cryptographic code or parameters, no fake preprocessing in reported numbers, no invented values. Anything marked [HUMAN] is a question for me, not a decision for you. Before writing an adapter, read the upstream source for that scheme and summarise the exact call sequence and where verification happens, quoting file paths and line ranges. Start with P0: write the Dockerfile and submodule pins, build all three upstream repos, and run their test suites.

## Appendix B — Upstream entry points found in source (for the agent's orientation; verify against pinned SHAs)

- **SimplePIR** (`pir/`): `pir.go` (interface; `RunPIR`, `RunPIRCompressed`, `RunFakePIR`), `simple_pir.go`, `double_pir.go`, `database.go`, `params.csv` (LWE params). Benchmarks via env vars `LOG_N`, `D` (`go test -bench PirSingle`), and analytical communication via `go test -run=BW`. cgo flag `-O3 -march=native`.
- **VeriSimplePIR** (`src/lib/pir/preproc_pir.h`): class `VeriSimplePIR(N, d, allowTrivial, verbose, simplePIR, randomData, preproc, honestHint)`; online `Init`, `GenerateHint`, `Query`, `Answer`, `PreVerify`, `Recover`; offline `PreprocInit`, `PreprocGenerateHint`, `PreprocSampleC`, `PreprocClientMessage`, `PreprocAnswer`, `PreprocProve`, `PreprocVerify`, `PreprocRecoverZ`, `VerifyPreprocZ`; `Fake*` variants exist (dev only). Benches in `src/demo/bench/` (`simplepir_bench`, `preproc_pir_bench`, `pir_bench`, `batch_pir_bench`, `verify_bench`, `proof_bench`). Analytical comms in `src/demo/scripts/params.cpp`. Needs `clang++`, `libssl-dev`, `LD_LIBRARY_PATH=…/bin/lib`.
- **apir-code** (module `github.com/si-co/vpir-code`): `lib/client` (`NewPIR`, `NewLWE`, `QueryBytes`, `ReconstructBytes`), `lib/server` (`NewPIRTwo`, `NewPIR`, `AnswerBytes`), `lib/database` (`CreateRandomBytes`, `CreateRandomMerkle`, `CreateRandomBinaryLWEWithLength`, `CreateRandomEllipticWithDigest`), `lib/merkle`, `lib/fss`, `lib/monitor` (CPU timers). Simulation driver `simulations/simul.go` with TOML configs (`DBBitLengths` in **bits**, `Repetitions = 30`, `BitsToRetrieve = 8192`). Multi-server configs in `simulations/multi/` (`pir-classic`, `pir-merkle`, `NumServers = [2,4,6,8,10]`, `BlockLength = 1024`). Authors' raw results are in `simulations/final_results/*.json`, a useful extra RQ1 cross-check. cgo flags `-march=native -msse4.1 -maes -mavx2 -mavx`.
