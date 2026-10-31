# CS4406 checkpoint: the price of verifiability in PIR

Iman Kalyan Chakraborty (2026801003), Shambhavi Gantla (2026801014). 30 October 2026

## Goal

Verifiable private information retrieval (PIR) lets a client fetch a record privately and detect a server that answers wrongly. Each paper reports its scheme's overhead against its own baseline, on its own hardware. We run the authors' code for eight configurations side by side on one Apple M1 Pro laptop with the same measurement code. Three have no verification: SimplePIR in Go, the C++ SimplePIR inside the VeriSimplePIR code, and classic two-server PIR. Five verify: VeriSimplePIR with and without its honest-digest assumption, Colombo et al.'s single-server LWE and DDH schemes, and their two-server Merkle-tree scheme.

We first reproduce the overhead ratios the papers publish. Because our hardware differs from theirs, we compare ratios, not absolute times. We then measure what the papers do not report:

- All verifiable schemes on the same machine and database sizes.
- How the overhead changes with record size (32 B to 8 KiB) and on small databases.
- The total cost per query including setup, amortised over 1 to 10,000 queries, and where one scheme becomes cheaper than another.
- Whether clients detect corrupted answers, which we test by injecting faults, and what it costs to recover after a rejection.
- How much latency the extra bytes add on slower networks.

If time allows, we also study concurrent clients, multicore servers, and record updates.

## Progress so far

- **All three codebases build and pass their own test suites natively on the M1 Pro, an ARM processor** (6 performance and 2 efficiency cores, 16 GiB RAM). The authors' code targets x86 Linux, so this first phase was a port. We skip only Colombo et al.'s tests that need a separate PGP key download.
- **The build is reproducible with one command.** It pins the three codebases, and the `dpf-go` library that Colombo et al.'s code uses, to fixed commits. It then applies our patches, builds everything, and runs every test suite with a log per suite.
- **Every change to the authors' code is a small patch file with a written reason.** Most patches change only compiler flags and library paths. VeriSimplePIR also needs three macOS source fixes that do not change what the code computes.
- **We ported `dpf-go` to ARM and checked it against the original.** Its four AES helpers existed only as x86 assembly, and we rewrote them with Go's standard AES library. We ran the original x86 build under Rosetta 2, Apple's x86 translator. Distributed point function (DPF) keys made on either architecture give identical outputs on the other in all 66 test instances. A port with one output bit flipped fails this check.
- **One command records the machine and toolchain.** It captures the CPU, cores, memory, OS, compiler versions, and pinned commits, plus a platform tag that every result will carry.

## Findings

- **The classic two-server PIR in Colombo et al.'s current code is not the one in their paper.** Its client sends DPF keys. The authors added that code in October 2024, after their 2023 paper. Earlier commits send plain XOR vectors.
- By default, the authors' SimplePIR and VeriSimplePIR benchmarks do not compute the real hint, the data the server sends the client during setup. SimplePIR skips it and VeriSimplePIR fills it with random values, so our measurement code must run the real setup.
- Colombo et al.'s LWE scheme returns one bit per query, and their simulation driver runs it at only three database sizes. Our planned record sizes do not map onto it directly.
- The 1 GiB VeriSimplePIR runs may exceed our 16 GiB of RAM. We record those as out of memory.

## What is left

We still have to write a measurement wrapper for each of the eight configurations, with correctness and fault-injection tests. We also need an automated runner for databases from 1 MiB to 1 GiB. With these we will first reproduce the papers' published overhead ratios. Then we will run all database and record sizes and the recovery experiments, and report overhead ratios with confidence intervals.

Iman has a background in zero-knowledge proofs, so in our first discussions with Prof. Nelakanti we planned to propose a new PIR scheme that uses them. We set it aside for lack of time, and would like to return to it after the course if Prof. Nelakanti has time.
