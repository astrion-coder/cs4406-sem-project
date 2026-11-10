# serve_go

The serve protocol (`docs/serve_protocol.md`) for both Go adapters, written once. `simplepir_go` and `apir_go` import it through a local `replace` in their `go.mod`. It uses only the standard library.

An adapter supplies one `serve.Arm` with two constructors. `serve.Main(args, arm)` handles `--mode serve` and `--mode client` and returns the exit code. Each adapter's `main` calls it whenever `--mode` is not `bench`, so bench mode never reaches this package.

| Interface | The adapter provides | This package does the rest |
|---|---|---|
| `Server` | `Info()`, `CheckQuery(q)` (length only, no library call), `Answer(q)` (the library's answer and its own `answer_ns`) | framing, `HELLO`/`INFO`, one reader per connection, one 4096-entry queue shared by W workers, `queue_ns`, `ERROR` for bad frames and lengths, `STATS` from `getrusage`, SIGTERM/SIGINT drain and the summary record, `GOMAXPROCS` = W + 1 |
| `Client` | `Info()` (its own fingerprint, rebuilt from `--seed`), `Next()` (one query per server, and a `Finish` that decodes, verifies and returns the bench-shaped record) | dialling, the fingerprint check, pipelined `req_id` matching, `mode` and `rtt_s` on each record, and the corpus file |

| File | Contents |
|---|---|
| `frame.go` | frame types, `ReadFrame`/`WriteFrame`, the 1 GiB limit decided from the header |
| `options.go` | the serve and client command lines. `--fault` is a usage error |
| `info.go` | `INFO`, `STATS`, the summary record |
| `server.go` | the server loop |
| `client.go` | the client loop and corpus writer |

A constructor that finds the parameters unsupported returns `serve.Unsupported` with the arm's bench-shaped records. `Main` then writes no ready line, appends them to `--out` (a server writes one record naming the cell), and exits 0, as bench mode does.

A worker writes its `ANSWER` under the connection's write lock, so a client that stops reading stalls the worker answering it. The load generator and the test clients always read, so this does not arise in our runs.

`notebooks/05_serve_thread_safety.ipynb` builds both Go servers with `-race` and runs every arm under concurrent load (see `docs/serve_protocol.md`, "Threads"). It cannot see inside S0-go's C kernel (`pir.c`), whose safety rests on the code reading in that document.
