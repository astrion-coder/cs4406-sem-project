# CS4406 checkpoint: the price of verifiability in PIR

Iman Kalyan Chakraborty, 30 October 2026

## Goal

Verifiable private information retrieval (PIR) lets a client fetch a record privately and detect a server that answers wrongly. Each scheme reports its overhead against its own baseline, on its own hardware. We run the authors' code for SimplePIR, VeriSimplePIR, and Colombo et al.'s authenticated PIR on one machine and one harness, and measure what verification costs in bytes, compute, client storage, and amortised cost per query. The build contract is `vpir_bench_spec.md`.

## What changed

- We replaced the earlier retrieval-pipeline (latency SLA) topic with this study.
- Everything runs natively on one Apple M1 Pro (6 performance and 2 efficiency cores, 16 GiB RAM). All three codebases target x86, so phase 0 became a port. We will compare overhead ratios with the papers, not absolute times.

## Done: the build gate (G0) passes

- `make setup` pins the three codebases and their `dpf-go` dependency at fixed commits. `make build` applies our patches, each justified in `patches/README.md`. `make test` runs every upstream test suite.
- All five suites pass on arm64: SimplePIR, apir-code, dpf-go, VeriSimplePIR, and our DPF equivalence check (`notebooks/01_g0_upstream_tests.ipynb`, cell 1). Two caveats apply. apir-code's `TestReal*` tests are skipped because they need a separately downloaded PGP key dump. VeriSimplePIR builds only with three macOS fixes that still need sign-off (see below).
- We rewrote the four x86-only AES helpers in `dpf-go` with Go's standard AES library. DPF keys made on either architecture give identical outputs on the other in all 66 test instances.
- `docs/upstream_call_sequences.md` traces how each of the eight arms runs and where it verifies.

## Findings

- **M0 is not the paper's baseline at our pinned commit.** Its two-server client sends DPF keys, added in October 2024 (commit `9940e0a`), after the 2023 paper. Earlier commits send plain XOR vectors.
- The SimplePIR and VeriSimplePIR benchmarks skip real preprocessing by default, so our adapters must call the real setup path.
- The single-server LWE arm returns one bit per query, and the upstream simulation driver runs it at only three database sizes. Our record-size grid does not map onto it directly.
- The 1 GiB VeriSimplePIR cells may not fit in 16 GiB. We will record them as out of memory, not shrink them.

## Next

1. Adapters for all eight arms behind one interface, with correctness and fault-injection tests (gate G1).
2. A resumable grid runner and a smoke run that draws every figure (gate G2).
3. Reproduce the published overhead ratios (RQ1), then run the full grid and analysis.

## Decisions pending

- M0 and M1: pin a pre-DPF commit to match the paper, or keep DPF and report it.
- How to map record sizes onto the one-bit LWE arm.
- Sign-off on three VeriSimplePIR macOS fixes beyond our build-only rule (a missing include, a guard on an unused `__float128` type, two integer constructors). None changes the computation.
- Licence: apir-code is GPL-3.0, which may make our adapters GPL.
