# Reproduction targets (RQ1)

> **Identifiers.** RQ1 to RQ7 (research questions) and H1 to H7 (hypotheses) are defined in [the spec](../vpir_bench_spec.md) §2, P0 to P10 (phases), G0 to G7 (gates), F1 to F13 (figures) and T1 to T4 (tables) in §12, and the tests behind each gate in §11. R1 to R28 (published reproduction targets) are defined in this file, and spec §3 summarises them. Decisions 1 to 30 are numbered in [plan.md](../plan.md).

Task SG-2. This file lists every published number we try to reproduce, so that `analysis/reproduce.py` can compare our measured values and ratios against them. Owner: SG.

## How to read this file

- **Sources.** [CNCG+23] is Colombo, Nikitin, Corrigan-Gibbs, Wu and Ford, "Authenticated private information retrieval", USENIX Security 2023, printed pages 3835 to 3851 (`usenixsecurity23-colombo.pdf`). [dCL24] is de Castro and Lee, "VeriSimplePIR", USENIX Security 2024, printed pages 5931 to 5946 (`usenixsecurity24-de-castro.pdf`). Page numbers are the ones printed in the page footer, not PDF page indices. In both PDFs the first PDF page is the USENIX cover, so printed page = PDF index + 3833 for [CNCG+23] and PDF index + 5929 for [dCL24].
- **Exact vs plot.** A value quoted in the text is exact. A value marked *plot* was read off a figure by eye and is approximate; the axis it was read on is given. Log-axis readings are good to roughly a factor of 1.2. Treat plot values as a sanity band, never as an exact target.
- **Kind.** `ratio` targets compare our ratio of two arms against the paper's ratio. `abs` targets compare an absolute number.
- **Comparison rule** (decisions 6 and 7 in `plan.md`):
  - `exact`: communication is deterministic given the parameters, so we compute it analytically (or measure it) and expect equality with a text value. Against a plot value, we expect agreement within plot-reading precision.
  - `hw`: compute time differs by hardware. Ratios between arms on the same machine should land in the published range or be explained. Absolute times only need to agree within a hardware factor; we report the factor rather than pass or fail.
  - `trend`: only the direction or shape is compared (for example "overhead grows with N").
- **Arms** are those of spec §4: S0-go, S0-cpp, S1, S2, S3, S4, M0, M1 (and Tier 3 M1-k, DoublePIR).
- `TODO[HUMAN]: check` marks anything we could not settle from the PDF.

## Authenticated PIR, Colombo et al. [CNCG+23]

Setup for every target in this group (§7, p.3845): two Intel Xeon E5-2680 v3 (Haswell) CPUs at 2.5 GHz per machine, 256 GB RAM, Ubuntu 20.04, Go 1.17.5, 10 Gigabit Ethernet. Multi-server experiments and Keyd run client and servers on separate machines; single-server experiments run on one machine. One core per machine, 30 runs, median reported. "User time" is query computation to record reconstruction; "bandwidth" is cumulative traffic to and from all servers (p.3845).

Parameters (p.3845): Merkle scheme uses BLAKE3. DDH uses P256. LWE (2^-128 integrity) uses q = 2^128, n = 4800. LWE+ (2^-64 integrity, integrity amplification with the repetition code) uses q = 2^32, n = 1100. Both LWE variants use a discrete Gaussian with σ = 6.4.

### Two-server point queries (M1 against M0)

