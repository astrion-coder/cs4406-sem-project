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
| Adapter memory at the test sizes, the basis for the tests' 2 GiB cap | the largest peak footprint is 0.29 GiB (S3 at 1 MiB), so the cap is 6.9 times it. The other seven cases peak at 0.24 GiB or less | `04_p7_memory_findings.ipynb`, cell 3 [exec 3] |
| S4 (DDH on P-256) cost per database size, measured awake, on AC and idle | a digest build takes 22.5 s at 64 KiB, 85.8 s at 256 KiB and 342.0 s at 1 MiB, and one query takes 1.21 s, 4.56 s and 17.61 s. Both grow about linearly with size (fitted exponents 0.98 and 0.97) | `06_s4_cost.ipynb`, cell 2 [exec 2] |
| S4 cannot meet the 30-minute cap per cell (decision 10) at the spec's repetition counts (6 digests offline, 31 queries online) | measured at 1 MiB: offline cell 34 min (over), online cell 15 min. Extrapolated: 517 min and 218 min at 16 MiB, 3985 min and 1647 min at 128 MiB | `06_s4_cost.ipynb`, cell 3 [exec 3] |
| Serve mode is free of data races under concurrent load (gate G6's thread-safety half) | with the Go race detector and ThreadSanitizer, both shown live on a deliberate race, 8 of 8 configurations ran 60 s at W = 6 with 16 clients and malformed traffic: 0 race reports, 0 mismatched answers among 4,747,596 checked | `05_serve_thread_safety.ipynb`, cells 2 and 3 [exec 2, 3] |

# Just for test
