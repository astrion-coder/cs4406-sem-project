# CS4406 — The Price of Verifiability in PIR

The implementation spec is [vpir_bench_spec.md](./vpir_bench_spec.md). Code lives in `src/cs4406_sem_project/`, measurement notebooks in `notebooks/`.

## Quickstart

```sh
brew install go llvm openssl@3 iperf3
uv sync
make setup     # pins the upstream repos as submodules in third_party/ and downloads Go modules
make build     # applies patches/ and builds all upstream code
make test      # runs the upstream test suites, logs under logs/p0/
make sysinfo   # writes env/sysinfo.json and env/reference_platform.txt
```

## Layout

```
Makefile  README.md  CLAUDE.md  pyproject.toml
env/          hardware description and sysinfo output
third_party/  simplepir, verisimplepir, apir-code (pinned git submodules), PINS.md
patches/      build-only patches per submodule, README.md
configs/  results/  tests/  docs/  notebooks/  logs/ (gitignored)
src/
  cs4406_sem_project/   harness/, analysis/
  adapters/             simplepir_go/, apir_go/, vsp_cpp/
  loadgen/
```

## Numeric claims

Every number stated about this project must appear here with the notebook and cell that produced it. "Cell N" counts every cell in the notebook from 1, Markdown included; the execution count shown by Jupyter is given as `[exec M]`. Re-run with:
`uv run jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=-1 <notebook>`
(on AC power, with Low Power Mode off).

Measurement machine: Apple M1 Pro, 8 cores (6 performance + 2 efficiency), 17179869184 bytes (16 GiB) RAM, macOS 27.2 (build 26B123), Python 3.11.17, Go 1.27.3, Homebrew clang 23.1.4, OpenSSL 3.5.9 ([00_environment.ipynb](./notebooks/00_environment.ipynb), cell 1 [exec 1]).