| ID | Claim | Published value(s) | Where | DB setup | Arms | Kind / rule |
|---|---|---|---|---|---|---|
| R1 | Maximum overhead of Merkle authenticated PIR over classic two-server PIR | "maximum overhead of 2.9× for user time and of 1.8× for bandwidth" | §7.1, Fig 3, p.3845 | 2 servers, 1 KiB records, DB sizes on the plot axis 0 to 10 GiB, baseline classic XOR PIR | M1/M0 | ratio; user time `hw`, bandwidth `exact` (within plot precision). See note R1a. |
| R2 | Overheads grow with database size, because each record carries an O(λ log N) Merkle proof | "Both the user time and the bandwidth overheads increase as the database size increases" | §7.1, p.3845 | as R1 | M1/M0 across 1 MiB to 1 GiB | `trend` |
| R3 | Fig 3 per-size values | *plot*, user time [s] axis: unauthenticated about 0.13, 0.22, 0.4, 0.68, 0.77, 0.95 and authenticated about 0.25, 0.62, 1.1, 1.68, 2.22, 2.4 at 1, 2, 4, 6, 8, 10 GiB. *plot*, bandwidth [MiB] axis: unauthenticated about 2.1, 3.0, 4.2, 5.1, 5.9, 6.6 and authenticated about 3.4, 4.9, 7.1, 8.8, 10.2, 11.6 at the same sizes. A leftmost point at x = 0 has no labelled size. TODO[HUMAN]: check which DB size the x = 0 point is. | Fig 3, p.3845 | as R1 | M0, M1 at 1 GiB (only the 1 GiB point is in our grid) | abs; user time `hw`, bandwidth `exact` within plot precision |
| R4 | Cost against number of servers at 1 GiB | *plot*, user time [s] axis: unauthenticated about 0.13 to 0.14 at k = 2 to 5; authenticated about 0.26, 0.28, 0.29, 0.315 at k = 2, 3, 4, 5. *plot*, bandwidth [MiB] axis: unauthenticated about 2.1, 3.1, 4.2, 5.2; authenticated about 3.4, 5.1, 6.8, 8.5. Text: user-time increase "almost negligible", bandwidth "increases linearly for both schemes" | §7.1, Fig 4, p.3845 (text continues on p.3846) | 1 GiB, 1 KiB records, 2 to 5 servers | M1-k/M0-k (Tier 3) | ratio and `trend`; bandwidth `exact` within plot precision, time `hw` |

**Note R1a, the 2.9× vs 2.7× discrepancy.** The introduction says "this form of authentication imposes less than 2.7× computational and 1.8× bandwidth overhead, compared with unauthenticated PIR" (§1, p.3836). §7.1 says "We measure a maximum overhead of 2.9× for user time and of 1.8× for bandwidth" (p.3845). We use 2.9×, the measured value next to its figure. Reading Fig 3, the 2.9× maximum sits near 8 GiB (about 2.22 s against 0.77 s), outside our grid. At 1 GiB the plot gives about 1.9× user time and about 1.6× bandwidth, so at our sizes M1/M0 should stay below the 2.9× and 1.8× maxima. The analysis should compare our 1 GiB ratio against the 1 GiB plot reading (R3) and check the trend (R2), not expect 2.9× itself.

### Single-server point queries (S3, S4 against S0-go)

All of §7.3 retrieves one data bit. SimplePIR runs "with its default configuration of 2048-bit database records. The client downloads a corresponding record and selects a desired bit from it" (p.3846). "Offline bandwidth" is the digest for the authenticated schemes and the hint for SimplePIR (p.3846). The SimplePIR baseline is "the original implementation of SimplePIR" (§6, p.3844), which is our S0-go.

