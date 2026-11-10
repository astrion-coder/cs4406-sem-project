# VeriSimplePIR adapter (`vsp_cpp`)

`build/adapters/vsp-adapter` drives `third_party/verisimplepir` for arms S0-cpp, S1 and S2. It follows the contract in `../README.md`, and this file covers only what the contract leaves to each adapter.

## Build

```sh
make -C src/adapters/vsp_cpp
```

This needs the already-built `third_party/verisimplepir/bin/lib/libverisimplepir.dylib` and does not rebuild it. It compiles with Homebrew LLVM `clang++ -std=c++17 -O3` and the same include and link flags as upstream's demo binaries, so the adapter shares the library's libc++ ABI. It never defines `NDEBUG`, because upstream's header asserts must stay live. It embeds `git -C third_party/verisimplepir rev-parse HEAD` as `upstream_sha`, and rebuilds when that commit changes.

The dylib's install name is the relative path `bin/lib/libverisimplepir.dylib`, which dyld resolves against the working directory. The build rewrites this one reference in the adapter binary to `@rpath/libverisimplepir.dylib`, with the rpath `@executable_path/../../third_party/verisimplepir/bin/lib`. The binary then runs from any directory with no `DYLD_LIBRARY_PATH`. `third_party/` is not touched. `make -C src/adapters/vsp_cpp OUT=<path>` builds elsewhere, and the rpath resolves when `<path>` is two levels below the repository root, as `build/adapters/vsp-adapter` is.

## Arms

| Arm | `VeriSimplePIR` flags | Calls |
|---|---|---|
| S0-cpp | `simplePIR=true, preproc=false` | `Init`, `GenerateHint`; per query `Query`, `Answer`, then `Recover`'s body per entry (`decode_record`) |
| S1 | `preproc=true, honestHint=true` | the S2 sequence |
| S2 | `preproc=true, honestHint=false` | `Init`, `GenerateHint`, `PreprocInit`, `PreprocGenerateHint`, `HashAandH`, `PreprocSampleC`, `PreprocClientMessage`, `PreprocAnswer`, `PreprocProve`, `PreprocVerify`, `PreprocRecoverZ`, `VerifyPreprocZ`; per query `Query`, `Answer`, `PreVerify`, then `Recover`'s body per entry (`decode_record`) |

All arms use `allowTrivial=true`, `randomData=false` and `batchSize=1`, as `full_preproc_pir_test` (`src/demo/test/pir_test.cpp:246-341`) does. S1 and S2 make the same calls, and `honestHint` changes only parameter selection (decision 4). No `Fake*` function and no `fake=true` argument is ever used. The paper's one-time digest check `DigVer` is not in the library and is not written here (decision 5).

`Answer` uses the packed database (`packMatrixHardCoded`, 26-bit basis), which is the kernel upstream's own benchmarks time. `scheme_params.answer_kernel` records this.

## Sizes and data

A database entry is one byte (`d = 8`), and a record is `k = record_bytes` consecutive entries in one database column (decision 18). One `Query` for that column and one `Answer` retrieve the record, and the client decodes each of its `k` entries. `Layout` in `vsp_adapter.cpp` is the only place that maps records to the library's entry indices:

```
entry(r, j) = (r / records_per_column) * entries_per_column + (r % records_per_column) * k + j
```

`entries_per_column` is the count `PlaintextDBParams::indexToColumn` uses (`database.h:30-43`), and `records_per_column = entries_per_column / k`. The entries left at the bottom of each column, and any after the last record, are zero padding. `plan_layout` picks the library's `N`: the smallest value on a 1/1024 geometric grid, starting at `db_bytes`, for which `computeParams` gives a matrix whose columns hold every record whole. Before any data is packed, `check_layout` confirms with upstream's own `indexToColumn` that every record's first and last entry share a column.