| Claim | Value | Source |
|---|---|---|
| Gate G0: every P0 upstream suite builds and passes natively on this Mac | 3 of 3 suites passed: simplepir `go test`, apir-code `go test` at `35cabd6` (`TestReal*` skipped, they need the SKS key dump), VeriSimplePIR `pir_test` | `01_g0_upstream_tests.ipynb`, cell 1 [exec 1] |
| Gate G1: every configuration passes the adapter conformance test | 56 of 56 tests passed for S0-go, S0-cpp, S1, S2, S3, S4, M0 and M1, at 1 MiB with 100 repetitions per check, at the three pinned upstream commits | `02_g1_conformance.ipynb`, cell 1 [exec 1] |
| P7: every configuration injects the P7 faults and follows its recovery policies as the adapter contract defines | 159 of 159 tests passed for S0-go, S0-cpp, S1, S2, S3, S4, M0 and M1: `stale_record`, `drop_server` and `bad_proof` detection, `abort`, `switch_replica` and `re_preprocess` with honest and cheating replicas, no false rejects in 1000 honest queries per verifiable arm, at 1 MiB (S4 at 64 KiB) with 100 faulted repetitions per check | `03_p7_recovery.ipynb`, cell 1 [exec 1] |
| M1 (apir-code's Merkle scheme): a corrupted proof makes upstream's client try to allocate far more memory than the machine has, before it rejects the answer | in 100 cheating-replica repetitions at 1 MiB, the adapter's guard refused 218 upstream decodes. Each would have allocated a median of 47.2 GiB (max 94.4 GiB) in `merkle.DecodeProof`, and 184 of the 218 exceed this Mac's 16 GiB | `04_p7_memory_findings.ipynb`, cell 2 [exec 2] |
| Adapter memory at the test sizes, the basis for the tests' 2 GiB cap | the largest peak footprint is about 0.3 GiB (S3 at 1 MiB), so the cap is about 7 times it. The other seven cases peak at 0.24 GiB or less. The footprint is sampled every 0.1 s, so the S3 peak moves by a few hundredths between runs (0.29 and 0.27 GiB on 10 November) | `04_p7_memory_findings.ipynb`, cell 3 [exec 3] |
| S4 (DDH on P-256) cost per database size, measured awake, on AC and idle | a digest build takes 22.5 s at 64 KiB, 85.8 s at 256 KiB and 342.0 s at 1 MiB, and one query takes 1.21 s, 4.56 s and 17.61 s. Both grow about linearly with size (fitted exponents 0.98 and 0.97) | `06_s4_cost.ipynb`, cell 2 [exec 2] |
| S4 cannot meet the 30-minute cap per cell (decision 10) at the spec's repetition counts (6 digests offline, 31 queries online) | measured at 1 MiB: offline cell 34 min (over), online cell 15 min. Extrapolated: 517 min and 218 min at 16 MiB, 3985 min and 1647 min at 128 MiB | `06_s4_cost.ipynb`, cell 3 [exec 3] |
| Grid feasibility at 16 MiB, 128 MiB and 1 GiB: one repetition per cell (`--phase all`) of every arm but S4, at 32 B and 8 KiB records, under a 10 GiB footprint cap, awake and on AC | 32 of 39 cells finished with correct answers. Over the cap: M1 at 32 B at 128 MiB and 1 GiB, S1 and S2 at 1 GiB at both record sizes, and S3 at 1 GiB. The largest footprint that fitted was S0-cpp at 1 GiB, 9.76 GiB | `07_grid_feasibility.ipynb`, cells 2 and 3 [exec 2, 3] |
| Memory ceiling: notebook 07's over-cap cells rerun at 14 GiB, the most a cell can have on this 16 GiB Mac | M1 at 128 MiB and 32 B fits, peaking at 11.74 GiB with a correct answer. S1 and S2 at 1 GiB at both record sizes, M1 at 1 GiB and 32 B, and S3 at 1 GiB all need more than 14 GiB | `08_memory_ceiling.ipynb`, cells 2 and 3 [exec 2, 3] |
| Expected machine time for RQ4 as `configs/recovery_policies.yaml` sets out the spec (356 Runs), priced from notebook 07's single repetitions | 1115.5 h (46.5 days), against a worst-case bound of 3888.0 h. S1 and S2 `re_preprocess` at 128 MiB take 1028.8 h of it (sweep 429.5 h and 359.8 h, trials 130.3 h and 109.2 h) | `09_rq4_plan.ipynb`, cell 1 [exec 1] |
| Expected machine time for alternatives to the spec's RQ4 counts | without `re_preprocess` 86.8 h. With `re_preprocess` at 30 + 1 repetitions in the trials and at f ≥ 0.5, 209.9 h, or 143.1 h with it at 32 B records only. The spec's own shrink to 1 GiB × 1 KiB takes 13.6 h, but leaves S1, S2 and S3 with no cell | `09_rq4_plan.ipynb`, cell 2 [exec 2] |
| Planning bounds for a grid cell at the spec's repetitions, from notebook 07's single repetitions (not results; the run started with a 1-minute load average of 7.5, in `results/raw/grid-feasibility-20261111T064956Z/meta.json`) | the slowest cells are S2 at 128 MiB (offline cell 19.3 min at either record size), S1 at 128 MiB and 8 KiB (19.1 min) and S0-cpp at 1 GiB (17.4 min at 8 KiB) | `07_grid_feasibility.ipynb`, cell 3 [exec 3] |
| Serve mode is free of data races under concurrent load (gate G6's thread-safety half) | with the Go race detector and ThreadSanitizer, both shown live on a deliberate race, 8 of 8 configurations ran 60 s at W = 6 with 16 clients and malformed traffic: 0 race reports, 0 mismatched answers among 4,747,596 checked | `05_serve_thread_safety.ipynb`, cells 2 and 3 [exec 2, 3] |
| RQ4 run: every arm at 128 MiB × 1 KiB (S3 one bit, S4 at 64 KiB), with notebook 10's reduced trial, offline and sweep counts | 116 of 116 Runs finished ok and citable in 6.79 h of adapter time | `10_rq4_day.ipynb`, cell 3 [exec 3] |
| RQ4 reruns after the apir-adapter fixes (S3's shadow client, M1's `recovery_s`, collecting after an injected fault) | notebook 12 reran 16 Runs (S3 under `wrong_record`, M1 under `switch_replica`), all ok in 1.12 h. Notebook 13 reran S3's 8 again, all ok in 0.79 h, with peak footprints of 6.66 to 6.86 GiB | `12_rq4_rerun.ipynb`, cell 3 [exec 3]; `13_rq4_rerun_s3.ipynb`, cell 3 [exec 3] |
| What the reruns changed | under `wrong_record`, S3's median query generation per Run was 0.95 to 1.33 s in notebook 10 and is 0.59 s after the fixes. Its peak footprints were 9.67 to 10.71 GiB and are 6.66 to 6.86 GiB. An M1 recovery attempt read 0.074 s and reads 0.037 s | `11_rq4_analysis.ipynb`, cell 7 [exec 7] |
| RQ4 detection at 128 MiB × 1 KiB | every faulted first answer of every verifiable arm was rejected, for each of the four online faults: 200 of 200 for S1, S2 and M1 (Wilson 95 % lower bound 0.981), 60 of 60 for S3 and S4 (0.940). S1 and S2 rejected 3 of 3 faulted offline phases per fault (0.438). The baselines accepted every flipped, wrong and stale answer, 100 of 100 each, and aborted only on a withheld one. No honest sweep answer was rejected: 3,385 to 5,817 per arm, 193 for S3 and S4 | `11_rq4_analysis.ipynb`, cell 2 [exec 2] |
| RQ4 recovery cost (F12): time to a verified record or to abort as a multiple of the fault-free latency, median over trials | `abort` 0.82 to 0.88 for S1 and S2, 0.97 to 1.00 for S3, S4 and M1. `switch_replica` 1.82 to 1.88 for S1 and S2, 1.97 to 2.00 for S3, S4 and M1, adding one fault-free query's bytes. `re_preprocess` at f = 0.1, 8 recoveries each: 4,240 times for S1 and 4,655 times for S2, adding 4,573 and 4,603 times the query's bytes. Figure `docs/figures/F12_recovery_cost.png` (cell 4) | `11_rq4_analysis.ipynb`, cell 3 [exec 3] |
| RQ4 expected cost against the cheating rate (F13): the sweep's mean time per query against the cost computed from the trials' and offline Runs' components (spec 6.1) | at f > 0, 17 of 18 `abort` points and 17 of 18 `switch_replica` points have overlapping 95 % intervals, and all 36 lie within 3.9 % of the computed cost. The 2 `re_preprocess` points (S1 and S2 at f = 0.1, 50 queries) overlap only through wide intervals: the measured mean is 1.98 times the computed one. Figure `docs/figures/F13_expected_cost.png` (cell 6) | `11_rq4_analysis.ipynb`, cell 5 [exec 5] |
| Gate G2: `make smoke` (the six record-size arms at 1 MiB × 1 KiB, S3 at 1 MiB, S4 at 64 KiB, 3 repetitions each; decision 23) runs in under 10 minutes and its rows produce every Tier 1 figure | 2.8 min wall on AC with the lid open; 16 of 16 (cell, phase) runs ok, 0 wrong answers, 0 schema errors; F1 to F6 and T1 to T4 built from those rows | `14_g2_smoke.ipynb`, cells 1 to 3 [exec 1, 2, 3] |
| Upstream SimplePIR's own `Query` and `Recover` mis-address entries packed several to a Z_p element, so the S0-go adapter addresses them itself for the one-bit anchor (decision 21) | with real setup on a seeded 2^16-entry one-bit database (9 entries per element), 18 of 200 queried entries came back right, 7 wrong, and 175 panicked; on 8-bit entries, which do not pack, 200 of 200 came back right | `15_rq1_anchors.ipynb`, cell 1 [exec 1] |
| RQ1 anchors run: S0-go at 1 GiB as 2^33 one-bit entries (SimplePIR README), S3 and S4 at 1 KiB (Colombo et al. Fig 6) | 6 of 6 (cell, phase) runs ok, 0 wrong answers, 0 schema errors, 7.9 min; S0-go's peak footprint 6.74 GiB | `15_rq1_anchors.ipynb`, cell 2 [exec 2] |
| RQ1 anchor targets | R23: hint 120.7 MiB against the README's 121 MB, pass. R24: online 241.4 KiB against 242 KB, pass. R25: 19.2 GB/s on one core against the README's 10 GB/s/core on a c5n.metal, a hardware factor of 1.9. Fig 6 at 1 KiB: S3's and S4's digest and online bytes within 2 % of the plot readings, pass; their user times 1.19 ms and 35.3 ms against about 18 and 130 ms, hardware factors | `15_rq1_anchors.ipynb`, cell 3 [exec 3] |
| Planning bound for the Tier 1 grid (206 cell-phase runs), from notebooks 06, 07, 08 and 14 (not a result) | 8.9 h of adapter time; S1 2.4 h, S2 2.0 h, S0-cpp 1.9 h, S4 1.1 h; the slowest run is S4's 1 MiB offline cell, 34.2 min | `16_tier1_plan.ipynb`, cell 1 [exec 1] |
| Planning bound for RQ6 and RQ7 (`make concurrency`): warm-up plus window of every load-generator run at the 20 focus cells expected to fit in memory | 63.0 h with `configs/load.yaml`'s 30 runs per worker count (RQ6 16.3 h, RQ7 46.7 h), or 21.0 h with 3 runs per worker count as RQ6 uses | `16_tier1_plan.ipynb`, cell 2 [exec 2] |
| Tier 1 grid through `make all` (spec P4): 206 (cell, phase) runs at 1 MiB to 1 GiB | 187 of 206 ok. 18 ran out of memory at the 14 GiB cap: S2 at 1 GiB in both phases (8), S1 at 1 GiB offline (4), M1 at 1 GiB with 32 B and 256 B records in both phases (4), M1 at 128 MiB × 32 B offline, and S3 at 1 GiB online. S3 at 1 GiB offline timed out at 7200 s. No wrong answers or schema errors | `17_tier1_grid.ipynb`, cell 2 [exec 2] |
| Adapter time of the Tier 1 grid and the RQ1 anchors, from their ledgers | 8.43 h for the 187 ok runs, against notebook 16's bound of 8.9 h. 11.55 h with the failed runs' final attempts (1.11 h out of memory, 2.0 h timed out), 13.95 h with the 2 rejected attempts. Anchors 6 of 6 ok in 0.13 h | `17_tier1_grid.ipynb`, cell 4 [exec 4] |
| Background load during the grid: its medians against earlier runs of the same cells (S4's cost runs of 10 November, notebook 07's feasibility run of 11 November) | 0.864 to 1.061 times, median 1.014, over 32 cells. The low end is M1 offline against the feasibility run | `20_grid_conditions.ipynb`, cell 2 [exec 1] |
| S3 at 1 GiB offline: preprocessing per repetition against 8 times its 128 MiB median (890 s) | 1055 s (1.19 times) before the lid closed. 1566, 2688 and 2563 s (1.76 to 3.02 times) beside interactive use. 1396, 2509 and 2725 s (1.57 to 3.06 times) on a quiet machine. Both full attempts timed out at 7200 s after three of four repetitions | `20_grid_conditions.ipynb`, cell 3 [exec 2] |
| Peak footprint, offline against online, in the Tier 1 grid | where both phases fit, offline is 1.32 to 1.38 times online for S0-cpp, 1.59 to 1.71 for S0-go, 1.33 to 1.37 for S1, 1.36 to 1.37 for S2 and 1.08 to 1.72 for M1. S1 at 1 GiB runs out of memory offline but fits online at 11.55 to 11.72 GiB. M1 at 128 MiB × 32 B runs out offline but fits online at 11.65 GiB | `20_grid_conditions.ipynb`, cell 5 [exec 3] |
| How macOS counts a forked child's memory, and what VeriSimplePIR's offline child costs | a 2 GiB allocation counts 2.08 GiB to the parent and 0.01 GiB to a child that only reads it. System memory grows 1.99 GiB for the allocation and 0.01 GiB for the fork. S1 at 128 MiB × 32 B offline: parent 1.13 GiB plus child 1.57 GiB, a sum of 2.70 GiB. Online, preprocessing in-process, 2.03 GiB. So preprocessing adds 0.90 GiB in-process and 1.57 GiB in the child | `20_grid_conditions.ipynb`, cells 6 and 7 [exec 4, 5] |
| S3's footprint at 1 GiB, and across repetitions at 128 MiB | 10.54 GiB offline at 1 GiB (the lid-closed attempt; the timed-out attempts record no peak), 14.04 GiB online when it ran out of memory. At 128 MiB the footprint when each repetition starts is 1.27, 1.80 and 1.94 GiB, while preprocessing stays at 113 s | `20_grid_conditions.ipynb`, cell 8 [exec 6] |
| RQ1 reproduction verdicts (T3, F1) over the Tier 1 grid and the anchors | 32 pass, 10 hardware factor, 7 context (different baseline), 1 explain, 7 missing. Explain: M1/M0 user time at 1 GiB, 1.554 against 1.9. Missing: S3's 1 GiB targets (R5, R9, R10, R11, R12) and S2's at 1 GiB (R15, R17) | `18_rq1_reproduction.ipynb`, cells 2 and 3 [exec 2, 3] |
| RQ2 largest overhead per pair (T4, F2) | M1/M0: 14.14 times server time and 23.19 times online bytes at 128 MiB × 32 B, 8.37 times client time at 1 MiB × 32 B. S2/S0-cpp: 2.19 times server time and 1.50 times online bytes at 1 MiB × 8 KiB. S1/S0-cpp: at most 1.066 times | `19_rq2_overheads.ipynb`, cell 3 [exec 3] |

# Just for test
