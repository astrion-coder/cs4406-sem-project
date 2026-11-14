# Upstream call sequences (P1 pre-adapter reading)

> **Identifiers.** RQ1 to RQ7 (research questions) and H1 to H7 (hypotheses) are defined in [the spec](../vpir_bench_spec.md) §2, P0 to P10 (phases), G0 to G7 (gates), F1 to F13 (figures) and T1 to T4 (tables) in §12, and the tests behind each gate in §11. R1 to R28 (published reproduction targets) are defined in [docs/reproduction_targets.md](reproduction_targets.md). Decisions 1 to 30 are numbered in [plan.md](../plan.md).

Spec Appendix A step: "read the upstream source for that scheme and summarise the exact call sequence and where verification happens". Read-only survey, no builds run.

**Pins read.** `third_party/simplepir` e9020b0, `third_party/verisimplepir` 3643bb7, `third_party/apir-code` 7a7f204, `third_party/dpf-go` 6eae873.

**Path prefixes.** `SP/` = `third_party/simplepir/pir/`. `VSP/` = `third_party/verisimplepir/src/`. `AP/` = `third_party/apir-code/`. `DPF/` = `third_party/dpf-go/dpf/`.

**Patched files.** Build patches were in progress while this was written. `AP/lib/matrix/matrix.go` and `AP/lib/matrix/matrix128.go` are cited at upstream `HEAD` line numbers (`git show HEAD:<file>`), because the working-tree patch shifts them by one line. No line in the VSP `Makefile`, `lib/math/` or `lib/bigint/`, or in dpf-go's patched `aes.go`/`aes_other.go`, is cited.

---

## S0-go: SimplePIR (Go)

**Offline.** Reference driver `SP/pir.go:99-171` (`RunPIR`):
1. `pi.PickParams(N, d, n, logq)`. Caller passes `n = 1<<10`, `logq = 32` (`SP/pir_test.go:13-14`).
2. `MakeRandomDB(N, d, &p)` (`SP/database.go:168-176`) or `MakeDB(N, d, &p, vals)` (`SP/database.go:178-214`).
3. `shared_state := pi.Init(DB.Info, p)` (`SP/pir.go:111`). This samples `A` as an `M x N` matrix (`SP/simple_pir.go:99-102`).
4. `server_state, offline_download := pi.Setup(DB, shared_state, p)` (`SP/pir.go:115`). `Setup` computes `H := MatrixMul(DB.Data, A)` and then **mutates the DB in place** (`DB.Data.Add(p.P / 2)`, `DB.Squish()`) at `SP/simple_pir.go:119-130`.
5. No client offline verification exists.

The compressed variant `RunPIRCompressed` (`SP/pir.go:174-247`) replaces step 3 with `pi.InitCompressed` plus `pi.DecompressState` (`SP/pir.go:186-187`). Both reseed the package-global PRG (`bufPrgReader = NewBufPRG(NewPRG(seed))`, `SP/simple_pir.go:110` and `:115`). After that, every `RandInt` draw, including the client secret in `Query`, comes from the stream keyed by the public seed. Global PRG: `SP/rand.go:36-37`.

**Online.** `pi.Query(i, shared, p, DB.Info)` returns `(State{secret}, Msg{query})` (`SP/simple_pir.go:145-160`). The query is padded to a multiple of `info.Squishing` (`:155-157`), so `Query` needs the post-`Setup` `DB.Info`. Next, `pi.Answer(DB, MsgSlice{query}, server_state, shared, p)` (`:162-183`). Then `pi.Recover(i, 0, offline_download, query, answer, shared, client_state, p, DB.Info)` returns `uint64` (`:185-214`). `RunPIR` calls `pi.Reset(DB, p)` (un-squish) **before** `Recover` (`SP/pir.go:149`). **Verification:** none. `verify_fused` is not applicable.

**Messages.** `Msg{Data []*Matrix}` (`SP/utils.go:14-24`). `Matrix{Rows, Cols uint64; Data []C.Elem}` (`SP/matrix.go:9-13`) with `typedef uint32_t Elem` (`SP/pir.h:4`). No serialiser exists. Upstream counts `Size()*Logq/8` bytes (`SP/pir.go:66`, `:117`, `:134`, `:144`), which for `Logq=32` equals `4*Rows*Cols`. Offline download counted upstream is `H` only (`L x N`). `A` (`M x N`) or the 16-byte seed (`CompressedState`, `SP/utils.go:10-12`) is not counted. Analytical helper: `GetBW` (`SP/simple_pir.go:88-97`), driven by `TestSimplePirBW` with env `LOG_N`, `D` (`SP/pir_test.go:113-134`).

