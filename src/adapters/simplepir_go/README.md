# simplepir_go: adapter for arm S0-go

Drives the original Go SimplePIR (`third_party/simplepir`, package `pir`) through the contract in `../README.md`. It serves one arm, `S0-go`, the unauthenticated single-server baseline. There is no verification, so every record has `detected: null` and `verify_fused: false`.

## Build

The root Makefile does not build this yet. From the repository root:

```sh
cd src/adapters/simplepir_go && GOPROXY=off GOTOOLCHAIN=local CGO_ENABLED=1 CC=$(brew --prefix llvm)/bin/clang \
  go build -ldflags "-X main.upstreamSHA=$(git -C ../../../third_party/simplepir rev-parse HEAD)" \
  -o ../../../build/adapters/simplepir-adapter .
```

The module replaces `github.com/ahenzinger/simplepir` with `../../../third_party/simplepir`. SimplePIR has no other dependencies.

## Call sequence

The real sequence of `RunPIR` (`pir/pir.go:99-171`), split so each call is timed alone. `FakeSetup` and `RunFakePIR` are never called.

| Step | Call | Timed as |
|---|---|---|
| Offline | `Init` (public matrix `A`) | `public_params_s` |
| Offline | `Setup` | `server_preproc_s` |
| Online | `Query` | `query_gen_s` |
| Online | `Answer` | `server_answer_s` |
| Online | `recoverRecord`, one decode of the record (see Decoding) | `client_recover_s` |

`Init` runs with the garbage collector on, because the library draws every element of `A` through `math/big` and leaves garbage behind. A collection may therefore fall inside `public_params_s`. `Setup` runs with the collector off.

`Setup` squishes the database in place. The next offline repetition first calls `Reset` (as `RunPIR` does), then reads four random records back with `GetElem` and compares them with the generator. A layout or `Reset` bug fails the run with exit 1.

The compressed path (`RunPIRCompressed`, `InitCompressed`, `DecompressState`) is not used. It reseeds the package-global generator from the public seed (`simple_pir.go:110`, `:115`), so the client secret in `Query` would come from a stream keyed by a public value (spec open question 7). The uncompressed path ships `A` itself, so `offline_down_bytes` includes it.

## Data shape

`Record` (`record.go`) marshals to exactly the contract's fields. `Layout` (`layout.go`) maps a record to the database entries that hold it.

- `--db-bytes` and `--record-bytes` become entries in one function, `plan`.
- A record of `R` bytes is `ceil(R/8)` pieces. Each piece is one 64-bit entry, the widest `Recover` can return (`uint64`). A record under 8 bytes is one entry of `8R` bits.
- `n_records = db_bytes / record_bytes` (floor). The database is padded with zero entries to fill whole record rows.
- Piece `k` of record `r` is entry `(r/m * pieces + k) * m + r%m`, with `m` the number of columns. So all pieces of a record share column `r % m`. One `Query` selects that column and the single `Answer` covers all of it (decision 11). Decoding reads each piece from the rows of its entry index (see Decoding).
- The plaintext modulus `p` comes from the library (`PickParams`, then `PickParamsGivenDimensions`). With `n = 1024` and `logq = 32` as in `pir_test.go`, `p` is read from `params.csv` by column count (991 up to `2^13` columns, 701 up to `2^15`). At `p = 991` each 64-bit entry takes `ne = 7` elements of Z_p, so the matrix is larger than the data by the ratio `7 * log2(991) / 64` (about 1.09).
- The shape is chosen so every record sits in one column and `ell + m` is smallest, which is the near-square shape `ApproxSquareDatabaseDims` aims for. `scheme_params.record_rows` is the number of stacked records per column.
- Upstream `Recover` recomputes `H * secret` on every call, so calling it once per piece would make `client_recover_s` grow linearly with the number of pieces. The adapter does not do that. See Decoding.

`scheme_params`: `n`, `logq`, `p`, `sigma`, `ell` (`Params.L`, matrix rows), `m` (`Params.M`, columns), `entry_bits`, `ne`, `packing`, `pieces_per_record`, `record_rows`, `entries`, `basis`, `squishing`, `compressed_a` (false), `parallelism` (`single-thread`).

## Decoding

A client holding one answer for a column decodes the queried record once. The work that does not depend on the piece is done once per record, then each piece is read out. The VeriSimplePIR adapter (S0-cpp) follows the same rule, so S0-go and S0-cpp differ in implementation and not in decode strategy.

`recoverRecord` in `session.go` is the body of `SimplePIR.Recover` (`simple_pir.go:185-214`) with the piece-independent part hoisted out of the per-piece loop, using only exported library functions:

1. Once per record: the query offset `(p/2) * sum(query)` modulo `q` (`Matrix.Get`), and `interm = MatrixMul(H, secret)` over the whole column.
2. Per piece `k`: for each of the `ne` answer rows of entry `Entry(r, k)`, `Params.Round(ans - interm + offset)`, then `ReconstructElem` to rebuild the 64-bit piece.

The answer matrix is read and never written (upstream `Recover` subtracts and re-adds `interm` in place). Timers wrap `recoverRecord` only.

**Check.** On the first online repetition (including a warm-up one) the adapter calls upstream `Recover` for the record's first and last piece, outside every timer, on the same query, answer, hint and secret as the timed decode, and exits 1 with a `decode check` message if either piece differs. This runs on faulted repetitions too, since `Recover` and `recoverRecord` see the same faulted inputs and must agree.

## Wire encoding and bytes

A matrix is serialised as its elements in little-endian `uint32`, row-major, no header. Length is `4 * rows * cols`, which is upstream's `Size() * Logq / 8`. The server receives the deserialised query, and the client receives the deserialised answer, so faults act on the bytes.

