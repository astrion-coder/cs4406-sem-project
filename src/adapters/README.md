# Adapter contract

Each adapter is a command-line program that drives one upstream library and writes one JSON object per repetition and phase. The harness (`src/cs4406_sem_project/harness/run_grid.py`) only ever calls this command line. Spec §9 is the source. This file fixes the details §9 leaves open, and `harness/schema.json` validates exactly these records.

## Adapters

| Directory | Binary | Arms | Upstream |
|---|---|---|---|
| `simplepir_go/` | `build/adapters/simplepir-adapter` | S0-go | `third_party/simplepir` |
| `apir_go/` | `build/adapters/apir-adapter` | S3, S4, M0, M1 | `third_party/apir-code` (GPL-3.0, so this directory is GPL-3.0) |
| `vsp_cpp/` | `build/adapters/vsp-adapter` | S0-cpp, S1, S2 | `third_party/verisimplepir` |

`make build` builds all three into `build/adapters/` (gitignored). The build embeds the upstream submodule's commit in the binary, and the adapter writes it as `upstream_sha`.

## Command line

```
<adapter> --arm ARM --db-bytes N --record-bytes N --phase offline|online|all
          --reps N --warmup N --seed N --fault KIND [--fault-prob F]
          [--recovery POLICY] [--replicas-cheat] --out PATH.jsonl
```

- `--arm` must be one the adapter serves. Any other value is a usage error.
- `--db-bytes` is the total database size in bytes. Each adapter converts to the library's own units (bits, entries, rows and columns) in one function.
- `--record-bytes` is the record size in bytes. S3 and S4 retrieve one bit per query, so they require `--record-bytes 0`, and any other value is a usage error (decision 10 in `plan.md`).
- `--phase offline` runs `--warmup` plus `--reps` offline phases. Each one rebuilds the server's setup from the same database.
- `--phase online` runs the offline phase once, without timing or recording it, then `--warmup` plus `--reps` queries. Each query uses a fresh random index.
- `--phase all` records both phases.
- `--seed` determines the database and the query indices. The same seed gives the same database and the same indices on every run. Database generation is never timed.
- `--fault` is one of the kinds in the fault table below. `--fault-prob` (default 1.0 when `--fault` is not `none`) is the chance that each repetition is faulted, drawn from the seeded generator.
- `--recovery` is `abort`, `switch_replica` or `re_preprocess` (see "Recovery"). `retry_same` is never allowed (decision 12).
- `--replicas-cheat` makes every replica that `switch_replica` turns to faulty in the same way as the first server. Without it, replicas are honest (spec §6.1).
- `--offline-in-process` (vsp-adapter only, decision 27) runs the offline repetitions in the adapter's own process instead of a forked child. It takes `--fault none` only.
- Bench mode is single-threaded. Go adapters call `runtime.GOMAXPROCS(1)`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Every record was written. Faulty, rejected and `unsupported_params` repetitions are results, not errors. |
| 1 | Internal failure after argument parsing. The harness records `crash`. |
| 2 | Usage error: an unknown arm, a bad flag, a fault the arm does not support, or a nonzero `--record-bytes` for S3 or S4. |

Records go only to `--out`, one JSON object per line, appended. Progress messages go to stderr.

## Record

One object per repetition and phase, warm-ups included. The harness supplies three fields through environment variables, so the adapter command line stays as spec §9 defines it.

| Field | Type | Source |
|---|---|---|
| `run_id` | string or null | `VPIR_RUN_ID` (null when unset) |
| `git_sha_harness` | string or null | `VPIR_GIT_SHA` (null when unset) |
| `platform` | string or null | `VPIR_PLATFORM` (null when unset). The harness sets it from `env/reference_platform.txt`. |
| `upstream_sha` | string | embedded at build time |
| `adapter` | string | `simplepir_go`, `apir_go` or `vsp_cpp` |
| `arm` | string | `S0-go`, `S0-cpp`, `S1`, `S2`, `S3`, `S4`, `M0` or `M1` |
| `scheme_params` | object | parameters the library actually chose (for example `n`, `logq`, `p`, `ell`, `m`, `honest_hint`, `tECC`, curve) |
| `db_bytes` | integer | `--db-bytes` |
| `record_bytes` | integer or null | `--record-bytes`, or null for S3 and S4 |
| `retrieved_bits` | integer | bits returned per query (1 for S3 and S4, otherwise `8 × record_bytes`) |
| `n_records` | integer | number of records, or of bits for S3 and S4 |
| `phase` | string | `offline` or `online` |
| `rep` | integer | 0-based. Warm-ups count first. |
| `is_warmup` | boolean | |
| `seed` | integer | |
| `fault` | string | `--fault` |
| `fault_prob` | number | |
| `faulted` | boolean | whether this repetition was actually faulted |
| `recovery` | string | `--recovery` |
| `status` | string | `ok` or `unsupported_params`. The harness writes `oom`, `timeout` and `crash`. |
| `metrics` | object | see below |
| `analytical` | object or null | upstream's own size formulas. It always has the keys `up_bytes`, `down_bytes` and `offline_down_bytes_hint_only`, each null where upstream has no formula. It may carry more keys when upstream gives more formulas. |
| `correct` | boolean or null | the recovered record equals the ground truth (online only) |
| `detected` | boolean or null | verifiable arms: true when the client rejected, false when it accepted. Baselines: always null. |
| `error` | string or null | the library's message for `unsupported_params`, otherwise null |