**Parameters.** `PickParams` iterates `mod_p` and `ApproxSquareDatabaseDims` (`SP/simple_pir.go:14-44`). `Params.PickParams` reads `sigma` and `p` from embedded `params.csv` matching `n`, `logq` and `m <= 2^logm` (`SP/params.go:37-80`). `params.csv` has rows for `log(n)=10`, `log(q)=32` and `log(m)` 13 to 21 only. Record: `p.N, p.Sigma, p.L, p.M, p.Logq, p.P` (`SP/params.go:12-21`) plus `DB.Info.Ne`, `Packing`, `Basis=10`, `Squishing=3` (`SP/database.go:34-35`).

**Fake paths to avoid.** `RunFakePIR` / `FakeSetup` (`SP/pir.go:41-96`, `SP/simple_pir.go:132-143`). `FakeSetup` never computes `H`. `BenchmarkSimplePirSingle` uses it (`SP/pir_test.go:408`). Real sequence: `TestSimplePir` and `TestSimplePirLongRow` (`SP/pir_test.go:161-169`, `:182-190`).

**Thread safety of `Answer`.** `Answer` reads `DB.Data` through `SelectRows`, which returns a view without copying (`SP/matrix.go:400-416`). It writes only into a fresh `out` allocated in `MatrixMulVecPacked` (`SP/matrix.go:200-221`) and into a fresh `ans`. No shared mutable state is touched. The DB must stay squished, and `Reset` (`SP/simple_pir.go:216-220`) must not run while answering. Client-side `Query` draws from the mutex-guarded global PRG (`SP/rand.go:42-47`, `:134-149`). `Recover` subtracts from and re-adds to the answer in place (`SP/simple_pir.go:201`, `:211`).

**Sizes.** `d` is **bits per entry** (`Row_length`, `SP/database.go:8`). Entry values are `uint64`. `MakeDB` takes `vals []uint64` (`SP/database.go:182-184`), and `Recover`/`GetElem` reconstruct through `Reconstruct_from_base_p` in `uint64` (`SP/utils.go:77-85`). A spec record of 32 B or more (256 bits or more) does not fit one entry. Upstream correctness tests use `d <= 32` (`SP/pir_test.go:163`, `:184`). Rejections are panics: `"No suitable params known!"` (`SP/params.go:78-79`), `"Params and database size don't match"` and `"Number of DB elems per entry must divide DB height"` (`SP/database.go:157-163`), and `"Bad params"` in `Squish` (`SP/database.go:42-45`). `MakeRandomDB` uses the unseeded global PRG (`SP/rand.go:155-157`).

---

## S0-cpp: SimplePIR in the VeriSimplePIR codebase

**Constructors.** The spec names `VeriSimplePIR(N, d, ..., simplePIR=true, preproc=false)`. The upstream bench and test instead use `VLHEPIR(N, d, true, verbose, simplePIR=true, randomData)` (`VSP/demo/bench/simplepir_bench.cpp:11`, `VSP/demo/test/pir_test.cpp:7`). Both call `db.computeParams(..., simplePIR, batchSize, preproc=false, honestHint)` (`VSP/lib/pir/pir.h:28`, `VSP/lib/pir/preproc_pir.h:34`). With `simplePIR=true` the SIS check is skipped (`VSP/lib/pir/preproc_pir.h:50-51`).

**Offline.** Real sequence in `basic_pir_test` (`VSP/demo/test/pir_test.cpp:4-41`) and `basic_preproc_pir_test` (`:207-244`):
1. `D = pir.db.packDataInMatrix(pir.dbParams)`, an `ell x m` matrix (`:11`).
2. `A = pir.Init()`: `lhe.genPublicA(m)`, `m x n` (`VSP/lib/pir/preproc_pir.cpp:300-302`, `VSP/lib/pir/lhe.cpp:4-8`).
3. `H = pir.GenerateHint(A, D)`: `ell x n` (`VSP/lib/pir/preproc_pir.cpp:311-319`).
4. No client offline verification.

**Online.** `ct_sk = pir.Query(A, index)` returns `(ct m x 1, sk n x 1)` (`VSP/lib/pir/preproc_pir.cpp:327-345`). Then `ans = pir.Answer(ct, D)` (`:347-355`) or `pir.Answer(ct, D_packed)` (`:357-365`). `D_packed` comes from `packMatrixHardCoded(D, p)` (`VSP/demo/test/pir_test.cpp:102`). Last, `pir.Recover(H, ans, sk, index)` returns `entry_t` (`VSP/lib/pir/preproc_pir.cpp:392-404`). **Verification:** none.

**Messages.** No serialiser in `lib/pir` (grep for `serial` finds only `lib/math/gsl`). `Matrix{rows, cols; Elem* data}` row-major (`VSP/lib/pir/mat.h:22-25`) with `typedef uint64_t Elem` (`VSP/lib/pir/mat.h:18`), so bytes = `rows*cols*8`. Upstream prints only `H` as offline download (`VSP/demo/bench/simplepir_bench.cpp:46`). `A` (`m x n`) has no seed form in this codebase: `genPublicA` calls `random(A)` with a fresh system seed (`VSP/lib/pir/mat.cpp:41-44`). Analytical helper: `VSP/demo/scripts/params.cpp` with `simplePIR=true` set by hand (`:30`). It prints hint, upload and download bits (`:42-45`, `:61-74`). It is a `main()` with hard-coded `N_d_params` (`:5-19`).