| ID | Claim | Published value(s) | Where | DB setup | Arms | Kind / rule |
|---|---|---|---|---|---|---|
| R5 | SimplePIR is much faster than LWE+ | "SimplePIR is 30-100× faster than LWE+" | §7.3, p.3846; also abstract, "30-100× more costly", p.3835 | 1 KiB, 1 MiB, 1 GiB, 1 bit retrieved | S3/S0-go at 1 MiB and 1 GiB | ratio, `hw`. See note R5a. |
| R6 | LWE+ uses 2t + 1 = 15 repetitions at 1 GiB | "the repetition code requires repeating the protocol 15 times (t = 7)" | §7.3, p.3846 | 1 GiB, 2^-64 integrity target (p.3845) | S3 at 1 GiB (`tECC` = 7, decision 9) | abs, `exact` (parameter check) |
| R7 | Integrity amplification multiplies online bandwidth by 2t + 1 | "integrity amplification increases online bandwidth by 2t + 1× (Section 3.3), whereas the client must download the digest only once" | §7.3, p.3847 | as R5 | S3 online bytes against one base-scheme run | ratio, `exact` |
| R8 | DDH is impractical at 1 GiB | "DDH takes over an hour to retrieve a data bit from a 1 GiB database" | Fig 6 caption, p.3846 | 1 GiB, 1 bit | S4, extrapolated to 1 GiB from the fitted scaling (decision 10), labelled extrapolated | abs, `hw` (only "> 3600 s" is checked) |
| R9 | Fig 6 user time | *plot*, user time [ms], log axis. 1 KiB: DDH about 130, LWE about 45, LWE+ about 18, SimplePIR about 0.5. 1 MiB: DDH about 6×10^4, LWE about 900, LWE+ about 350, SimplePIR about 5. 1 GiB: LWE about 3.8×10^4, LWE+ about 5.4×10^4, SimplePIR about 400 (DDH omitted). | Fig 6, p.3846 | as R5 | S4 at 1 MiB; S3 and S0-go at 1 MiB and 1 GiB | abs, `hw` |
| R10 | Fig 6 offline bandwidth (digest or hint) | *plot*, offline bandwidth [KiB], log axis. 1 KiB: DDH about 3, LWE about 6.8×10^3, LWE+ about 390, SimplePIR about 820. 1 MiB: DDH about 93, LWE about 2.2×10^5, LWE+ about 1.25×10^4, SimplePIR about 4.1×10^3. 1 GiB: LWE about 7×10^6, LWE+ about 4×10^5, SimplePIR about 1.2×10^5. | Fig 6, p.3846 | as R5 | S3, S4, S0-go setup download (hint-only count, decision 13) | abs, `exact` within plot precision |
| R11 | Fig 6 online bandwidth | *plot*, online bandwidth [KiB], log axis. 1 KiB: DDH about 5.8, LWE about 2.8, LWE+ about 5, SimplePIR about 0.045. 1 MiB: DDH about 185, LWE about 90, LWE+ about 200, SimplePIR about 6.3. 1 GiB: LWE about 2.9×10^3, LWE+ about 1.1×10^4, SimplePIR about 235. | Fig 6, p.3846 | as R5 | S3, S4, S0-go online bytes | abs, `exact` within plot precision |
| R12 | Concrete offline bandwidth is lower for SimplePIR | "Concrete offline bandwidth is lower in SimplePIR due to database packing" | §7.3, p.3847 | as R5 | S3 digest against S0-go hint | `trend` |

**Note R5a.** From the R9 plot readings, SimplePIR is about 36× faster than LWE+ at 1 KiB and about 70× at 1 MiB, but about 135× at 1 GiB (5.4×10^4 ms against about 400 ms), which is above the stated 100×. The plot is on a log axis, so this may be reading error. TODO[HUMAN]: check p.3846 Fig 6 against the authors' published result files in apir-code before treating 100× as the upper bound at 1 GiB.

**Note on `tECC` at small sizes.** The paper states t = 7 only for 1 GiB (p.3846). It does not print t for the 1 KiB and 1 MiB LWE+ bars. Our `tECC` = 4, 5, 6 at 1 MiB, 16 MiB, 128 MiB come from the authors' `scripts/integrity_amplification.py` (decision 9), not from the paper. TODO[HUMAN]: check that the 1 MiB LWE+ bar in Fig 6 used t = 5.

**Note on SimplePIR in Fig 6.** The Fig 6 SimplePIR values at 1 GiB (hint about 1.2×10^5 KiB, online about 235 KiB, both *plot*) are of the same size as the SimplePIR README numbers (R23, R24), which supports using S0-go as this baseline.

## VeriSimplePIR, de Castro and Lee [dCL24]