### Metrics

Every record carries every metric key from both phases, with null for the other phase's metrics. `analytical` likewise carries all three keys. Times are seconds. Every wall-clock time `X_s` has a sibling `X_cpu_s` with the process CPU time over the same span. Timers wrap only the scheme call, not serialisation or database generation. Bytes are `len()` of the serialised message, as each adapter's README defines its encoding. A metric that does not apply to an arm is `null`, never omitted.

| Phase | Metric | Meaning |
|---|---|---|
| offline | `public_params_s` | sampling the public parameters (for example the matrix `A`), which does not depend on the database |
| offline | `server_preproc_s` | server builds its hint, digest or Merkle tree |
| offline | `server_preproc_proof_s` | S1 and S2 only: `PreprocAnswer` plus `PreprocProve` |
| offline | `client_offline_s` | client offline work, 0 when there is none |
| offline | `offline_down_bytes` | everything the client downloads offline, including the public matrix or its seed |
| offline | `offline_down_bytes_hint_only` | the hint or digest alone, which is what the papers count (decision 13) |
| offline | `offline_up_bytes` | |
| offline | `client_storage_bytes` | persistent client state between queries |
| online | `query_gen_s` | |
| online | `up_bytes`, `up_bytes_per_server` | total, and a list with one entry per server |
| online | `server_answer_s` | a list with one entry per server |
| online | `down_bytes`, `down_bytes_per_server` | |
| online | `client_recover_s` | reconstruction, excluding verification where upstream separates them |
| online | `client_verify_s` | null when `verify_fused` is true |
| online | `verify_fused` | true for M1, S3 and S4, where upstream verifies inside reconstruction (decision 14). False for every other arm. |
| both | `process_peak_rss_bytes` | the adapter process's peak resident memory at the end of the phase (`getrusage`, bytes on macOS) |

Both parties run in one process in bench mode, so per-party peak memory (`server_peak_rss_bytes`, `client_peak_rss_bytes` in spec §6) is measured only in serve mode (P8).

## Faults

Faults are injected only here, in the adapter, by changing the serialised server message before the client parses it. Never by editing library code.