**Parameters.** `LHE::n = 2048` and `logq = 64` for `Elem = uint64_t` (`VSP/lib/pir/lhe.h:6`, `:10`). `p`, `m`, `ell` come from `computeParams` (`VSP/lib/pir/database.cpp:343-449`), which uses `get_simple_pir_m_and_ell` (`:383`, `:93-107`) and only the slack-LWE check with `honest_hint=true` (`:410`). `sigma = 6.4` (`:149`). `LWE_ERROR_PROB = 2^-40` (`:5`). Record `pir.dbParams.{p,m,ell}`, `LHE::n`, `LHE::logq`.

**Fake paths to avoid.** The bench uses `pir.FakeInit()` (`VSP/demo/bench/simplepir_bench.cpp:24`) and `pir.GenerateFakeHint()` (`:45`), and builds the DB with `packMatrixHardCoded(ell, m, p)` (`:15`). That overload fills a random packed matrix (`VSP/lib/pir/mat_packed.cpp:64-81`, `random_fast` at `:79`), so hint, DB and answer are unrelated. `random_fast` reads `/dev/random` with `fgets` (`VSP/lib/pir/mat.cpp:29-39`).

**Thread safety of `Answer`.** Both overloads are `const` methods taking `const` inputs. They return a freshly allocated `Matrix` (`VSP/lib/pir/preproc_pir.cpp:347-365`). `grep static|omp|thread` over `VSP/lib/pir/` finds no shared mutable state. The deeper kernels in `mat.cpp` and `mat_packed.cpp` were not audited line by line.

**Sizes.** `d` is **bits** per entry (`VSP/lib/pir/database.h:81-82`). `entry_t` is `BigUnsigned` (`database.h:7`), so any `d` is representable. `loadRandomData` seeds `mt19937_64` from `std::random_device` (`database.h:109`). It is unseeded, so a seeded DB needs `randomData=false` and the adapter fills `db.data` (public, `database.h:84`). Rejections are `assert(false)` (process abort, not an exception): `computeParams` at `VSP/lib/pir/database.cpp:430-440`, and the packed path when `ceil(log2 p) > BASIS=26` (`VSP/lib/pir/mat_packed.cpp:9`, `:39-43`). The Makefile adds no `-DNDEBUG` (grep), so asserts are live.

---

## S1 and S2: VeriSimplePIR (honest digest / full)

**Constructor.** `VeriSimplePIR(N, d, allowTrivial, verbose, simplePIR=false, randomData, batchSize=1, preproc=true, honestHint)` (`VSP/lib/pir/preproc_pir.h:25-56`). **`honestHint` is read in exactly one place**, parameter selection: `computeParams` (`VSP/lib/pir/database.cpp:359-360`, `:407`, `:432`), through `check_slack_lwe_correctness`, which adds `1 + log2(ell)` to the bound when `!honest_hint` (`:153-154`). No `VeriSimplePIR` method branches on it (grep `honest` over `VSP/lib`). In code, S1 and S2 run the same call sequence with different `p`, `m`, `ell`.

**Offline.** Reference: `full_preproc_pir_test` (`VSP/demo/test/pir_test.cpp:246-341`, `honestHint=false` at `:256`):
1. `D = packDataInMatrix`, `D_T = transpose(D)` (`:261-262`).
2. `A = pir.Init()` and `H = pir.GenerateHint(A, D)` (`:269-270`).
3. `A_2 = pir.PreprocInit()`: `preproc_lhe.genPublicA(ell)` (`:274`; `VSP/lib/pir/preproc_pir.cpp:8-10`).
4. `H_2 = pir.PreprocGenerateHint(A_2, D_T)`: `m x n` multi-limb (`:275`; `preproc_pir.cpp:19-27`).
5. `pir.HashAandH(preproc_hash, A_2, H_2)`: SHA-256 (`:277`; `preproc_pir.cpp:154-162`).
6. `C = pir.PreprocSampleC()`: `40 x ell` binary (`:279`; `preproc_pir.cpp:37-42`).
7. Test-only shortcut: `correct_Z = matMulLeftBinary(C, D)`, then `VerifyPreprocZ(correct_Z, ...)` (`:281-283`). This is not protocol.
8. Client: `pir.PreprocClientMessage(A_2, C)` returns 40 ciphertexts plus 40 secret keys (`:295`; `preproc_pir.cpp:45-73`).
9. Server: `pir.PreprocAnswer(cts, D_T)` (`:300`; `preproc_pir.cpp:97-114`).
10. Server: `pir.PreprocProve(hash, cts, res_cts, D_T)` returns the proof matrix (`:302`; `preproc_pir.cpp:191-201`).
11. Client: `pir.PreprocVerify(A_2, H_2, hash, cts, res_cts, proof)` (`:304`; `preproc_pir.cpp:210-248`).
12. Client: `Z = pir.PreprocRecoverZ(H_2, sks, res_cts)` (`:306`; `preproc_pir.cpp:252-270`).
13. Client: `pir.VerifyPreprocZ(Z, A, C, H)` (`:315`; `preproc_pir.cpp:272-295`).

