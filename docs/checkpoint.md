# CS4406 checkpoint: the price of verifiability in PIR

Iman Kalyan Chakraborty (2026801003), Shambhavi Gantla (2026801014). 30 October 2026

## Goal

Verifiable private information retrieval (PIR) lets a client fetch a record privately and detect a server that answers wrongly. Each scheme reports its overhead against its own baseline, on its own hardware. We run the authors' code for SimplePIR, VeriSimplePIR, and Colombo et al.'s authenticated PIR side by side on one Apple M1 Pro laptop with one harness. We measure what verification costs in bytes, compute, client storage, and amortised cost per query. Because our hardware differs from the papers', we compare overhead ratios with theirs, not absolute times.

## Progress so far

- All three codebases now build and pass their own test suites natively on Apple silicon (6 performance and 2 efficiency cores, 16 GiB RAM). One command pins each codebase and its `dpf-go` dependency to a fixed commit, applies small portability patches, builds everything, and runs every test suite. Two caveats apply. We skip apir-code's tests that need a separately downloaded PGP key dump. VeriSimplePIR builds only with three small source fixes we still have to approve.
- `dpf-go` had x86-only assembly. We rewrote its four AES helpers with Go's standard AES library. DPF keys made on either architecture give identical outputs on the other in all 66 test instances.
- We traced how each of the eight scheme configurations runs and where it verifies, as groundwork for the measurement code.

## Findings

- **The two-server baseline in Colombo et al.'s current code is not the one in their paper.** Its client sends DPF keys, added in October 2024, after the 2023 paper. Earlier commits send plain XOR vectors.
- The SimplePIR and VeriSimplePIR benchmarks skip real preprocessing by default, so our measurement code must call the real setup path.
- The single-server LWE scheme returns one bit per query, and the upstream simulation driver runs it at only three database sizes. Our record-size grid does not map onto it directly.
- The 1 GiB VeriSimplePIR cells may not fit in 16 GiB. We will record them as out of memory, not shrink them.

## Plan

1. Week of 2 November: measurement adapters for all eight configurations, each with correctness and fault-injection tests.
2. Week of 9 November: an automated grid runner, a small end-to-end run that draws every figure, and a reproduction of the published overhead ratios.
3. Week of 16 November: the full grid (databases from 1 MiB to 1 GiB, records from 32 B to 8 KiB), overhead ratios with confidence intervals, amortised cost per query, and the report.

## Open questions

- Which two-server baseline to measure: the paper's XOR version (an earlier commit) or the current DPF version.
- How to map record sizes onto the one-bit LWE scheme.
- Whether to accept three VeriSimplePIR source fixes for macOS. They change no computation, but everything else we patch is a build setting.
- Licence: apir-code is GPL-3.0, which may make our code GPL.