| Fault | Phase | Arms | What the adapter does |
|---|---|---|---|
| `none` | both | all | |
| `flip_answer_bit` | online | all | flips one random bit, chosen among the answer bits that the honest client's output for the queried record depends on (see below) |
| `wrong_record` | online | all | replaces the answer with the honest answer to a different index |
| `flip_hint_bit` | offline and online | S0-go, S0-cpp, S1, S2 | flips one random bit of the hint the client downloads, chosen among the bits the queried record's decoding depends on. In an offline record, S1 and S2 report whether preprocessing verification rejected it. In an online record, the corrupted hint is used for that repetition's reconstruction, which is where a baseline shows the effect. |
| `stale_record` | online | all | answers from a stale database, the current one with every bit of every record flipped, so every record differs, including one-bit records. The client's offline state still matches the current database. The adapter builds the stale server once, untimed. |
| `drop_server` | online | all | withholds the answer of one server, chosen from the fault stream. That server still computes the answer, and its `server_answer_s` is recorded. The answer is never sent, so it adds 0 to `down_bytes`, to its entry in `down_bytes_per_server` and to `recovery_down_bytes`. The client reconstructs nothing from that query, so `correct` is null. |
| `bad_proof` | offline | S1, S2 | flips one bit of the preprocessing proof (`PreprocProve`'s output), below the proof's norm bound, so `PreprocVerify` sees a changed value. Spec §5.1 lists it for S2. S1 makes the same preprocessing calls (decision 4), so it applies there too. |

A uniformly random bit usually cannot change the answer. It may fall outside the rows that hold the record, or below the rounding threshold of a lattice ciphertext, where it disappears in decoding. Such a fault demonstrates nothing, for a baseline or a verifiable arm, so the adapter picks a bit that matters:

- For lattice schemes, the bit is in an element the record's decoding reads, at or above `log2(q/p)`.
- For XOR and Merkle schemes, it is any bit of the record's block.
- For DDH, it is any bit of the answer.

Each adapter's README states its rule. An honest client's output must change when the bit flips, and the adapter checks this on its own first faulted repetition.

For `flip_hint_bit`:

- A baseline arm's offline record can only show `faulted=true`, because a baseline has no check that could fail.
- For a verifiable arm, the client checks every hint it accepts. So in S1 and S2 an online record with a corrupted hint first runs the hint check (`VerifyPreprocZ`) and reports its verdict as `detected`.

For a baseline arm (S0-go, S0-cpp, M0), a fault shows up as `correct=false` with `detected=null`. That is the threat verification removes. For a verifiable arm, `detected=true` means the client rejected the answer.

VeriSimplePIR rejects answers and parameter sets with `assert`, which aborts the whole process. So the C++ adapter runs parameter selection and every fault-injected verification in a `fork()`ed child. A child that aborts means `unsupported_params` or `detected=true`, and the parent still writes the record.

## Recovery

What the client does after it rejects an answer (spec §6.1, decision 12). Every online record of a faulted run reports the outcome.

| `--recovery` | Arms | After a rejection |
|---|---|---|
| `abort` | all | stop. The record's outcome is `aborted`. |
| `switch_replica` | S1, S2, S3, S4, M1 | send a fresh query for the same index to a replica holding the same database: another single server, or another server pair. |
| `re_preprocess` | S1, S2 | discard the client's state, run the whole offline phase again with the same server, then send a fresh query to it. |

Any other combination is a usage error (exit 2). A policy makes at most 3 further attempts after the first rejection, the same `k` as the disabled `retry_same`. On each attempt the serving server is faulted with probability `--fault-prob`, drawn from the fault stream, except that an honest replica never is. With `--fault drop_server`, the client counts a missing answer as a rejection.

Every record gains these fields, null in offline records:

| Field | Type | Meaning |
|---|---|---|
| `recovery_outcome` | string or null | `accepted` (the first answer was accepted), `recovered` (accepted after at least one rejection), `aborted` (the policy gave up), or `silent_wrong` (the client accepted a wrong record) |
| `recovery_attempts` | integer or null | further attempts made, 0 when the first answer was accepted |

and these metrics, null in offline records and when `recovery_attempts` is 0:

| Metric | Meaning |
|---|---|
| `recovery_s`, `recovery_cpu_s` | all work after the first rejection: queries, answers, decoding and verification, plus the offline phase for `re_preprocess`. `recovery_s` counts a two-server attempt's servers as answering in parallel, so each attempt adds its slower answer, as `latency_e2e` does for the first attempt. `recovery_cpu_s` sums all of it |
| `recovery_up_bytes`, `recovery_down_bytes` | bytes those attempts exchanged, offline bytes included |

`correct` and `detected` describe the first answer, as before. The outcome covers every attempt: `silent_wrong` means the record the client finally accepted is wrong, on the first attempt or a later one, so `recovered` always ends with the correct record. A baseline's outcome is `accepted` or `silent_wrong`, or `aborted` for `drop_server`. The analysis derives the time to a correct answer from the first attempt's times plus `recovery_s`. No adapter models how long a client waits before it gives up on a withheld answer, so under `drop_server` that time is a lower bound.

## Encoding bytes

Bytes measure what a client and server would exchange over a network. When upstream has a serialiser, use it. When it has none, as VeriSimplePIR does, encode each element in the fewest whole bytes its bit width needs, packed tightly, which is what upstream's size formulas count. In-memory representations, such as two 64-bit limbs per multi-limb element, count toward `client_storage_bytes`, not toward communication.

## Conformance test

`tests/test_adapters.py` runs every built adapter through the spec §11 checks: `test_correctness`, `test_fault_detection`, `test_fault_baseline` and `test_comm_analytical`. Gate G1 passes when it passes for all eight arms:

```sh
make build && make conformance
```

`make conformance` (`harness/conformance.py`) runs the test in parallel partitions, one per slow S4 run, and refuses to start unless the partitions cover every collected test exactly once. It writes each partition's log and a `SUMMARY.md` to `logs/p1/<UTC time>/`. `notebooks/02_g1_conformance.ipynb` runs it, and the README's G1 row cites that cell. `uv run pytest tests/test_adapters.py -k ...` still runs any subset directly. `make recovery` runs `tests/test_recovery.py` (the "Recovery" rules) the same way, in five partitions, into `logs/p7/`, and `notebooks/03_p7_recovery.ipynb` runs it.

Both this test and `tests/test_recovery.py` start each adapter through `harness/memcap.py`. It kills the adapter and its children once their combined memory footprint (resident plus compressed, from `proc_pid_rusage`) passes `VPIR_ADAPTER_MEM_CAP_GIB` (default 2 GiB), and the test fails with that message.