Setup for every target in this group (§6, p.5941): C/C++, one thread, Ubuntu 20, Intel i7 at 2.5 GHz, 32 GB RAM, clang++ 10 with -O3. "All SimplePIR benchmarks were taken with the VeriSimplePIR library using optimal parameters" and parameters were "chosen to minimize online communication". The SimplePIR baseline uses log(q) = 32, log(n) = 10; a second baseline "SimplePIR (64-bit)" is "optimized for online computation on a 64-bit machine". Database entries are one bit (d = 1). Results are given in terms of total database size (p.5941 to p.5942). Figures 7 and 8 are printed on p.5943; the text that describes them is on p.5942.

The headline percentages (R13 to R16) are against the 32-bit SimplePIR baseline: "The minimal communication parameters from SimplePIR use log(q) = 32, while the minimal parameters for VeriSimplePIR use log(q) = 64" (p.5942). Our S0-cpp is 64-bit, so the comparable targets are the 64-bit bars in R17 to R19 (decision 6).

| ID | Claim | Published value(s) | Where | DB setup | Arms | Kind / rule |
|---|---|---|---|---|---|---|
| R13 | Headline online overhead | "The online communication overhead is roughly 1.1-1.5× SimplePIR, and the online computation time on the server is essentially the same" | Abstract, p.5931 | 1-bit entries, 4 GiB and up, 32-bit SimplePIR baseline | S1/S0-cpp, S2/S0-cpp | ratio; comm `exact` at paper sizes via `params.cpp`, compute `hw` at our sizes (decision 7) |
| R14 | Online communication overhead range | "the concrete communication overhead for the online phase is between 13% and 50% over SimplePIR, depending on database sizes and whether or not the honest-digest assumption is in place. The computation overhead for VeriSimplePIR is at most 40% if the honest digest assumption is not in place" | §1.2, p.5932 | as R13 | as R13 | ratio; as R13 |
| R15 | Without the honest digest (full VeriSimplePIR) | "a 12% to 40% slowdown in the compute time and a 40% to 50% increase in the total communication" | §6, Benchmarks: Online Phase, p.5942 | Fig 7: compute at 4, 8, 16 GiB; communication at 4 to 256 GiB; 32-bit SimplePIR baseline | S2/S0-cpp | ratio; comm `exact` (analytic at paper sizes), compute `hw` (our sizes, with caveat) |
| R16 | With the honest digest | "the communication overhead of VeriSimplePIR drops to 13% to 20%, and the online computation actually outperforms the original semi-honest SimplePIR protocol" | §6, p.5942 | as R15 | S1/S0-cpp | ratio; as R15 |
| R17 | Fig 7 online compute, per query (total, with the client part hatched) | *plot*, seconds, linear axis. Totals at 4 / 8 / 16 GiB: SimplePIR about 0.58 / 1.08 / 2.23; SimplePIR (64-bit) about 0.54 / 1.03 / 1.97; VeriSimplePIR (honest digest) about 0.56 / 1.04 / 1.97; VeriSimplePIR about 0.77 / 1.54 / 2.51. Server part (unhatched) at 4 / 8 / 16 GiB: about 0.44 / 0.89 / 1.94, 0.42 / 0.86 / 1.72, 0.43 / 0.86 / 1.71, 0.61 / 1.31 / 2.19 in the same order. | Fig 7 left, p.5943 | 1-bit entries, 4, 8, 16 GiB | S0-cpp, S1, S2 (our sizes 1 MiB to 1 GiB only) | ratio, `hw`. Against the 64-bit bars the plot gives roughly 1.0× for S1 and roughly 1.27× to 1.5× for S2. |
| R18 | Fig 7 online communication, per query (upload equals download) | *plot*, MiB, linear axis. At 4 / 8 / 16 / 32 / 64 / 128 / 256 GiB: SimplePIR about 0.47 / 0.67 / 1.00 / 1.41 / 2.00 / 2.83 / 4.00; SimplePIR (64-bit) about 0.56 / 0.80 / 1.13 / 1.60 / 2.31 / 3.27 / 4.62; VeriSimplePIR (honest digest) about 0.58 / 0.82 / 1.16 / 1.64 / 2.37 / 3.35 / 4.73; VeriSimplePIR about 0.70 / 1.02 / 1.45 / 2.11 / 3.00 / 4.23 / 6.19. | Fig 7 right, p.5943 | 1-bit entries, 4 to 256 GiB | S0-cpp, S1, S2 via `params.cpp` at the paper's sizes (decision 7) | abs and ratio, `exact` within plot precision. Against the 64-bit bars the plot gives roughly 1.02× to 1.04× for S1 and roughly 1.25× to 1.34× for S2. |
| R19 | Fig 8 offline communication (hint or digest plus proof preprocessing) | *plot*, GiB, linear axis. At 4 / 8 / 16 / 32 / 64 / 128 / 256 GiB: SimplePIR about 0.23 / 0.34 / 0.50 / 0.71 / 1.00 / 1.42 / 2.00; SimplePIR (64-bit) about 0.57 / 0.80 / 1.13 / 1.60 / 2.30 / 3.27 / 4.62. VeriSimplePIR (honest digest), stored part / total: about 0.57 / 1.33, 0.80 / 1.90, 1.15 / 2.68, 1.62 / 3.80, 2.33 / 5.53, 3.30 / 7.82, 4.68 / 11.1. VeriSimplePIR, stored part / total: about 0.68 / 1.62, 1.00 / 2.36, 1.43 / 3.35, 2.08 / 4.92, 2.95 / 6.97, 4.17 / 9.88, 6.10 / 14.5. | Fig 8, p.5943 | as R18 | S0-cpp, S1, S2 setup download via `params.cpp` | abs, `exact` within plot precision |
| R20 | Composition of offline communication | Stored part "is dominated (> 95%) by the size of the digest H1"; the part not stored is "almost entirely (> 95%) the matrix H2, the commitment to D^T" | §6, Benchmarks: Offline Phase, p.5942 | as R18 | S1, S2 message breakdown | ratio, `exact` |
| R21 | A 64-bit SimplePIR hint is more than twice the 32-bit one | "Growing the SimplePIR modulus to 64 bits would more than double the size of the database digest" | §6, p.5942 | as R18 | S0-cpp hint against a 32-bit SimplePIR hint at the same size. S0-go is 32-bit but a different implementation, so this is indicative only. | ratio, `exact` |
| R22 | Offline (preprocessing) compute, dishonest digest, one core | The sentence is garbled in the PDF: "the offline computation with a dishonest digest is roughly 100 seconds for a 4 GB database is roughly 200 seconds and for an 8 GB database. The computation is almost entirely on the server and scales linearly with the database." Read as about 100 s at 4 GB and about 200 s at 8 GB. TODO[HUMAN]: check this reading of p.5942. | §6, p.5942 | 1-bit entries, 4 GB and 8 GB | S2 offline time at our sizes, compared as time per GB under the stated linear scaling | abs, `hw` and `trend` (linear) |

