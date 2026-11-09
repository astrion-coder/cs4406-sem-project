# Plan: the price of verifiability in PIR

Revised 9 November 2026. Owners: Iman Kalyan Chakraborty (IC) and Shambhavi Gantla (SG).

This plan divides the remaining work between the two of us. The build contract is `vpir_bench_spec.md`. Where this plan and the spec disagree, this plan wins until task IC-1 writes the decisions below into the spec.

## Where we are

- Phase P0 is done. All three upstream codebases (SimplePIR, VeriSimplePIR, and Colombo et al.'s apir-code) build and pass their own test suites natively on the M1 Pro (`notebooks/01_g0_upstream_tests.ipynb`, cell 1). That is gate G0.
- `make setup`, `make build`, `make test` and `make sysinfo` work. The `verify-vpir-bench` skill drives and checks them.
- Nothing from phase P1 onward exists yet: no adapters, harness, analysis or results.
- Scope is everything in the spec. That means all eight configurations, the Tier 1 and Tier 2 phases P1 to P9, and the Tier 3 stretch phase P10 last.
- This schedule assumes the 20 November deadline from earlier course notes. Nobody has confirmed it.

## Decisions already made

| # | Topic | Decision |
|---|---|---|
| 1 | Machine | Every reported number comes from the M1 Pro (native arm64, Homebrew toolchain), run idle on AC power. Spec v1.3 §7. |
| 2 | Two-server code | Pin apir-code to `35cabd62fb898926f5bea6035caeae39091e091d` (15 October 2024). That is the commit before DPF queries were added, so classic two-server PIR (M0) sends plain XOR vectors as in the paper, which "uses fastxor" (§6). Only `lib/client/pir_point.go` and `lib/server/point.go` differ from the current pin, so the LWE, DDH and Merkle code is the same. |
| 3 | VeriSimplePIR macOS fixes | Accepted: `patches/verisimplepir/02` to `04`. The spec's patch rule (§10 rule 1) gets a line allowing portability fixes that do not change what the code computes. |
| 4 | VeriSimplePIR with and without the honest digest (S1, S2) | Both make the same calls, preprocessing included. Only the `honestHint` flag differs, and it changes parameter choice alone. This matches the paper (§6 item 3, p.5941) and `database.cpp:118-119`. |
| 5 | VeriSimplePIR digest check | The paper's one-time digest check (`DigVer`) is not in the upstream code. We do not write it, because the spec forbids new cryptographic code. We document it as a limitation of S2. |
| 6 | Baseline for VeriSimplePIR's published ratios | Our C++ SimplePIR (S0-cpp) is 64-bit (`mat.h:18`). We compare against the paper's "SimplePIR (64-bit)" bars, not its 32-bit headline baseline. |
| 7 | VeriSimplePIR sizes | The paper measures from 4 GiB up, and our largest database is 1 GiB. We reproduce its communication ratios at the paper's sizes with VeriSimplePIR's own `params.cpp`, since communication is deterministic. We compare compute at our sizes, with a caveat. |
| 8 | Spec's "800 MB client storage" target | Dropped. In the paper it belongs to a password-checking application on an 8 GB database, not a headline setup. |
| 9 | Single-server LWE (S3) | Use `Amplify` (the paper's LWE+), with `tECC` = 4, 5, 6, 7 at 1 MiB, 16 MiB, 128 MiB and 1 GiB. These come from the authors' `scripts/integrity_amplification.py` at the paper's 2^-64 integrity target (p.3845). No sizes above 1 GiB. |
| 10 | One-bit schemes (S3 LWE+, S4 DDH) | Report cost per retrieved bit at each database size, and leave them out of the record-size sweep. S4 runs only up to 128 MiB, with a 30-minute cap per cell. |
| 11 | SimplePIR in Go (S0-go), records over 64 bits | One query, then one `Recover` per 64-bit piece. The pieces sit in the same database column, which the single answer already covers. |
| 12 | After a rejected answer | We follow Colombo et al.: the client never retries the same server (`retry_same` is disabled for every configuration). Recovery runs use `abort`, `switch_replica` (an honest replica), and `re_preprocess` for S1 and S2, which the VeriSimplePIR paper prescribes (p.5936). If "no retry" was meant to forbid switching servers too, tell IC, and the recovery experiments shrink to detection plus abort. |
| 13 | Setup download size | Record both: the hint only (what the papers count) and the full download, including the public matrix or its seed. |
| 14 | Merkle verification time (M1) | Verification happens inside reconstruction, so record it merged (`verify_fused=true`). |
| 15 | Rounds per query | One round trip for every scheme. LWE+ sends its 2t+1 sub-queries in that one round. |
| 16 | Crashes and threads | VeriSimplePIR aborts the process on a rejected proof, so every cell runs in its own subprocess. The DDH digest build is forced to one thread. |
| 17 | Licence | apir-code is GPL-3.0. Its adapter lives in its own directory with a GPL licence file. IC confirms with Prof. Nelakanti before the repo is public. |

## How we split the work

- **IC owns the cryptographic side and the reference machine.** That covers the three adapters, fault injection, recovery and serve mode, plus every measurement run on the M1 Pro.
- **SG owns the measurement and analysis side.** That covers the result schema, the grid runner, the analysis and figures, the reproduction targets, the network experiments and the load generator.
- **The split is also by machine.** SG's work is Python and Go that runs anywhere. Synthetic result rows that match the schema are enough to build and test it. Only IC's adapters need the patched build on the Mac. All reported numbers still come from the M1 Pro.
- **Claude agents write code under each owner.** The owner reviews every change and runs the `verify-vpir-bench` skill before calling a task done.

### File ownership

Only the owner edits these paths. Ask the owner for a change instead of editing.

| Path | Owner |
|---|---|
| `vpir_bench_spec.md`, `patches/`, `third_party/`, Makefile targets `setup`, `patch`, `build` | IC |
| `src/adapters/` (all three adapters) | IC |
| `src/cs4406_sem_project/harness/recovery.py`, `configs/recovery_policies.yaml` | IC |
| `docs/serve_protocol.md` | IC |
| `src/cs4406_sem_project/harness/schema.json`, `validate.py`, `grid.py`, `run_grid.py`, `dummynet.sh` | SG |
| `configs/grid.yaml`, `configs/network_profiles.yaml`, `configs/load.yaml`, `configs/prices.yaml` | SG |
| `src/cs4406_sem_project/analysis/`, Makefile targets `smoke`, `bench`, `analyse`, `figures`, `all` | SG |
| `src/loadgen/` | SG |
| `docs/reproduction_targets.md` | SG |
| `docs/threats_to_validity.md`, `README.md` claims table, the final report | Both |

### Contracts to fix first

These are what the two halves meet on. Each has one author, and the other owner reviews it within half a day.

1. **The result record.** SG writes `schema.json` from spec §9 by 11 November, morning. IC's adapters emit exactly that.
2. **The adapter command line.** It is fixed by spec §9, and the harness calls only that.
3. **The grid file.** SG writes `configs/grid.yaml` by 11 November. It names the database sizes, record sizes, configurations and repetition counts for each tier.
4. **The serve-mode wire protocol.** IC writes `docs/serve_protocol.md` by 14 November. SG's load generator depends on it.

## Schedule

All dates are in November 2026. Measurement runs happen at night on the M1 Pro, and nobody uses that Mac while one is running.

| Dates | Phase | IC | SG | Gate |
|---|---|---|---|---|
| 10 | Start | IC-1 | SG-1, SG-2 | |
| 10 to 13 | P1 adapters | IC-2, IC-3, IC-4, IC-5 | SG-3, SG-4 | G1 (13th): correctness and fault tests pass for all 8 configurations |
| 12 to 14 | P2 harness | reviews | SG-4, SG-5 | G2 (14th): smoke run produces every figure |
| 14 to 15 | P3 reproduction | IC-6 runs | SG-5 reproduction table | G3 (15th): both review the reproduction table |
| 15 to 16 | P4 full grid | IC-6 runs (nights) | SG-5 analysis | grid complete, failures recorded |
| 15 to 17 | P6 volume and network | runs `sudo` steps | SG-6 | G4: network calibration passes |
| 15 to 17 | P7 recovery | IC-7 | | G5 is met by decision 12 |
| 16 to 18 | P8 serve mode, multicore | IC-8 | | G6: concurrent answers match sequential ones |
| 17 to 19 | P9 concurrency | runs | SG-7 | G7: load generator is not the bottleneck |
| 19 | P10 stretch | IC-9 | | |
| 19 to 20 | Report | both | both | definition of done (spec §14) |

## Tasks for IC

- **IC-1 (10th). Record decisions and move the apir-code pin.**
  - Write decisions 1 to 17 into `vpir_bench_spec.md` (v1.4).
  - Move apir-code to `35cabd6` in the Makefile and `third_party/PINS.md`, and check that its patches still apply.
  - Run `make build test`. All five suites must pass again.
  - If the older commit fails to build or test, keep the current pin, report M0 as DPF-based, and tell SG.
  - Add `docs/papers/` to `.gitignore`, merge `p0-macos` into `main`, and push so SG can start.
- **IC-2 (10 to 12). SimplePIR Go adapter (S0-go).** Write `src/adapters/simplepir_go/` to the spec §9 command line. It uses the real setup (`RunPIR`), never `RunFakePIR`, and records over 64 bits follow decision 11.
- **IC-3 (10 to 13). apir-code Go adapter.** Write `src/adapters/apir_go/`, covering:
  - S3 through `Amplify` with the `tECC` table (decision 9).
  - S4 through DDH: one thread, up to 128 MiB, a 30-minute cap.
  - M0 and M1: the two-server classic and Merkle schemes, run in one process.
  - A GPL licence file in the directory (decision 17).
- **IC-4 (10 to 13). VeriSimplePIR C++ adapter.** Write `src/adapters/vsp_cpp/` for S0-cpp, S1 and S2. It uses the real preprocessing calls (decision 4), never the `Fake*` functions, and reports parameter rejections as `status="unsupported_params"`.
- **IC-5 (12 to 13). Correctness and fault tests (gate G1).**
  - Spec §11 `test_correctness`, `test_fault_detection`, `test_fault_baseline` and `test_comm_analytical` must pass at 1 MiB for every configuration.
  - Faults are injected only in the adapters, by changing the serialised server message.
  - Afterwards, run `/maintain-verification-skill` so the skill covers the adapters.
- **IC-6 (14 to 16). Reference runs.** On the M1 Pro, run the reproduction anchors (P3), then the full grid (P4), using SG's runner. Record `make sysinfo` with every run, and follow the spec §7 hygiene: AC power, `caffeinate -dims`, nothing else running.
- **IC-7 (15 to 17). Failure and recovery (P7, RQ4).**
  - Write `configs/recovery_policies.yaml` from decision 12, and `harness/recovery.py`.
  - Run 100 trials per configuration, fault and policy, and the cheating-rate sweep (spec §5.1).
  - Produce figures F12 and F13.
- **IC-8 (16 to 18). Serve mode and multicore (P8, RQ7).**
  - Write `docs/serve_protocol.md` by the 14th, then add `--mode serve` and `--mode client` to each adapter.
  - Quote the source lines that show whether each library's answer function is thread-safe.
  - Pass `test_concurrent_correctness`, run the core sweep (W = 1, 2, 4, 6), and produce F11.
- **IC-9 (19). Stretch (P10).** Rebuild cost after record updates (RQ5), worked out from the measured setup times. A second machine for RQ7 if SG's laptop has a different CPU.

## Tasks for SG

- **SG-1 (10). Set up.**
  - Clone the repo and run the README quickstart.
  - Read spec §4 to §12 and this plan.
  - Tell IC which machine you develop on. If it is not an Apple-silicon Mac, `make setup` and `make build` will not work there, and that is fine for SG's tasks.
- **SG-2 (10 to 11). Reproduction targets.** Write `docs/reproduction_targets.md` with every published number we reproduce. Record its value, the paper's figure or table and page, and the database setup. The two PDFs are in IC's `docs/papers/`, so ask IC for them.
  - These targets are already checked against the PDFs:
    - Colombo et al. Fig 3 (p.3845): a maximum 2.9× user time and 1.8× bandwidth for the Merkle scheme over classic two-server PIR. The introduction's "2.7×" contradicts it, so use 2.9×.
    - Colombo et al. Fig 4: two to five servers at 1 GiB.
    - Colombo et al. Fig 6 (p.3846): SimplePIR is 30 to 100× faster than LWE+. LWE+ beats LWE at 1 KiB and 1 MiB but is 1.4× slower at 1 GiB. DDH takes over an hour at 1 GiB.
    - Colombo et al. §7.4 (p.3847): 1.11 s authenticated against 1.10 s unauthenticated.
    - VeriSimplePIR (p.5942): online communication 13 to 20% over SimplePIR with the honest digest and 40 to 50% without it. Compute is 12 to 40% slower without it.
  - Also record the SimplePIR README numbers in spec §3, and decisions 6 to 8.
- **SG-3 (10 to 11). Result schema.** Write `harness/schema.json` and `harness/validate.py`, with tests, from spec §9 and §6. Hand it to IC by the 11th, morning.
- **SG-4 (11 to 13). Grid and runner (P2).** Write:
  - `configs/grid.yaml` and `harness/grid.py`, with every bit and byte conversion in this one place.
  - `harness/run_grid.py`. It is resumable, runs one fresh subprocess per cell and phase, applies timeouts, and records `oom`, `timeout`, `crash` and `unsupported_params`. It captures peak memory, refuses rows whose platform tag differs from `env/reference_platform.txt`, and writes `results/raw/<run_id>/*.jsonl`.
  - A `--dry-run` that prints the run plan and an upper time bound.
  - Tests including `test_resume`.
- **SG-5 (12 to 16). Analysis and figures (P2, P3, P5).**
  - Write `analysis/aggregate.py` (medians, p95, paired bootstrap confidence intervals, overhead ratios), `amortise.py` (amortised cost, break-even query count, network latency model), `reproduce.py` and `plots.py`.
  - Produce figures F1 to F6 and tables T1 to T4.
  - Build them against synthetic rows first. Then run `make smoke` with IC's adapters (gate G2), and use the smoke cell times to estimate how long the full grid takes before IC schedules it.
  - Every number in README.md comes from a notebook cell (CLAUDE.md), so add one notebook per research question in `notebooks/`.
- **SG-6 (15 to 17). Query volume and network (P6, RQ3a and RQ3b).**
  - Run the 100-query session check, where measured and computed totals must agree within 5%.
  - Write `harness/dummynet.sh` with the three network profiles on `lo0`. IC runs it with `sudo` on the M1 Pro.
  - Pass the ping and iperf3 calibration (gate G4), then run the network experiments. Produce F8 and F9.
- **SG-7 (17 to 19). Load generator and concurrency (P9, RQ6).**
  - Write `src/loadgen/` in Go, in closed-loop and open-loop modes, against `docs/serve_protocol.md`.
  - Pass `test_loadgen_not_bottleneck` (gate G7), then run the concurrency experiments with IC. Produce F10.

## Together (19 to 20)

- Fill `docs/threats_to_validity.md` from spec §13. Add decisions 5 to 8 and the single-machine limits.
- Write the report with one measured claim per research question, negative results included.
- Run `make all` from a clean checkout and check that README.md cites a notebook cell for every number.

## Risks

1. **The schedule is tight.** The spec estimated about three and a half weeks for this work, and we have eleven days. The order above puts reproduction and the full grid first, so serve mode, concurrency and the stretch phase are what slip if anything does.
2. **Machine time is unknown.** We will not know how long the full grid takes until the smoke run. The 1 GiB VeriSimplePIR and LWE+ cells may run out of memory, which we record rather than shrink.
3. **The older apir-code commit may not build here.** IC-1 checks this first, and the fallback is the current pin with M0 reported as DPF-based.
4. **One machine runs both sides.** In the concurrency and multicore runs, the load generator and the server share the M1 Pro, and macOS cannot pin cores. `test_loadgen_not_bottleneck` catches the worst case. SG's laptop can take the load generator if that test fails.
5. **The deadline is assumed.** If it is later than 20 November, keep this order and spread the dates.
