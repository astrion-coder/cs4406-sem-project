# Serve protocol

This document fixes how a client talks to an adapter running as a server, for phase P8 and the experiments that need separate processes: RQ3b (emulated network), RQ6 (concurrent clients) and RQ7 (server cores). It is the contract between the adapters' `--mode serve` and `--mode client` (task IC-8) and the load generator in `src/loadgen/` (task SG-7). Spec §9 gives the command lines, and this document adds what §9 leaves open. Bench mode, `src/adapters/README.md`, is unchanged.

## Processes

- **Server.** `<adapter> --mode serve` builds the database from `--seed`, runs the offline phase once, then answers queries with `--workers` W worker threads that share one read-only database. A two-server arm (M0, M1) runs two server processes, with `--server-id 0` and `--server-id 1`.
- **Client.** `<adapter> --mode client` is a real client of the scheme. It generates each query, sends one query per server, and decodes and verifies the answers. It is used for the single-client network runs (RQ3b), and it writes the query corpus the load generator replays.
- **Load generator.** `src/loadgen/` holds no cryptography. It replays a corpus of recorded queries to the servers and checks every answer against the recorded hash of the honest answer. This keeps it cheap enough not to be the bottleneck (`test_loadgen_not_bottleneck`), and the same Go program drives every arm.

## Command lines

```
<adapter> --mode serve  --arm ARM --db-bytes N --record-bytes N --seed N --workers W
                        --listen HOST:PORT [--server-id 0|1] [--out PATH.jsonl]
<adapter> --mode client --arm ARM --db-bytes N --record-bytes N --seed N
                        --connect HOST:PORT[,HOST:PORT] --session-queries Q --out PATH.jsonl
                        [--corpus-out PATH]
```

- `--listen 127.0.0.1:0` picks a free port. When the server is ready to answer, it writes exactly one line to stderr: `vpir-serve ready HOST:PORT`, with the real port. The harness waits for that line.
- The client takes the same `--arm`, `--db-bytes`, `--record-bytes` and `--seed` as the server, because it rebuilds its offline state itself (see "Session").
- `--fault` is a usage error (exit 2) in both modes. A targeted fault needs the queried index, which a PIR server does not know. Fault and recovery runs (RQ4) stay in bench mode.
- Exit codes are as in bench mode. On SIGTERM or SIGINT the server stops accepting connections, answers the queries it has already read, appends one summary record to `--out` if it was given, and exits 0. The summary record is one JSON object: `"mode": "serve"`, the `INFO` fields except `scheme_params`, and the final `STATS` fields (see "Server statistics").

## Framing

Every message on the wire, and every entry in a corpus file, is a frame:

```
u32  length   bytes that follow: 1 + len(body)
u8   type
...  body
```

All integers are unsigned and big-endian. A peer that receives a frame longer than 1 GiB, or a type it does not know, sends `ERROR` with request id 0 and closes the connection. A server reads the header alone before deciding, so it never waits for the body of an oversized frame. A server accepts only the three client-to-server types, `HELLO`, `QUERY` and `STATS_REQ`. Any other type, `EXPECT` included, is unknown to it.

| Type | Name | Direction | Body |
|---|---|---|---|
| 0x01 | `HELLO` | client to server | JSON `{"protocol": 1}` |
| 0x02 | `INFO` | server to client | JSON, see "Session" |
| 0x03 | `QUERY` | client to server | `u64 req_id`, then the query bytes for this server |
| 0x04 | `ANSWER` | server to client | `u64 req_id`, `u64 queue_ns`, `u64 answer_ns`, then the answer bytes |
| 0x05 | `STATS_REQ` | client to server | empty |
| 0x06 | `STATS` | server to client | JSON, see "Server statistics" |
| 0x07 | `ERROR` | server to client | `u64 req_id` (0 for the connection), then a UTF-8 message |
| 0x10 | `EXPECT` | corpus files only | `u64 req_id`, `u8 server_id`, `u32 answer_len`, 32-byte SHA-256 of the honest answer |

A client chooses its own `req_id` values and never uses 0, which `ERROR` reserves for the connection.

Query and answer bytes are exactly the bench-mode encodings in each adapter's README, so `up_bytes` and `down_bytes` mean the same thing in both modes. S3's `2t+1` sub-queries travel in one `QUERY` frame, as in bench mode (decision 15).