## SimplePIR README (Henzinger et al., USENIX Security 2023)

Checked on 12 November against `README.md` at the pinned SHA `e9020b03bf2872c75b8954e749e32408b5db87ed` (line 82): "On a database 1 GB in size containing $2^{33}$ 1-bit entries, SimplePIR has a throughput of roughly 10 GB/s/core, 121 MB of offline download, and 242 KB of online communication, when running on an AWS EC2 c5n.metal instance". The DoublePIR line (87) gives "roughly 7.4 GB/s/core, 16 MB of offline download, and 345 KB of online communication". The README's MB and KB are binary units: the hint is `ell × n × 4` bytes and the online bytes `(m + ell) × 4`, and only MiB and KiB put them near 121 and 242. The S0-go anchor runs this setup through `--record-bytes 0` (decision 21).

| ID | Claim | Value (README) | DB setup | Arms | Kind / rule |
|---|---|---|---|---|---|
| R23 | SimplePIR hint size | ~121 MB | 1 GB, 2^33 entries of 1 bit | S0-go anchor (spec §5) | abs, `exact` |
| R24 | SimplePIR online communication | ~242 KB | as R23 | S0-go anchor | abs, `exact` |
| R25 | SimplePIR server throughput | ~10 GB/s/core, AWS c5n.metal | as R23 | S0-go anchor | abs, `hw` |
| R26 | DoublePIR hint size | 16 MB | as R23 | DoublePIR (optional, Tier 3) | abs, `exact` |
| R27 | DoublePIR online communication | 345 KB | as R23 | DoublePIR (optional) | abs, `exact` |
| R28 | DoublePIR server throughput | 7.4 GB/s/core | as R23 | DoublePIR (optional) | abs, `hw` |