Spec fields: `server_preproc_proof_s` covers steps 9-10, and `client_offline_s` covers steps 8 and 11-13.

**Online.** `Query(A, index)`, then `Answer(ct, D)`, then `pir.PreVerify(ct, ans, Z, C)`, then `Recover(H, ans, sk, index)` (`VSP/demo/test/pir_test.cpp:322-331`). `PreVerify` checks `Z*u == C*v` (`VSP/lib/pir/preproc_pir.cpp:367-377`). **Verification is a separate call before `Recover`**, so `verify_fused=false`. Every check (`PreVerify`, `PreprocVerify`, `VerifyPreprocZ`) reports failure by `std::cout` and then `assert(false)` (`preproc_pir.cpp:219-223`, `:231-235`, `:241-245`, `:283-293`, `:370-374`). None returns a value, so a detected fault aborts the process.

**Messages.** No serialiser. `Matrix` costs 8 B per element. `Multi_Limb_Matrix` holds two `Matrix` of equal shape (`q_data`, `kappa_data`), 16 B per element (`VSP/lib/pir/multilimb_lhe.h:12-20`). `BinaryMatrix` stores `bool*`, 1 B per element in memory (`VSP/lib/pir/mat.h:91-94`). Shapes come from the code above: `A` `m x n`, `H` `ell x n`, `A_2` `ell x n` multi-limb, `H_2` `m x n` multi-limb, each preproc ciphertext `ell x 1` multi-limb (x40), each response `m x 1` multi-limb (x40), proof `40 x ell`, `Z` `40 x m`, `C` `40 x ell`, online `ct` `m x 1`, `ans` `ell x 1`. Directions are not stated in code. The analytical accounting is `VSP/demo/scripts/params.cpp:49-59`: hint download = `H` + "additional hint" (`H_2`) + `Z_t`; preproc upload = ciphertexts; preproc download = responses + proof. Set `preproc=true` and `honesthint` by hand (`:31-32`). The `kappa` formula in `params.cpp:50` (`ell * ceil(sqrt(ell/m))`) differs from the constructor's (`ceil(sqrt(m*ell))`, made odd, `VSP/lib/pir/preproc_pir.h:43-44`).

**Parameters.** As S0-cpp, plus `STAT_SEC_PARAM 40` (`VSP/lib/pir/mat_packed.h:6`), `kappa` and `preproc_lhe = Multi_Limb_LHE(p*ell, kappa)` (`VSP/lib/pir/preproc_pir.h:43-48`), and the `honestHint` flag. With `preproc=true`, `computeParams` uses the SimplePIR shape (`database.cpp:383`) and applies **both** slack-LWE and SIS checks (`:406-408`, `:431-433`). S0-cpp applies only slack-LWE with `honest_hint=true` (`:410`, `:435`). The S1/S0-cpp and S2/S0-cpp ratios can therefore compare different `p`, `m`, `ell`.

**Fake paths to avoid.** `preproc_pir_bench.cpp` uses random `D_T` (`:9-10`) and `PreprocFakeClientMessage` (`:22`). `PreprocFakeInit`, `PreprocGenerateFakeHint`, `FakeInit` and `GenerateFakeHint` appear at `:55-59`. `PreprocFakeAnswer`, `PreprocFakeProve` and `fake=true` appear at `:81-87`. Online, it uses random packed D (`:117`), fake `A`/`H` (`:122-124`), `SampleFakeCandZ` (`:127`) and `FakePreVerify` (`:157`). It also leaves `preproc_hash` uninitialised (`HashAandH` commented out, `:15-16`, `:61-62`). `PreprocFakeComputeAnswer` (`preproc_pir.cpp:118-139`) is for "database is too big for benchmarking machine". Every `fake=true` argument suppresses the assert.

**Thread safety.** Same as S0-cpp for `Answer`. `PreprocAnswer` and `PreprocProve` are `const` and allocate their results (`preproc_pir.cpp:97-114`, `:191-201`). Each `random()` call builds a fresh `PRNG(osuCrypto::sysRandomSeed())` (`VSP/lib/pir/mat.cpp:41-49`). Each `GaussSample` builds a fresh `mt19937_64(random_device)` (`VSP/lib/pir/gauss.cpp:31-32`). Thread safety of `sysRandomSeed` is not determined.