## Session

A connection starts with `HELLO`, and the server replies `INFO`:

```json
{"protocol": 1, "arm": "S1", "server_id": 0, "servers": 1, "db_bytes": 1048576,
 "record_bytes": 32, "n_records": 32768, "seed": 1234, "workers": 4,
 "upstream_sha": "...", "db_sha256": "...", "public_seed": "..." , "scheme_params": {}}
```

`record_bytes` is null for S3 and S4, as in bench records, and `n_records` counts bits for them (`8 × db_bytes`).

The protocol does not transfer offline state. The client rebuilds it from the same `--seed`, untimed, and checks the fingerprint before its first query. RQ3a and RQ3b take offline bytes and times from bench mode, and nothing in serve mode measures them.

- `db_sha256` is the SHA-256 of the seeded database bytes.
- `public_seed` is the seed of the public parameters (the matrix `A`), in hex, or null where upstream fixes them. apir-code fixes `A` (`utils.SeedMatrixA`). S0-go must use upstream's `InitCompressedSeeded` in serve and client mode, because `Init` draws `A` from an unseeded generator. That call overwrites the library's global PRG reader (`simple_pir.go:110,115`), so it runs once, at startup, before any query.
- S1 and S2 clients run the whole preprocessing exchange locally against their own server object built on the same database. The server's answers depend only on the database and the query, so the client's verification state fits the remote server.
- The client compares `arm`, `db_bytes`, `record_bytes`, `seed`, `db_sha256` and `public_seed` with its own, and exits 1 on any difference. The corpus run below then proves agreement directly, since every answer must decode correctly.

## Queries