R23 and R24 are consistent in size with the SimplePIR bars of [CNCG+23] Fig 6 at 1 GiB (R10, R11; *plot*, p.3846).

## Decisions 6, 7 and 8 and how they change the comparisons

- **Decision 6, 64-bit baseline.** S0-cpp is 64-bit (`mat.h:18`). [dCL24]'s headline ratios R13 to R16 are against 32-bit SimplePIR (p.5941, p.5942). So `reproduce.py` compares S1/S0-cpp and S2/S0-cpp against the "SimplePIR (64-bit)" bars of Fig 7 and Fig 8 (R17 to R19). R13 to R16 are still reported, but as context with the baseline mismatch stated. The plot readings suggest the gap matters: honest-digest communication overhead is roughly 2% to 4% over the 64-bit bars against 13% to 20% over the 32-bit ones.
- **Decision 7, paper sizes.** [dCL24] measures from 4 GiB up (Fig 7, Fig 8, p.5943) and our largest database is 1 GiB. Communication is deterministic, so R18 to R21 are recomputed at 4 to 256 GiB with VeriSimplePIR's own `params.cpp` and compared `exact` (within plot precision). Compute targets R15 to R17 and R22 are compared at our sizes (1 MiB to 1 GiB) with a caveat that the paper never measured there; only ratios are checked, and only loosely (`hw`).
- **Decision 8, the 800 MB target is dropped.** "The only overhead from VeriSimplePIR is the roughly 800 MB of data that each client must locally store throughout the online phase" (§7, p.5943) belongs to the Blyss password-checking application, "a hash table about 8 GB in size" holding "around 400 million SHA1 hashes (20 bytes each)" (p.5943). It is not a headline setup and is not a target. If anyone wants a cross-check, the stored part of the 8 GiB bars in Fig 8 is the comparable quantity (R19).

## Not reproducible here

