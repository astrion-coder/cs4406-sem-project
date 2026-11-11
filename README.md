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

Measurement machine: Apple M1 Pro, 8 cores (6 performance + 2 efficiency), 17179869184 bytes (16 GiB) RAM, macOS 26.6.1, Python 3.11.17, Go 1.27.3, Homebrew clang 23.1.4, OpenSSL 3.5.9 ([00_environment.ipynb](./notebooks/00_environment.ipynb), cell 1 [exec 1]).

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

# Just for test