**Sizes.** `d` is bits. Rejections: `checkSISHardness` asserts in the constructor (`VSP/lib/pir/preproc_pir.h:58-65`, called at `:50-51`). `computeParams` asserts at `VSP/lib/pir/database.cpp:430-440`. `assert(ell > 0)` and related checks sit in `get_balanced_m_and_ell` (`:84-90`), which is not on the preproc path.

---

## S3: apir-code single-server LWE (`cmp-vpir-lwe`, `cmp-vpir-lwe-128`)

**Which client.** `simulations/simul.go` maps `cmp-vpir-lwe` to `client.NewAmplify` / `server.NewAmplify` with `tECC` repetitions (`AP/simulations/simul.go:158-164`, `:252-254`). It does **not** use `client.NewLWE`. `cmp-vpir-lwe-128` maps to `NewLWE128` (`:165-167`, `:209-211`). `NewAmplify` runs `2*tECC+1` independent `LWE` clients and decodes the majority (`AP/lib/client/amplify.go:18-30`, `:48-61`). `tECC` is defined only for 2^13, 2^23 and 2^33 bits (`AP/simulations/simul.go:102-106`). Any other size panics (`:160-163`). `TestAmplify` uses `threshold := 8` (`AP/amplify_test.go:17`, `:26`).

**Offline.** `db := database.CreateRandomBinaryLWEWithLength(rnd, dbLen)` (`AP/lib/database/lwe.go:27-30`). It sets `db.Auth.DigestLWE = Digest(db, numRows)` = `A * DB` with `A` from the fixed public seed (`:18-25`, `:57-59`; `SeedMatrixA` at `AP/lib/utils/lwe.go:9`). Then `p := utils.ParamsWithDatabaseSize(rows, cols)` (`AP/lib/utils/lwe.go:34-41`). The client receives the digest inside `db.Info` (`NewLWE`, `AP/lib/client/lwe.go:31-37`). There is no client offline verification. Tests: `AP/lwe_test.go:16-42`, `AP/lwe128_test.go:16-25`, `AP/amplify_test.go:16-27`.

**Online.** `c.QueryBytes(index)` (`AP/lib/client/lwe.go:68-72`). `Query` regenerates `A` (`N x L`) from `SeedA` on **every** query (`:45`). Then `s.AnswerBytes(q)`, which computes `matrix.BinaryMul(q, db.Matrix)` (`AP/lib/server/lwe.go:20-29`). Then `c.ReconstructBytes(a)` (`AP/lib/client/lwe.go:93-95`). **Verification is fused** into `Reconstruct` (`:74-91`). Each of the `M` columns must decode to 0 or `t`, otherwise `errors.New("REJECT")` (`:86`). The function returns only `outs[c.state.j]`, one bit (`:90`), and mutates the answer in place (`:76`). `verify_fused=true`. LWE128 has the same structure (`AP/lib/client/lwe128.go:75-92`).

**Messages.** `MatrixToBytes`: 4-byte rows plus 4-byte cols (big-endian), then `uint32` data (`AP/lib/matrix/matrix.go` HEAD `:39-48`). `MatricesToBytes` (Amplify): 4-byte length plus concatenation (HEAD `:64-84`). `Matrix128ToBytes`: 8-byte header plus 16 B per element (`AP/lib/matrix/matrix128.go` HEAD `:31-44`). Upstream counts query and answer with `BytesSize()` = `rows*cols*4` (HEAD `:206-208`) or `*16` (128: HEAD `:185-187`), which excludes the header (`AP/simulations/simul.go:236-237`, `:277-278`). Digest size: `DigestLWE.BytesSize()` (`:261`).

**Parameters.** `ParamsDefault`: `P=2`, `N=1100`, `Sigma=6.4`, `BytesMod=4` (`AP/lib/utils/lwe.go:24-32`). `B = rows*12*ceil(sigma)` (`:65-68`). 128-bit: `N=4800`, `BytesMod=16` (`:48-54`). Record `params.{N,Sigma,L,M,B,BytesMod}` and `tECC`.

**Fake paths.** None found. `simul.go` builds the real digest. It times query+answer+reconstruct as one `Reconstruct` CPU figure (`:268-276`) and runs one query per repetition, `numRetrievedBlocks := 1` (`:249`), although `BitsToRetrieve = 8192` (`AP/simulations/simul.toml`).

**Thread safety of `Answer`.** `BinaryMul` allocates `out` and reads `db.Matrix` through C (`AP/lib/matrix/matrix.go` HEAD `:150-171`). No shared writes appear in the apir code. Client `NewGauss` uses `utils.MathRand()` over a package-global buffered PRG (`AP/lib/utils/rand.go:36-50`, `AP/lib/utils/gauss.go:30-31`).