`scheme_params` records `entry_bits`, `pieces_per_record` (`k`), `N` (the library's entries, padding included), `entries_per_column`, `records_per_column`, `columns_used` and `padding_factor` (`N / db_bytes`).

Upstream sizes the matrix near-square from `N` alone, so a record longer than a column forces a larger `N`. The padding factor is at most 1.02 for 32 B and 256 B records at every grid size except S2 at 1 MiB x 256 B (1.10). It is large where records are long and the database is small: at 1 MiB, 1.32 (S0-cpp, S1) and 1.97 (S2) for 1 KiB records, and 21.3 and 31.9 for 8 KiB records. Such a cell measures a database that many times larger than `db_bytes`.

The database is `db_bytes` bytes from `--seed` through an AES-CTR stream (upstream's `osuCrypto::PRNG`). Query indices and fault choices come from two further streams on the same seed. The ground truth for `correct` is the record's bytes.

`pack_database` writes each entry into the `ell x m` matrix over Z_p at upstream's `indexToRow` and `indexToColumn`, at the bits `PlaintextDBParams::recover` reads. This is `Database::packDataInMatrix`'s encoding, written from the bytes, because upstream's `Database` keeps one `BigUnsigned` per entry (about 32 bytes each, so 32 GiB at 1 GiB). For all six 1 MiB conformance cells the matrix is byte-identical to `packDataInMatrix` on the same entries. The database, its transpose for S1 and S2, and the packed copy for `Answer` are built once and never timed.

Decoding (`decode_record`) runs the body of `VeriSimplePIR::Recover` (`preproc_pir.cpp:392-404`) for every entry of the record: one `lhe.decrypt` of the column, then `dbParams.recover` per entry. `Recover` itself decrypts the whole column again for each entry, so calling it `k` times would make `client_recover_s` measure `k - 1` redundant decryptions. On the first online repetition the adapter checks that upstream's `Recover` returns the same byte as `decode_record` for the record's first and last entry, and exits 1 if it does not.

## What each timer covers

| Metric | Calls |
|---|---|
| `public_params_s` | `Init`, and `PreprocInit` for S1 and S2 |
| `server_preproc_s` | `GenerateHint`, and for S1 and S2 `PreprocGenerateHint` and the server's `HashAandH` |
| `server_preproc_proof_s` | `PreprocAnswer`, `PreprocProve` |
| `client_offline_s` | S1 and S2: the client's own `HashAandH`, `PreprocSampleC`, `PreprocClientMessage`, `PreprocVerify`, `PreprocRecoverZ`, `VerifyPreprocZ`. 0 for S0-cpp |
| `query_gen_s` | `Query` |
| `server_answer_s` | `Answer`, also when `drop_server` then withholds the answer |
| `client_verify_s` | `PreVerify` (S1 and S2), preceded by `VerifyPreprocZ` in a faulted online `flip_hint_bit` repetition. Null for S0-cpp |
| `client_recover_s` | `decode_record`: one `lhe.decrypt`, then `dbParams.recover` for each of the record's entries |
| `recovery_s` | see "Recovery" |

In a faulted repetition the client's checks run in a child (see "Isolation"). The child starts a clock in a shared page before the checks and stops it when they return, or in its `SIGABRT` handler when upstream rejects. So `client_verify_s` and `client_offline_s` cover the checks up to the rejection, without the fork.

The client hashes `A_2` and `H_2` itself, because it cannot take the Fiat-Shamir hash from the server.

## Encoding and bytes

The library has no serialiser. As the contract's "Encoding bytes" section requires, a message is its elements, each in the bits its modulus needs, packed tightly and rounded up to whole bytes once per message (`wire_bytes`). `scheme_params` records the widths:

| Elements | Modulus | Bits (`wire_bits_*`) |
|---|---|---|
| `A`, `H`, `ct`, `ans` | q = 2^64 | 64 |
| `A_2`, `H_2`, preprocessing ciphertexts and responses | q * kappa | 64 + bits(kappa - 1) |
| preprocessing proof | values below p * m (`PreprocVerify`'s norm bound) | bits(p * m - 1) |

| Metric | S0-cpp | S1, S2 |
|---|---|---|
| `offline_down_bytes` | `A`, `H` | `A`, `H`, `A_2`, `H_2`, 40 preprocessing responses, proof |
| `offline_down_bytes_hint_only` | `H` | `H`, `H_2`, proof (params.cpp's "hint download") |
| `offline_up_bytes` | 0 | 40 preprocessing ciphertexts |
| `up_bytes`, `down_bytes` | `ct`, `ans` | `ct`, `ans` |

`client_storage_bytes` is the memory the client keeps between queries, in the library's own arrays: `A` and `H` at 8 bytes per element, and for S1 and S2 also `Z` (8 bytes per element) and `C` (one `bool` each).

`analytical` reproduces `src/demo/scripts/params.cpp:42-74` with its integer truncation. It carries `up_bytes`, `down_bytes` and `offline_down_bytes_hint_only` in every record, plus params.cpp's `offline_up_bytes`, `offline_preproc_down_bytes` and `client_storage_bytes`. `offline_down_bytes` is null because params.cpp counts no public matrix.

Online sizes match params.cpp exactly. The S1 and S2 offline sizes come within about 1.5%, for two reasons:

- params.cpp:50 uses `kappa = ell * ceil(sqrt(ell/m))`, and the constructor (`preproc_pir.h:43-44`) uses `ceil(sqrt(m*ell))`, made odd. Since `ell >= m`, params.cpp's kappa is about `2 ell` and the running one about `ell`, so params.cpp counts about one bit more per multi-limb element. It also counts `log2(kappa)` as a fraction, where the wire needs whole bits. `scheme_params` records both kappas.
- params.cpp counts proof elements as `log2(p*m)` bits, and the wire needs `ceil(log2(p*m))`.

`client_storage_bytes` is about twice params.cpp's figure, because params.cpp counts `H` only and the client also keeps `A` (the same size as `H` when `m` is close to `ell`), `Z` and `C`.

## Faults

Decoding computes `pt = round((ans - H sk) / Delta)`, where `Delta = q/p` is a power of two (`lhe.h:16-21`). Each flip targets one random entry of the record. That entry occupies some rows of the answer, and some digit bits `[lo, hi)` of each of those Z_p elements (`PlaintextDBParams::indexToRow` and `::recover`). Adding `2^j Delta` to `ans - H sk` changes digit bit `j` and nothing below it, with no rounding involved. So each flip below is chosen to change a record digit bit, and the honest client's output always changes.

- `flip_answer_bit`. In a row of the chosen entry, the adapter flips answer bit `log2(Delta) + j` for a random record digit bit `j`. That bit is at or above `log2(q/p)`.
- `flip_hint_bit`, online. `H[row][col]` enters decoding multiplied by `sk[col] = 2^t * odd`. In a row of the chosen entry and a random column, the adapter flips hint bit `log2(Delta) + j - t` for a random record digit bit `j >= t`. That bit is also at or above `log2(q/p)`, and the record's digit moves by `2^j * odd`. The corrupted hint is used for that repetition only, and is then restored.
- `flip_hint_bit`, offline. No query exists yet, so the adapter flips a random bit at or above `log2(Delta)` in any element of `H`.
- `wrong_record`. The adapter replaces the answer with the honest `Answer` to a fresh `Query` for a different random record.
- `stale_record`. The stale server is a second packed database: `pack_database` over the same `Layout`, from the database bytes with every bit flipped (the padding stays zero), packed with `packMatrixHardCoded`. It is built once per run, untimed, and only for this fault. It answers the client's own query with upstream's `Answer`. The client's hint still matches the current database, so decoding yields noise rather than the flipped record. As for `wrong_record`, `server_answer_s` times the honest server's `Answer`, which is the same kernel at the same size.
- `drop_server`. These arms have one server, so it is the one that withholds its answer. The server computes the answer with `Answer` and never sends it. The record has `server_answer_s` timed, `down_bytes` 0 and `correct` null, and the client counts the missing answer as a rejection.
- `bad_proof` (S1 and S2, offline). The proof `PreprocProve` returns is a 40 x ell matrix whose entries lie below `PreprocVerify`'s norm bound `p*m`. The adapter flips bit `b` of a random entry, for a random `b` below `bits(p*m - 1)` (`wire_bits_proof`). The entry then either reaches `p*m` or more, which the norm check rejects, or moves by `2^b`. That adds `2^b` times a row of the random `A_2` to a row of `Z A_2`, which `PreprocVerify` compares with `C H_2` modulo `q*kappa`.

On the first faulted repetition of `flip_answer_bit` or `flip_hint_bit`, the adapter decodes the faulted answer with `decode_record` and exits 1 if the record did not change.

How each arm reports a fault:

- S0-cpp has no checks. An online fault shows as `correct=false` with `detected=null`, except `drop_server`, where nothing is decoded and `correct=null`. An offline `flip_hint_bit` record shows only `faulted=true`, because no client check exists for it to fail. S0-cpp does not take `bad_proof`, because it has no proof.
- S1 and S2, online answer faults (`flip_answer_bit`, `wrong_record`, `stale_record`). `PreVerify` runs in a forked child.
- S1 and S2, online `flip_hint_bit`. The child runs `VerifyPreprocZ` on the corrupted hint, because a client checks every hint it accepts against `Z`, and then `PreVerify`.
- S1 and S2, `drop_server`. Nothing is verified. `detected=true`, because the missing answer is a rejection.
- S1 and S2, offline `flip_hint_bit` and `bad_proof`. The child runs `PreprocVerify`, `PreprocRecoverZ` and `VerifyPreprocZ` with the corrupted hint or proof.

When the child aborts, `detected=true` and `correct=null`, because nothing was recovered. When it exits cleanly, `detected=false`, and the parent runs `decode_record` to set `correct`. Honest repetitions never fork.

## Recovery

S0-cpp never rejects an answer, so it takes only `--recovery abort`, and its outcome is `accepted` or `silent_wrong`, or `aborted` for `drop_server`. S1 and S2 take `abort`, `switch_replica` and `re_preprocess`. After a rejected first answer, `run_recovery` makes at most 3 further attempts for the same record and stops at the first one the client accepts. `correct` and `detected` still describe the first answer.

- `switch_replica`. A replica holds the same database, and upstream's `Answer` is a deterministic function of the database and the query, so the run's honest server objects serve it. Each attempt is a fresh `Query` from the client's existing state. Without `--replicas-cheat` the replica is honest, so the first attempt recovers. With it, each attempt is faulted with probability `--fault-prob`, drawn from the fault stream, in the same way as the first answer: another bit flip, another wrong record, the stale database, or no answer. `--replicas-cheat` has no effect under the other policies. An attempt runs in this process, and only a faulted attempt's checks fork.
- `re_preprocess`. Each attempt runs in a forked child: the whole offline phase (`run_offline`, the calls an offline record times) against the same server objects, then a fresh query from the new client state. The offline phase is honest, because every fault with a recovery is an online fault. The query is faulted with probability `--fault-prob`. The child hands its offline metrics and its query result back through a `MAP_SHARED` page and exits, so the library's leak of tens of MiB per offline phase dies with it. The new client state dies with it too, and the next repetition starts from the run's one offline state. The parent draws whether the attempt is faulted and a seed for the child's own fault choices, so its fault stream advances the same way on every run.

In an online `flip_hint_bit` run, a faulted attempt corrupts the hint for that attempt, as a faulted first answer does, and an honest replica's attempt uses the honest hint. A `switch_replica` attempt counts no hint download, as the first answer counts none.

| Metric | What it sums over the further attempts |
|---|---|
| `recovery_s`, `recovery_cpu_s` | each attempt's `Query`, `Answer`, checks and `decode_record` spans, plus for `re_preprocess` the offline phase's `public_params`, `server_preproc`, `server_preproc_proof` and `client_offline` spans. Forking, waiting and bookkeeping are not counted. |
| `recovery_up_bytes` | each attempt's `ct`, plus for `re_preprocess` the offline phase's `offline_up_bytes` |
| `recovery_down_bytes` | each attempt's `ans`, 0 when it was withheld, plus for `re_preprocess` the offline phase's `offline_down_bytes`. Under `drop_server` with `switch_replica --replicas-cheat`, every attempt is withheld, so this is 0 |

## Isolation

Upstream reports a bad parameter set or a failed check with `assert(false)`, which aborts the process. `run_isolated` forks. The child sends its stdout and stderr into a pipe and turns `SIGABRT` into `_exit(134)`, which also keeps macOS from writing a crash report. The parent reads the pipe and classifies the exit. `run_isolated_value` also hands a value back through a `MAP_SHARED` mapping. Four kinds of step run in a child:

1. Layout and parameter selection: `plan_layout`, the constructor (`computeParams`, `checkSISHardness`), `check_layout`, and the packed kernel's width check (`packMatrixHardCoded`, at most 26 bits). A rejection here writes every requested record with `status="unsupported_params"` and the message as `error`.
2. Every timed offline repetition (`run_offline_isolated`). Upstream's `Matrix` has no destructor (`mat.h`, commented out), and the multi-limb `matMul`, `matMulVec` and `matMulLeftBinary` allocate both result limbs and then overwrite them (`multilimb_lhe.cpp:14-17`). One S1 offline phase at 1 MiB therefore leaks about 33 MiB, which would reach gigabytes over 100 repetitions in one process. The child does the timed calls, returns the record's fields and its own peak RSS, and exits, so the leak dies with it. The fault bit for a faulted repetition is drawn in the parent, so the fault stream stays deterministic. The online phase's untimed setup runs once in the parent.
3. Every fault-injected verification (`verify_isolated`), which times itself (see "What each timer covers").
4. Every `re_preprocess` attempt, for the same leak as item 2 (see "Recovery").

## Known limits

- **Padding at small databases with long records.** See "Sizes and data". `padding_factor` in `scheme_params` says how much larger the library's database is than `db_bytes`.
- **Online repetitions leak a little.** They run in the parent, as the contract has them, and so do `switch_replica` attempts. The library's temporaries in `Query`, `Answer`, `decrypt` and `PreVerify` are never freed, and peak RSS grows by about 44 KiB per S1 online repetition at 1 MiB. These temporaries are vectors of length `m` or `ell`, so the growth scales with the square root of the database size.