| Claim | Where | Why not |
|---|---|---|
| Fig 3 points above 1 GiB, and therefore the 2.9× and 1.8× maxima as measured | [CNCG+23] §7.1, Fig 3, p.3845 | Our largest DB is 1 GiB (16 GiB RAM, spec §5). We compare the 1 GiB point and the trend (R2, R3). |
| LWE (q = 2^128) results: LWE+ "slower (1.4×) for the 1 GiB database" and faster at 1 KiB and 1 MiB; "The LWE construction is also faster (3-79×)" than DDH; DDH "has twice the online bandwidth of the LWE construction" | [CNCG+23] §7.3, p.3846 | No arm runs plain LWE; S3 is LWE+ only (decision 9). The spec §3 row lists the "LWE/LWE+ crossover" as a comparison, which our arms cannot make unless an LWE arm is added. |
| Fig 6 values at 1 KiB | [CNCG+23] Fig 6, p.3846 | 1 KiB is not in our grid (spec §5). |
| Keyd point lookup: "1.11 seconds for authenticated PIR, 1.10 seconds for unauthenticated PIR and 0.22 seconds for non-private direct look-up"; abstract "under 1.2 core-seconds" | [CNCG+23] §7.4, p.3847; abstract p.3835 | 3,557,164 keys, about 3 GiB (p.3845), above our 1 GiB limit; client and servers on separate machines over 10 GbE with an added 0.4 ms RTT (p.3847); we have one machine. Kept as an optional sanity point only (spec §3). |
| Keyd capacity: 12 cores authenticated against 11 cores unauthenticated | [CNCG+23] §7.4, p.3847 | An estimate for a multithreaded deployment, not a measurement we can repeat. |
| Predicate queries: overhead "less than 1.1×" (Fig 5, p.3846), Table 7 medians "1.01× for user time and 1.05× for bandwidth" (p.3847), "1.02×" (§1, p.3836), "at least 350×" more than point queries (p.3836) | [CNCG+23] §7.2, §7.4 | No FSS or predicate-query arm in spec §4. |
| VLHE PIR against APIR and APIR+: APIR+ runtime "roughly 5×" and APIR "about 25×" that of VLHE PIR; "near 40% reduction" in online communication against APIR; "7-12×" smaller against APIR+; APIR+ offline "about 4.5× smaller"; "roughly 7×" at 1024-bit entries | [dCL24] §6.1, p.5942 | VLHE PIR (non-preprocessed) is not an arm, and Fig 9 (p.5944) uses 4 to 256 GiB databases. |
| Spiral "around 25× slower online computation" than VeriSimplePIR; DoublePIR worse in both | [dCL24] §7, p.5943 | Spiral is not an arm; the setting is the 8 GB password application. |
| Multi-machine timings over 10 GbE | [CNCG+23] p.3845 | Single M1 Pro (decision 1). Our M0/M1 user time has no real network; network is modelled (spec §5). |
| SimplePIR and DoublePIR throughput on AWS c5n.metal (R25, R28) | SimplePIR README, line 82 and 87 | Different hardware; kept only as a `hw` sanity check. |

## Discrepancies found while checking plan.md and spec §3 against the PDFs

1. **2.9× vs 2.7×** (confirmed). §1 p.3836: "less than 2.7× computational and 1.8× bandwidth overhead". §7.1 p.3845: "maximum overhead of 2.9× for user time and of 1.8× for bandwidth". We use 2.9×, as plan.md says. Also, by the plot, the maximum is near 8 GiB and the 1 GiB ratio is about 1.9×, so 2.9× is not a target at our sizes (note R1a).
2. **VeriSimplePIR percentages are against 32-bit SimplePIR.** plan.md lists "13 to 20% ... and 40 to 50%" and "12 to 40% slower" as targets without the baseline; p.5942 ties them to log(q) = 32 SimplePIR. Under decision 6 the comparable targets are the 64-bit bars (R17 to R19), which give much smaller honest-digest overheads.
3. **"40 to 50%" is total communication, not only "online communication".** p.5942 says "a 40% to 50% increase in the total communication" in the online benchmarks paragraph. plan.md calls it online communication. The paragraph is about Fig 7 (online), so this is probably the same thing; noted for precision.
4. **Page of Figures 7 and 8.** plan.md cites p.5942. The text is on p.5942 but Figures 7 and 8 are printed on p.5943.
5. **"30 to 100×" at 1 GiB.** The Fig 6 plot reading at 1 GiB gives about 135× (note R5a). Possibly reading error on a log axis. TODO[HUMAN]: check.
6. **LWE/LWE+ crossover is in spec §3 but no arm produces plain LWE** (see Not reproducible).
7. **Offline compute sentence in [dCL24] is garbled** (R22, p.5942).
8. **Fig 4 page.** plan.md gives no page for Fig 4; it is on p.3845.
9. The other plan.md citations check out: Fig 3 p.3845, Fig 6 p.3846, §7.4 p.3847 (1.11 s against 1.10 s), and t = 7 with 15 repetitions at 1 GiB (p.3846) with the 2^-64 target stated on p.3845.
