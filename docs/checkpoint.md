# CS4406 checkpoint: the price of verifiability in PIR

Iman Kalyan Chakraborty (2026801003), Shambhavi Gantla (2026801014). 30 October 2026

## Goal

Verifiable private information retrieval (PIR) lets a client fetch a record privately and detect a server that answers wrongly. Each paper reports its scheme's overhead against its own baseline, on its own hardware and parameters. We run the authors' code for SimplePIR, VeriSimplePIR, and Colombo et al.'s authenticated PIR side by side on one Apple M1 Pro laptop with one harness, and first reproduce the overhead ratios the papers publish. Because our hardware differs from theirs, we compare ratios, not absolute times. We then measure what the papers do not report:

- All verifiable schemes, single-server and two-server, on the same machine, database grid, and harness.
- How the overhead changes with record size (32 B to 8 KiB) and on small databases, where we expect fixed verification costs to dominate.
- The total cost per query, including setup and client storage, amortised over 1 to 10,000 queries, and the query count at which one scheme overtakes another.
- Whether corrupted answers are actually detected, tested by fault injection, and the time and bytes needed to recover a correct answer after a rejection.
- How the extra bytes turn into latency on slower network links.

If time allows, we also measure throughput under many concurrent clients, scaling with server cores, and the cost of rebuilding after record updates.

## Progress so far

- **All three codebases build and pass their own test suites natively on Apple silicon** (6 performance and 2 efficiency cores, 16 GiB RAM). The papers' code targets x86 Linux, so this first phase was a port. We skip only apir-code's tests that need a separately downloaded PGP key dump.
- **The build is reproducible with one command.** It pins each codebase and its `dpf-go` dependency to a fixed commit, applies our patches, builds everything, and runs every test suite with a log per suite.
- **Every change to upstream code is a small patch file with a written reason.** Most change only compiler flags and library paths. VeriSimplePIR also needs three small macOS source fixes that do not change what the code computes.
- **We ported `dpf-go` to Apple silicon and proved the port exact.** Its four AES helpers existed only as x86 assembly, and we rewrote them with Go's standard AES library. DPF keys made on either architecture give identical outputs on the other in all 66 test instances, with the x86 build running under Rosetta 2. A port with one output bit flipped fails this check.
- **Every run records its machine, toolchain, and pinned commits,** and each result carries a platform tag, so numbers from different machines cannot mix.
- **We traced each of the eight scheme configurations** through its setup and query calls, the messages we will measure, and where it verifies. VeriSimplePIR verifies in a separate call. Colombo et al.'s LWE and Merkle clients verify inside reconstruction, so we can time their verification only together with decoding.

## Findings

- **The two-server baseline in Colombo et al.'s current code is not the one in their paper.** Its client sends DPF keys, added in October 2024, after the 2023 paper. Earlier commits send plain XOR vectors.
- The SimplePIR and VeriSimplePIR benchmarks skip real preprocessing by default, so our measurement code must call the real setup path.
- The single-server LWE scheme returns one bit per query, and the upstream simulation driver runs it at only three database sizes. Our record-size grid does not map onto it directly.
- The 1 GiB VeriSimplePIR cells may not fit in 16 GiB. We will record them as out of memory, not shrink them.

## What is left

We still have to write the measurement adapters for all eight configurations, with correctness and fault-injection tests, and an automated runner for the grid of databases from 1 MiB to 1 GiB. With these we will first reproduce the papers' published overhead ratios, then run the full grid and the recovery experiments, and report overhead ratios with confidence intervals.