- A client may pipeline queries on one connection. The server can answer them out of order, so the client matches answers by `req_id`. The load generator's closed loop keeps one query in flight per client, and its open loop pipelines.
- Readers put each complete `QUERY` on one FIFO queue shared by the W workers. The queue holds at most 4096 queries, and a full queue stops the readers, which pushes back through TCP. A worker answers one query at a time, so at most W library `Answer` calls run at once.
- `answer_ns` is the monotonic time around the library's answer call alone, which is the span bench mode times as `server_answer_s`. `queue_ns` runs from the moment the frame has been read to the moment a worker starts on it.
- The server checks each query's length against the size its parameters imply, and sends `ERROR` with that query's `req_id` without calling the library when it differs. The connection stays open, and later queries on it are answered. Upstream answers abort the whole process on a malformed input (`mat_packed.cpp:424`, `assert(a.mat.rows % 8 == 0)`; `log.Fatal` in apir-code's `pir_point.go:46,56`).
- For a two-server arm, the client sends one `QUERY` to each server and finishes when both answers have arrived.

## Server statistics

`STATS_REQ` returns the server process's counters at that moment:

```json
{"wall_s": 12.5, "cpu_user_s": 40.1, "cpu_sys_s": 0.4, "peak_rss_bytes": 123456789,
 "answered": 9001, "queued": 3}
```

`answered` counts the `ANSWER` frames written so far, and `queued` the queries read but not yet started by a worker.

The load generator asks at the start and the end of its steady-state window. `server_cpu_util` = 100 × Δ(`cpu_user_s` + `cpu_sys_s`) / Δ`wall_s`, which can exceed 100 with several workers, as psutil's per-process figure does. `server_peak_rss_bytes` comes from the final `STATS`, or from the server's summary record. CPU and memory come from `getrusage` on the server itself, so no external sampler is needed.

## Corpus

`--mode client --corpus-out PATH` runs Q queries one at a time against honest servers, checks that each decodes to the right record, and writes:

- `PATH`, a file of frames. First the `INFO` of each server, in server order. Then for each query, with `req_id` = 1, 2, ..., Q, and for each server in order: its `QUERY` frame, then an `EXPECT` frame with the length and SHA-256 of that server's answer.
- `--out`, one bench-shaped online record per query, with the client-side costs `query_gen_s`, `client_recover_s` and `client_verify_s`. `server_answer_s` comes from `answer_ns`, and a new metric `rtt_s` gives one round-trip time per server.

The client exits 1 if any answer decodes wrong, so a corpus only ever records answers that a verified honest server gave. Each library's answer is a deterministic function of the database and the query (see "Threads"), so a byte-identical answer under load proves the answer correct. This is the check `test_concurrent_correctness` makes.

The load generator replays the corpus round-robin. A corpus of 64 queries is enough, because no library caches anything between queries. Per request it records the send and receive times, `queue_ns`, `answer_ns`, and whether the answer matched its `EXPECT` frame. A mismatch is a correctness failure, never a slow answer.

## What the load generator measures

- `throughput_qps`: queries whose answers all matched, completed inside the steady-state window, divided by its length (spec §5.1).
- Observed latency: from the first byte of the first `QUERY` sent to the last byte of the last `ANSWER` received.
- End-to-end latency (spec §6, query generation to verified record): the observed latency plus that corpus entry's own `query_gen_s`, `client_recover_s` and `client_verify_s`. The client's work is added rather than run, which keeps the load generator out of the server's way. The report has to say so.
- The load generator's own CPU use over the window, from `getrusage`, for `test_loadgen_not_bottleneck`.

## Threads

Spec §9 requires reading each library's answer function before running it from several threads. All five are safe to share across threads, with the one condition below. W is therefore the worker-pool size, and no arm needs worker processes or a lock.

| Arm | Answer call | Why concurrent calls are safe |
|---|---|---|
| S0-go | `SimplePIR.Answer` | Reads the database and writes a local `ans` (`simple_pir.go:162-183`). With a batch of one, `SelectRows` returns the database matrix itself (`matrix.go:400-402`). The C kernel `matMulVecPacked` only reads its inputs and writes `out` (`pir.c:101-196`), and `pir.c` has no static state. |
| S0-cpp, S1, S2 | `VeriSimplePIR::Answer(ct, PackedMatrix)` | Declared `const` (`preproc_pir.h:149`, `preproc_pir.cpp:357`). Its kernel `simplepir_matVecMulColPacked_variableCompression` (`mat_packed.cpp:406`) builds a local `Matrix out` and only reads the packed database. The answer path has no `static`, `thread_local` or `mutable` state and no OpenMP or pthreads. |
| S3 | `server.Amplify.Answer` | Calls `matrix.BinaryMul` per sub-query and writes a fresh matrix (`amplify.go:23-29`). The server keeps no mutable fields. |
| S4 | `server.DH.AnswerBytes` | Decodes the query into locals and runs one goroutine (`NGoRoutines := 1`, `dh.go:25`) that only reads the database (`dh.go:19-74`). |
| M0, M1 | `server.PIR.Answer` | Writes only locals (`point.go:42-62`). The `cores` field is stored (`point.go:18-28`) and never read, so it has no effect at this pin. |

Rules that follow:

- **VeriSimplePIR answers must be freed.** `Matrix` has no destructor (`mat.h:51-53`, commented out), so the server frees each answer's data once it has been written to the socket. Otherwise every answer leaks `8 × ell` bytes.
- **Go servers raise `GOMAXPROCS` to W + 1.** Bench mode pins it to 1 (`simplepir_go/main.go`, `apir_go/main.go`). The extra slot is for network I/O, and the worker pool still caps answer work at W.
- **Clients are not shared.** apir-code's clients write `c.state` in `Query` and read it in `Reconstruct` (`client/lwe.go:44-51,75-90`, `client/dh.go:62`, `client/pir_point.go:50,69`). Each client process owns its own client object.

The table rests on reading the code. Once serve mode exists, IC also runs the Go servers built with `-race` and the C++ server built with `-fsanitize=thread`, at 1 MiB with W = 6 and 16 clients, and records the result here.

- **Go, 10 November.** No race reports and no mismatched answers. S0-go, S3, M0 and M1 ran at 1 MiB for 65 s, and S4 at 64 KiB for 100 s. Every answer was checked against the corpus by SHA-256: 1,137,619 for S0-go, 26,628 for S3, 25,544 for M0, 2,072 for M1 and 154 for S4. The detector cannot see inside S0-go's C kernel, so that part still rests on the reading above.

## Changes this needs elsewhere

- **Schema (SG-3).** Records need a `mode` field (`bench`, `client` or `serve`) and the client metric `rtt_s`. Load-generator rows need their own record type.
- **Spec §9** points here for the wire protocol, and §7's note that apir-code servers take `cores` gets the finding above.