**Sizes.** `dbLen` is **bits**. The DB is a square bit matrix, `CalculateNumRowsAndColumns(dbLen, true)` (`AP/lib/database/db.go:167-177`). The record size `d` is not a parameter. The API returns one bit per query, while the answer covers a whole row of `M` bits. There is no explicit rejection besides the `tECC` map.

---

## S4: apir-code single-server DDH (`cmp-vpir-dh`)

**Offline.** `database.CreateRandomEllipticWithDigest(rnd, dbLen, group.P256, true)` (`AP/lib/database/elliptic.go:20-73`). It stores 1 bit per byte (`:24-30`), computes per-row digests over `runtime.NumCPU()` goroutines (8 for DBs of 1 MiB or less) (`:31-34`), and hashes them with BLAKE2b-256 into a global digest (`:35`, `:56-58`). **Client offline verification:** `client.NewDH` re-hashes `SubDigests` and `panic`s on mismatch (`AP/lib/client/dh.go:23-35`). Test: `AP/dh_test.go:20-47`. `simul.go` skips 2^33 bits for DH (`AP/simulations/simul.go:116-119`).

**Online.** `c.QueryBytes(index)` returns `NumColumns` compressed group elements (`AP/lib/client/dh.go:39-70`). Then `s.AnswerBytes(q)` returns `NumRows` elements (`AP/lib/server/dh.go:19-58`). Then `c.ReconstructBytes(a)` (`AP/lib/client/dh.go:72-108`). **Verification is fused.** Every row is checked and `errors.New("reject")` is returned on any bad row (`:92-93`). Returns a `byte` bit. `verify_fused=true`.

**Messages.** Already `[]byte`. `MarshalGroupElements` concatenates `MarshalBinaryCompress()` outputs (`AP/lib/database/elliptic.go:109-119`). Element size is computed at runtime by `getGroupElementSize` (`:136-141`). Digest bytes: `len(SubDigests)+len(Digest)` (`AP/simulations/simul.go:301`).

**Parameters.** Group passed by the caller (`group.P256`, `simul.go:136`). Hash `crypto.BLAKE2b_256` (`elliptic.go:35`). Record group name, hash, and `ElementSize`.

**Fake paths.** None found.

**Thread safety.** `AnswerBytes` hard-codes `NGoRoutines := 1` (`AP/lib/server/dh.go:25`). `processRows` reads `db.Entries` and `input`, and writes only its local `prods` (`:60-74`). Whether circl `Element.Add` with a shared `input[j]` argument mutates that argument is not determined (not read).

**Sizes.** `dbLen` bits, square layout (`elliptic.go:21`), so memory is 1 byte per DB bit. One bit per query.

---

## M0: apir-code two-server point PIR (as coded)

**Offline.** `db := database.CreateRandomBytes(rnd, dbLen, numRows, blockLen)` (`AP/lib/database/bytes.go:49-70`). No digest. `c := client.NewPIR(rnd, &db.Info)` and `s0, s1 := server.NewPIRTwo(db)` (`AP/pir_test.go:39-41`).

**Online.** `queries := c.QueryBytes(be32(index), 2)` (`AP/lib/client/pir_point.go:38-41`), then `a0 := s0.AnswerBytes(queries[0])` and `a1 := s1.AnswerBytes(queries[1])`, then `c.ReconstructBytes([][]byte{a0, a1})` (`AP/pir_test.go:47-57`). `reconstructValuePIR` XORs `answers[k][ix*bs:(ix+1)*bs]` (`AP/lib/client/client.go:78-87`). **Verification:** none.

**Messages.** Queries and answers are `[]byte`, so `len()` is the wire size. Query = DPF key (`type DPFkey []byte`, `DPF/dpf.go:7`). Answer = `nRows*BlockSize` bytes (`AP/lib/server/point.go:65`).

**Parameters.** DPF domain `logN = bits.Len(NumColumns)` (`pir_point.go:58`, `point.go:61`). The DPF PRF keys are fixed constants (`DPF/dpf.go:22-35`). Record `numRows`, `numColumns`, `blockLen`, `logN`.

**Thread safety.** `Answer` builds a local `q` from `dpf.EvalFull` (allocates, `DPF/dpf.go:243-262`) and a fresh `out`, and only reads `s.db` (`point.go:55-79`). dpf globals `keyL`/`keyR` are written only in `init()` (`DPF/dpf.go:17-18`, `:34-35`). The non-amd64 helpers from the in-progress patch are not determined. **`cores` is stored by `NewPIR` but never read by `Answer`** (`point.go:21`, `:27-36`, `:55-79`), so there is no native intra-query parallelism in this server.

**Sizes.** `dbLen` is **bits** (`entries := make([]byte, dbLen/8)`, `bytes.go:51`; tests use `oneMB = 1024*1024*8`, `AP/complex_test.go:17-19`). `blockLen` is bytes. `numColumns = dbLen/(8*numRows*blockLen)`, an integer division (`bytes.go:56`). `dpf.Gen` panics if `logN > 63` (`DPF/dpf.go:72-74`).

