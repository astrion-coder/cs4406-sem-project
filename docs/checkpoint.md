# CS4406 checkpoint: the price of verifiability in PIR

Iman Kalyan Chakraborty (2026801003), Shambhavi Gantla (2026801014). 30 October 2026

## Goal

Verifiable private information retrieval (PIR) lets a client fetch a record privately and detect a server that answers wrongly. Each scheme reports its overhead against its own baseline, on its own hardware. We run the authors' code for SimplePIR, VeriSimplePIR, and Colombo et al.'s authenticated PIR side by side on one Apple M1 Pro laptop with one harness. We measure what verification costs in bytes, compute, client storage, and amortised cost per query. Because our hardware differs from the papers', we compare overhead ratios with theirs, not absolute times.

## Progress so far

- **All three codebases build and pass their own test suites natively on Apple silicon** (6 performance and 2 efficiency cores, 16 GiB RAM). The papers' code targets x86 Linux, so this first phase was a port. We skip only apir-code's tests that need a separately downloaded PGP key dump.
- **The build is reproducible with one command.** Each codebase and its `dpf-go` dependency is pinned to a fixed commit. The same command applies our patches, builds everything, runs every test suite, and writes a log and a pass or fail summary for each suite.
- **Every change to upstream code is a small patch file with a written reason.** Most change only compiler flags and library paths. VeriSimplePIR also needs three small source fixes for macOS (a missing header, an unused 128-bit float type, and two integer constructors). None of them changes what the code computes.
- **We ported `dpf-go` to Apple silicon and proved the port exact.** Its four AES helpers existed only as x86 assembly, and we rewrote them with Go's standard AES library. DPF keys made on either architecture give identical outputs on the other in all 66 test instances, with the x86 build running under Rosetta 2. A port with one output bit flipped fails this check.
- **Every run records its machine and toolchain.** That covers the CPU, cores, memory, OS, compiler versions, and the pinned commits. Each result carries a platform tag, so we cannot mix numbers from different machines.
- **We traced how each of the eight scheme configurations runs.** For each one we recorded the setup and query call sequence, the message types we will measure, and where verification happens. VeriSimplePIR verifies in a separate call. Colombo et al.'s LWE and Merkle clients verify inside reconstruction, so we can time their verification only together with decoding.

## Findings

- **The two-server baseline in Colombo et al.'s current code is not the one in their paper.** Its client sends DPF keys, added in October 2024, after the 2023 paper. Earlier commits send plain XOR vectors.
- The SimplePIR and VeriSimplePIR benchmarks skip real preprocessing by default, so our measurement code must call the real setup path.
- The single-server LWE scheme returns one bit per query, and the upstream simulation driver runs it at only three database sizes. Our record-size grid does not map onto it directly.
- The 1 GiB VeriSimplePIR cells may not fit in 16 GiB. We will record them as out of memory, not shrink them.

## What is left

We still have to write the measurement adapters for all eight configurations, with correctness and fault-injection tests, and an automated runner for the grid of databases from 1 MiB to 1 GiB and records from 32 B to 8 KiB. With these we will first reproduce the papers' published overhead ratios, then run the full grid. The report will give overhead ratios with confidence intervals, amortised cost per query, and the number of queries at which each scheme's setup cost pays off.
