# The price of verifiability in PIR: a common-hardware re-evaluation

Iman Kalyan Chakraborty (2026801003), Shambhavi Gantla (2026801014). CS4406, 13 November 2026

## Summary

Verifiable private information retrieval (PIR) lets a client fetch a record without revealing which one, and detect a server that answers wrongly. Each paper reports its scheme's cost against its own baseline, on its own hardware. We ran the authors' code for eight configurations on one Apple M1 Pro laptop, under one measurement harness, over databases from 1 MiB to 1 GiB and records from 32 B to 8 KiB. Every number below comes from a raw result row and is listed in the repository's README with the notebook cell that computes it.

PENDING_SUMMARY

## What we compared

Three configurations do not verify and serve as baselines:

- **S0-go**: SimplePIR, the authors' Go code.
- **S0-cpp**: the 64-bit SimplePIR inside the VeriSimplePIR code base.
- **M0**: classic two-server XOR PIR from Colombo et al.'s code.

Five verify:

- **S1** and **S2**: VeriSimplePIR with and without its honest-digest assumption. Upstream does not implement the one-time digest check the full scheme needs, so S2 runs without it.
- **S3** and **S4**: Colombo et al.'s single-server authenticated PIR over integrity-amplified LWE and over DDH. Both retrieve one bit per query.
- **M1**: Colombo et al.'s two-server Merkle-tree authenticated PIR.

Each verifiable configuration is compared with the baseline from its own code base: S1 and S2 against S0-cpp, M1 against M0. The two-server schemes assume the servers do not collude, so a cost comparison across families is not a security equivalence.

## How we measured

- **One machine.** Apple M1 Pro, 6 performance and 2 efficiency cores, 16 GiB RAM, macOS, Go 1.27 and Homebrew clang. The upstream code targets x86 Linux. We ported it with build-only patches, each with a written reason, and ported `dpf-go`'s x86 assembly to Go's AES library, checked bit for bit against the original under Rosetta. All three upstream test suites pass natively.
- **The authors' code, real setup.** Each configuration has a small adapter that calls the library the way its own tests do. No adapter uses the fake or random preprocessing that some upstream benchmarks default to.
- **A fresh process per cell and phase.** The offline phase (hint or digest, proofs, client checks) and the online phase (query, answer, recover, verify) run separately, at least 5 offline and 30 online repetitions (3 offline at 1 GiB), single-threaded. A cell may use at most 14 GiB, and a cell that needs more is recorded as out of memory, not shrunk.
- **Hygiene checks on every run.** On AC power, lid open, nothing else heavy running. Each run compares wall and monotonic time to catch sleep, and records power state at both ends.
- **Statistics.** Medians over repetitions. Overhead is median(verifiable) / median(baseline) with a paired bootstrap 95 % interval over 10 000 resamples.
- **Faults are injected in the adapters only**, by changing the serialised server message, never the cryptographic code.

The Tier 1 grid covers 4 database sizes by 4 record sizes for the six record-size configurations, S3 at four sizes and S4 at 64 KiB to 1 MiB. S4 takes 342 s per digest at 1 MiB, so its larger sizes are extrapolated from a linear fit and labelled as such.

PENDING_RESULTS

## What we found in the upstream code

- **SimplePIR's own query path mis-addresses packed entries.** When several entries share one Z_p element, upstream's `Query` and `Recover` still address the element, not the entry. With real setup on a one-bit database most queries panic or return a wrong bit. The README's one-bit numbers come from a benchmark that uses a random hint and checks no answer. Our adapter addresses packed entries itself.
- **A corrupted Merkle proof makes M1's client try to allocate tens of GiB.** `merkle.DecodeProof` sizes an allocation from the proof's first four bytes before reading any hash. A cheating server can therefore exhaust the client's memory instead of being caught. The adapter refuses these decodes and records them as rejections.
- **VeriSimplePIR leaks memory per phase.** Its matrix type has no destructor. The adapter runs offline repetitions in a child process so the leak dies with it, except where that does not fit in memory.
- **Colombo et al.'s current classic two-server PIR is not the paper's.** Since October 2024 its client sends DPF keys. We pinned the commit before that change, where it sends XOR shares as the paper describes.
- **Upstream benchmarks skip the real setup by default.** SimplePIR's skips the hint and VeriSimplePIR's fills it with random values. Our adapters always run the real setup.

## Limits

- **Ratios, not absolute times.** Our machine is an ARM laptop, the papers used x86 servers. We compare overhead ratios with the papers. Absolute times differ by hardware factors that we report separately.
- **Implementation, not only scheme.** A ratio across code bases mixes scheme cost with engineering. The price pairs therefore stay within one code base, and S0-go against S0-cpp measures the implementation effect on its own.
- **Memory.** With 16 GiB of RAM, S2 and S3 do not fit at 1 GiB, and M1 at 1 GiB fits only with records of 1 KiB and up. Their 1 GiB comparisons are missing. Cells close to the 14 GiB cap run while macOS compresses memory, which can slow them.
- **S2 is not the full scheme.** Upstream lacks the digest check, so S2's overhead understates the paper's full VeriSimplePIR.
- **S4 beyond 1 MiB is extrapolated** from a linear fit to its measured sizes.
- **Tier 2 runs at one cell.** RQ3a, RQ3b, RQ4, RQ6 and RQ7 run at 128 MiB × 1 KiB with reduced counts, so they say nothing about how their effects move with size. RQ6's load generator shares the machine with the server, and gate G7 checks that it is not the bottleneck.
- **Emulated network.** RQ3b shapes loopback traffic with macOS dummynet. Its 10 ms timer tick cannot emulate the LAN profile's 0.5 ms round trip, so LAN runs at full loopback latency with its bandwidth limit only.
- **No core pinning.** macOS cannot pin threads, so a single-threaded run may move between performance and efficiency cores.

The full list, with evidence for each entry, is in `docs/threats_to_validity.md`.

## Reproducing this report

`make build test` rebuilds the three code bases and reruns their test suites. `make all` reruns the Tier 1 grid and the reproduction anchors and redraws F1 to F6. `make session-check`, `make netem`, `make concurrency` and `make figures` cover the Tier 2 runs. The README lists every number in this report with the notebook and cell that produce it, and each figure is written by the cell named in its caption.