## M1: apir-code two-server Merkle PIR

**Offline.** `db := database.CreateRandomMerkle(rnd, dbLen, numRows, blockLen)` (`AP/lib/database/merkle.go:14-66`). It builds the tree with `merkle.New(blocks)` (`:30`), then one proof per block, stored after the block plus a `0x80` pad byte. The stored block length becomes `blockLen + proofLen + 1` (`:40-42`). It sets `PIRType: "merkle"` and `Merkle{Root, ProofLen}` (`:60-61`). `proofLen = ceil(log2(#blocks))*32 + 8` (`AP/lib/merkle/merkletree.go:76-78`). `numColumns = numBlocks/numRows`, integer division (`merkle.go:39`). The client keeps `Root` in `db.Info`. There is no client offline step.

**Online.** Same client and server types as M0 (`AP/merkle_test.go:52-83`). The server is unaware of Merkle (`AP/lib/server/point.go:14-17`). **Verification is inside `Reconstruct`.** `reconstructPIR` switches on `dbInfo.PIRType` (`AP/lib/client/client.go:49-76`): XOR-reconstruct, then `UnPadBlock`, then split off the proof, then `merkle.DecodeProof`, then `merkle.VerifyProof(data, proof, dbInfo.Root)` (`:54-64`). It returns `errors.New("REJECT!")` on mismatch (`:68-70`). Through the public API, verification is fused (`verify_fused=true`). The steps are sequential in one function, and `merkle.VerifyProof`, `DecodeProof` and `database.UnPadBlock` are exported (`AP/lib/merkle/proof.go:45`, `:80`; `AP/lib/database/pgp.go:174`). `DecodeProof` does not bounds-check `numHashes` (`proof.go:82-89`), so a corrupted count can panic with index out of range rather than return REJECT. `VerifyProof` never returns a non-nil error (`proof.go:55-61`), so the `log.Fatalf` at `client.go:65-67` is unreachable. Expected plaintext in the test: `db.Entries[i*bs:(i+1)*bs-ProofLen-1]` (`merkle_test.go:79`).

**Messages, parameters, thread safety.** Same as M0. Hash is BLAKE3 (`proof.go:46`).

**Sizes.** As M0, plus per-block proof overhead in server memory and answer size.

---

## M0: what the code sends (for the `[HUMAN]` question in spec section 4)

Facts only.

1. **Current client.** `client.NewPIR` (`AP/lib/client/pir_point.go:29-35`) returns a `PIR`. `Query(index, numServers)` with `numServers == 2` returns `dpf.Gen(uint64(iy), uint64(bits.Len(uint(NumColumns))))` keys `k0`, `k1` (`:57-60`). For any other `numServers` it returns XOR secret shares of a one-hot bit vector of `NumColumns/8 + 1` bytes from the unexported `secretShare` (`:62`, `:80-110`). There is no exported way to make the client emit XOR bit vectors for exactly 2 servers.
2. **Current server.** `server.NewPIRTwo` sets `numServers = 2` (`AP/lib/server/point.go:38-42`). `Answer` expands the key with `dpf.EvalFull` only in that case (`:59-62`). `server.NewPIR` leaves `numServers = 0` and treats `q` as a raw selection bit vector (`:27-36`, `:82-90`). Either way, the server XORs the selected blocks of each row (`:67-77`).
3. **Each of the two servers receives** one `DPFkey` (`[]byte`). After `EvalFull` it holds the full `NumColumns`-bit selection vector share. Both answers are `nRows*BlockSize` bytes.
4. **Tests.** `retrievePIRPoint` (`AP/pir_test.go:38-62`) and the 2-server `retrieveBlocksMerkle` (`AP/merkle_test.go:56-57`) use `NewPIRTwo`, so both M0 and M1 run on DPF queries at HEAD. `TestMerkleFourServers` uses `server.NewPIR` with 4 XOR shares (`merkle_test.go:37-50`, `:58-59`). `key_test.go:66-67` and `:90-91` also use `NewPIRTwo`.
5. **Simulation configs.** `simulations/pirClassicMulti.toml` and `pirMerkleMulti.toml` name primitives `pir-classic` and `pir-merkle`. `simulations/simul.go` does not accept them (`validSimulation`, `AP/simulations/simul.go:382-386`). They run through `simulations/multi/`. Its server uses `NewPIRTwo` when `-servers == 2`, otherwise `NewPIR` (`AP/simulations/multi/server/server.go:127-133`). `simul.py` passes `servers=2` by default (`AP/simulations/multi/simul.py:101-102`). The client uses `client.NewPIR` and sends `len(connections)` queries (`AP/simulations/multi/client/client.go:142-145`, `:224`). `multi/pir_classic.toml` sets `NumRows = 1 # vector`, `BlockLength = 1024` (lines 3-4).
6. **History.** DPF was added in commit `9940e0a` "dpf + vector" (2024-10-16). It added both the `numServers == 2` DPF branch in `Query` and `NewPIRTwo`/`EvalFull` in `point.go` (`git show 9940e0a -- lib/client/pir_point.go lib/server/point.go`). Before it (`9940e0a^`), 2-server queries went through `secretShare`, and `multi/pir_classic.toml` had `NumRows = 0` (matrix).
7. **Logged results.** `AP/simulations/multi/final_results/client_pir_classic_8589935000.log:11` (dated 2022/08/04) reads `stats,0,258,0.162362`. Its server ran with `nRows` 0 (`server_0_pir_classic_8589935000.log:1`, `flags: ... 8 0 1024`). `AP/simulations/multi/final_results_fix/client_pir_classic_8589935000.log:12` (dated 2024/10/17) reads `stats,0,570,0.123505`, and its server log line 2 reads `using DPF with numServers`. Inferred, not run: 258 = 2 x (1024/8 + 1), the `secretShare` vector length for a 1024-column matrix layout. 570 is the summed DPF key bytes for the vector layout. Which of the two result sets the published paper reports cannot be read from the code.
8. **gRPC tool.** `AP/cmd/grpc/server/main.go:141-146` builds `server.NewPIR` (no DPF) for `pointPIR`/`pointVPIR`. Its client calls `client.NewPIR(...).QueryBytes(in, len(lc.connections))` (`AP/cmd/grpc/client/main.go:160`, `:245`). With 2 connections the client sends DPF keys to a server that expects bit vectors. Inferred, not run: that pairing would not answer correctly, which matters if Tier 2 reuses `cmd/grpc`.

