# Threats to validity

The report must address each threat below (spec §13). This file is owned by both IC and SG. The first section repeats the spec's list, each with where the project handles it. The second adds threats found while building and testing the adapters, with the evidence for each. Entries marked **open** still need data or a decision before the report.

## From spec §13

- **Implementation, not scheme.** Ratios across codebases mix scheme cost with engineering quality. Handled by the within-codebase pairs (S1 and S2 against S0-cpp, M1 against M0) and the S0-go against S0-cpp ablation. **Open:** the ablation needs grid data.
- **Different security levels and parameters.** Every record carries `scheme_params`. The report claims equal security only where the papers state it.
- **Fake versus real preprocessing.** Spec rule 2. Every adapter calls the real setup and preprocessing (`src/adapters/*/README.md`).
- **S2 lacks the digest check.** Upstream has no `DigVer`, and rule 1 forbids writing it (decision 5). S2's overhead against S0-cpp understates the paper's full scheme.
- **VeriSimplePIR ratios against a different baseline and at different sizes.** We compare with the paper's 64-bit SimplePIR bars (decision 6) and reproduce its communication ratios at its sizes analytically (decision 7).
- **LWE+ `tECC` values partly derived.** The 16 MiB and 128 MiB values come from the authors' `scripts/integrity_amplification.py` at the 2^-64 target, not from the paper (decision 9).
- **Single-threaded, single machine.** The main grid runs single-threaded, two-server timing is in-process, and network effects are modelled.
- **Trust assumptions differ.** Two-server schemes assume non-colluding servers. A cost comparison is not a security equivalence.
- **Apple silicon versus the papers' x86-64 hardware.** Compare ratios, not absolute times.
- **Performance and efficiency cores, no pinning.** A single-threaded run may move between core types. RQ7 stops at W = 6.
- **Unified memory bandwidth.** No bandwidth counters (§6). Multicore results depend on this machine's memory system.
- **Portable cryptoTools backends in VeriSimplePIR.** arm64 builds use the portable `block` and AES, not the authors' SSE2 path.
- **Go 1.27 instead of 1.19, and clang instead of GCC.** Both arms of each pair use the same toolchain.
- **Thermal throttling.** **Open:** the analysis checks each cell for drift across repetitions.
- **Load generator and server on one machine (RQ6).** **Open:** needs the load generator.
- **Emulated networks and loopback (RQ3b).** **Open:** needs the dummynet runs.
- **Serving wrappers are ours.** One protocol and one server loop per language serve every arm (`docs/serve_protocol.md`).
- **Recovery policies are modelling choices.** Honest replicas, a budget of 3 further attempts, and no `retry_same` (decision 12, `configs/recovery_policies.yaml`).
- **Stale code.** The upstream repos are 2023 to 2024 research prototypes.

## Found while building the adapters

- **Measurement conditions on a laptop.** `caffeinate` cannot stop clamshell sleep, and macOS's monotonic clock stops while the machine sleeps. A timing run with the lid closed therefore shows normal timed spans while taking hours. This happened once, and that run was discarded. Every timing run since checks wall against monotonic time, power source and lid at both ends, and fails otherwise (`harness/hygiene.py`, `notebooks/06_s4_cost.ipynb` cell 1).
- **S4 is measured only up to 1 MiB.** At the spec's repetitions an S4 offline cell takes 34 minutes at 1 MiB. So the 16 MiB and 128 MiB costs are extrapolated from a linear fit (exponents 0.98 for the digest and 0.97 per query) and labelled as such (decision 10, spec v1.6, `notebooks/06_s4_cost.ipynb` cells 2 and 3). S4 has no RQ4 focus cell for the same reason.
- **An upstream M1 client can be made to allocate up to 96 GiB.** `merkle.DecodeProof` allocates as many slice headers as a corrupted proof's first 4 bytes say, before reading any hash. The apir-code adapter refuses such decodes, which always panic, and records the same rejection with `client_recover_s` null. So faulted M1 repetitions carry no reconstruction time for those answers. The refused decodes would have allocated a median 47.2 GiB (`notebooks/04_p7_memory_findings.ipynb` cell 2). This is a finding about the library, not something the benchmark adds.
- **`drop_server` has no client timeout.** No adapter models how long a client waits before it gives up on a withheld answer, so under `drop_server` the time to a correct answer is a lower bound (`src/adapters/README.md`, "Recovery").
- **Memory is sampled, not traced.** Peak footprints come from polling every 0.1 s, so a short peak can be missed. On reruns the S3 peak moved between 0.27 and 0.29 GiB (`notebooks/04_p7_memory_findings.ipynb` cell 3). Adapters in the tests run under a 2 GiB cap, and a cell that needs more fails rather than shrinks.
- **VeriSimplePIR leaks memory per online repetition.** Upstream `Matrix` has no destructor, so peak RSS grows by about 44 KiB per S1 online repetition at 1 MiB in the parent process. Offline phases run in a child process so their leak dies with it (`src/adapters/vsp_cpp/README.md`, "Known limits"). Online `process_peak_rss_bytes` therefore grows with the repetition count.
- **VeriSimplePIR pads small databases with long records.** Upstream sizes its matrix from the entry count alone, so a long record at a small database runs on a larger matrix. Every record carries `scheme_params.padding_factor`, and the analysis flags cells above 1.1 (decision 19).
- **Thread safety rests partly on reading the code.** The Go race detector and ThreadSanitizer found no race in any serve-mode arm, and both caught a deliberate one (`notebooks/05_serve_thread_safety.ipynb` cells 2 and 3). Neither sees inside the libraries' C and C++ code, so concurrent `Answer` calls there rest on the code reading in `docs/serve_protocol.md`.
- **Serve and client mode seed what bench mode draws at random.** S0-go uses upstream's `InitCompressedSeeded`, and VeriSimplePIR draws `A` with upstream's own `pseudorandom`, both keyed by `--seed`, so a client can rebuild the server's offline state. The distribution is the same as in bench mode, which keeps upstream's draw (`docs/serve_protocol.md`, "Session").