| Metric | Bytes |
|---|---|
| `offline_down_bytes_hint_only` | hint `H`, `ell * n * 4` |
| `offline_down_bytes` | `H` plus `A`, `(ell + m) * n * 4` |
| `client_storage_bytes` | the same, since the client keeps `H` and `A` |
| `up_bytes` | query, `m` padded up to a multiple of 3 (the squish factor), times 4 |
| `down_bytes` | answer, `ell * 4` |

`analytical` uses the arithmetic of `SimplePIR.GetBW` (`simple_pir.go:88-97`): hint `L*N*Logq/8`, upload `M*Logq/8`, download `L*Logq/8`. `GetBW` only prints, so the adapter repeats its arithmetic. Every record carries all three `analytical` keys (`up_bytes`, `down_bytes`, `offline_down_bytes_hint_only`). Every record also carries every metric key, with null for the other phase's metrics. The measured upload exceeds the analytical one by at most 8 bytes (query padding).

## Data and indices

AES-128 counter mode keyed by `sha256(domain, seed)`. Domains `db`, `index`, `fault-online`, `fault-offline`, `check`. Record `r` is keystream bytes `[r*R, (r+1)*R)`, so the ground truth is regenerated from the index and never stored. The same seed gives the same database and query indices. `A`, the client secret and the LWE noise come from the library's own unseeded generator, so they differ between runs.

## Faults

Applied to serialised bytes or to a decoded copy of them, never in library code. Each repetition is faulted with probability `--fault-prob`, drawn from the seeded stream.

`Recover` for record `r` reads `pieces * ne` consecutive rows of the answer and of the hint, starting at row `(r / m) * pieces * ne`. Within each 32-bit element, rounding to a multiple of `delta = floor(2^32 / p)` drops every bit below `log2(q/p)`, so the lowest bit that matters is `ceil(log2(q/p))` (23 at `p = 991` and `p = 701`). A uniformly random bit lands outside those rows or below that bit most of the time, so the faults below pick among bits that matter.

| Fault | What happens |
|---|---|
| `flip_answer_bit` | one uniform bit among the answer bits the record depends on, that is a uniform row of the record's `pieces * ne` rows and a uniform bit position in `[ceil(log2(q/p)), 32)`. Flipping it moves one Z_p digit of one piece by at least one, and the digit change survives `Reconstruct_from_base_p` (an odd power of `p` times a nonzero digit difference is nonzero mod `2^64`). |
| `wrong_record` | the honest answer to a fresh query for a different record replaces the answer |
| `flip_hint_bit` | online records: one bit of the client's hint, flipped for that repetition's `Recover` and flipped back. Offline records: one uniform bit of a copy of the hint (no record is queried offline), flagged `faulted`, with `correct` and `detected` null because S0-go has no offline check. |
| `stale_record` | the answer comes from a second upstream database over the record-bit-flipped data (every byte of every record complemented), laid out as the honest one is (same entries, decision 11 pieces in one column) and squished as `Setup` leaves it, then answered by upstream `Answer` on the client's real query. Built once at start-up, untimed, and only when this fault is requested. The client keeps the honest hint, so decoding yields a wrong record and the outcome is `silent_wrong`. |
| `drop_server` | no answer reaches the client. The record has `faulted=true`, `correct` null, the answer and client metrics null and `down_bytes` null, and outcome `aborted`. |

`bad_proof` is a usage error (exit 2) because S0-go has no proof.

The online hint bit is chosen like the answer bit, with one more condition. Flipping bit `b` of `H[j][c]` shifts the client's `H * secret` term by `2^b * secret[c] mod q`, and that shift survives rounding only if it is at least `delta` away from 0 (mod q). The adapter holds the client secret in bench mode, so it draws `(row, column, bit)` uniformly from the record's rows, all `n` columns and bits `[ceil(log2(q/p)), 32)`, and redraws until the shift is at least `delta`. About one draw in nine is rejected at `p = 991`, mostly bit 31 with an even `secret[c]`. The result is uniform over the hint bits the record's decoding actually depends on, given this query's secret.

Self-check. On the first faulted repetition of `flip_answer_bit` or `flip_hint_bit` the adapter first decodes the honest answer and hint (untimed) and requires the ground-truth record, then requires the faulted decode to differ. Either failure aborts with exit 1 and a `fault check` message. The extra decode is outside every timer.

S0-go has no verification, so `detected` is always null and a faulted run shows up only as `correct: false`.

## Recovery

S0-go is a baseline and accepts only `--recovery abort`. `switch_replica` and `re_preprocess` are usage errors (exit 2). Every online record has `recovery_attempts` 0 and null `recovery_*` metrics. `recovery_outcome` is `accepted` when the decoded record is correct, `silent_wrong` when it is not (`correct=false`), and `aborted` for `drop_server`. Offline records carry the recovery fields as null.

## Single thread, memory

`runtime.GOMAXPROCS(1)` and the garbage collector is off during measurement (as in `RunPIR`), with explicit `runtime.GC()` between untimed steps. The library's `fmt.Printf` output goes to stderr. The database matrix takes `ell * m * 4` bytes before `Setup`, and `Setup` allocates a squished copy (a third of that) while the original is still live.

## Parameter rejection

The library rejects with panics. The adapter turns a panic during parameter selection or database setup into one `unsupported_params` record per requested phase (exit 0), with the panic text in `error`. A `--db-bytes` that needs more than `2^21` columns hits an upstream quirk: `Params.PickParams` walks past the last row of `params.csv` into the empty line after its trailing newline, so the panic text is `index out of range` rather than `No suitable params known!`.