---

## Open questions for P1

1. **M0 baseline.** At HEAD, M0 and M1 both send DPF keys for 2 servers. A Chor-style XOR bit-vector query for 2 servers exists only at `9940e0a^` or through `NewPIR` with an adapter-built share, and the latter is new query code. The humans decide whether M0 is DPF (as coded), pre-DPF (older pin), or both.
2. **S1 vs S2.** `honestHint` changes only parameters. Which offline steps S1 skips under the honest-digest assumption (e.g. `PreprocVerify`, or the whole `Preproc*` exchange) is not stated in code.
3. **S3 primitive.** `cmp-vpir-lwe` runs `Amplify` (`2*tECC+1` LWE instances). The spec names `client.NewLWE`. `tECC` is defined only for three sizes. The humans must confirm which client S3 uses, and the `tECC` value for other DB sizes.
4. **Record size on 1-bit schemes.** S3 and S4 return one bit per query through the API, although each answer covers a full row (S3) or column (S4). How a `d`-byte record maps to queries (8*d queries, or one query plus non-public row decoding) is undecided. The spec's `record_bytes` axis does not map directly.
5. **S0-go records over 64 bits.** `Recover` returns `uint64`. Options: one query plus several `Recover` calls for indices in the same column (`i`, `i+M`, ...; column = `i % M`, `SP/simple_pir.go:199`, `:205`), or limit S0-go to `d <= 64` bits.
6. **Aborts as rejections.** VeriSimplePIR verification failures and parameter rejections are `assert(false)`. Fault-injection and `unsupported_params` cells need a subprocess boundary (or `SIGABRT` handling) to record `detected=true` or `status=unsupported_params`.
7. **Offline byte accounting.** Upstream SimplePIR (Go and C++) counts `H` only, not `A` or the seed. Decide whether `offline_down_bytes` includes `A` (uncompressed in C++) or the seed (Go `RunPIRCompressed`), and whether to use the compressed Go path given its global-PRG reseed (`SP/simple_pir.go:110`, `:115`).
8. **Single-threaded offline.** `CreateRandomEllipticWithDigest` uses `runtime.NumCPU()` goroutines (`AP/lib/database/elliptic.go:31-34`). Spec section 7 requires single-threaded runs, so `GOMAXPROCS=1` or an equivalent must be set.
9. **Merkle memory.** Stored blocks grow by `proofLen+1`. Inferred: at 32 B records and 1 GiB, `proofLen = 25*32+8 = 808` B, so the stored DB is about 26x the data and an `oom` is likely.
10. **Separating M1 verify time.** An adapter could reconstruct as `classical` and then call the exported `UnPadBlock`/`DecodeProof`/`VerifyProof` itself to split `client_recover_s` from `client_verify_s`. Whether that counts as "adapter only" under rule 1 or should stay `verify_fused=true` is a human call.
11. **VeriSimplePIR `kappa`.** `params.cpp` and the constructor compute `kappa` differently. The analytical-vs-measured byte comparison (spec section 6, 1 % flag) may disagree for multi-limb messages.
