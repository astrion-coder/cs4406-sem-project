# Serve and client mode

Every adapter also runs as a network server, `--mode serve`, and as that server's client, `--mode client`, following `docs/serve_protocol.md`. A server builds the database from `--seed`, runs the offline phase once, prints `vpir-serve ready HOST:PORT` on stderr, and answers framed `QUERY` messages with `--workers` threads. On SIGTERM or SIGINT it finishes what it has read, appends one summary record to `--out` and exits 0. A client rebuilds the offline state from the same seed, checks each server's fingerprint, sends queries, verifies every answer, and writes one record per query plus, with `--corpus-out`, the corpus SG's load generator replays. Two-server arms (M0, M1) run two server processes. `tests/test_serve.py` is gate G6's judge.

## Sub-features

- `serve-ready` starts a server on `127.0.0.1:0` and prints exactly one ready line with the real port.
- `serve-client` runs a client session against one or two servers: every answer verifies, each record has `"mode": "client"` and one `rtt_s` per server, and the corpus is written.
- `serve-shutdown` stops a server with SIGTERM: exit 0 and one appended summary record with `"mode": "serve"` and its final counters.
- `serve-judge` passes `tests/test_serve.py` for an arm: concurrent correctness, malformed frames, STATS, shutdown and usage errors.

## How to get to it (user POV)

- `build/adapters/<adapter> --mode serve --arm ARM --db-bytes N --record-bytes N --seed N --workers W --listen HOST:PORT [--server-id 0|1] [--out PATH.jsonl]`.
- `build/adapters/<adapter> --mode client --arm ARM --db-bytes N --record-bytes N --seed N --connect HOST:PORT[,HOST:PORT] --session-queries Q --out PATH.jsonl [--corpus-out PATH]`.
- `uv run pytest tests/test_serve.py -k ...`.
- `notebooks/05_serve_thread_safety.ipynb`, which runs every arm under the race detectors (see `notebooks.md`).

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, `doctor.sh` exits 0, and `build/adapters/` is current (`make adapters`).

- **One server.** Run `bash $S/step.sh serve-s1 bash $S/serve_session.sh S1`. Exit 0. It prints `server 0: vpir-serve ready 127.0.0.1:<port>`, `client: exit 0, 8 records, corpus <n> bytes`, and `server 0: exit 0, 1 summary record(s)`.
- **Two servers.** Run `bash $S/step.sh serve-m1 bash $S/serve_session.sh M1`. Exit 0, with a ready line for server 0 and server 1, and both servers exiting 0 with one summary record each.
- **Check the records.** Run `bash $S/step.sh serve-records uv run python -c "import json,sys; c=[json.loads(l) for l in open(sys.argv[1])]; s=[json.loads(l) for l in open(sys.argv[2])]; print(len(c), all(r['mode']=='client' and r['correct'] and len(r['metrics']['rtt_s'])==2 for r in c), s[0]['mode'], s[0]['answered'])" "$VERIFY_RUN/serve-M1/client.jsonl" "$VERIFY_RUN/serve-M1/server0.jsonl"`. It prints `8 True serve 8`.
- **Judge.** Run `bash $S/step.sh serve-judge uv run pytest tests/test_serve.py -q -p no:cacheprovider -k "S0-go or S0-cpp or M0"`. Exit 0, with `60 passed` and none skipped.
- **Ready again.** Run `bash $S/doctor.sh`. It exits 0, and `pgrep -f -- "--mode serve"` prints nothing, so no server outlived its step.

## Gotchas

- Never start a server with `&` in a step of your own. `serve_session.sh` and the judge kill only the servers they started. A server left running holds its port and a CPU core.
- The server's ready line waits for the offline phase. S4 takes about 22 s at 64 KiB, and about 6 minutes at 1 MiB, before it is ready.
- `--fault` is a usage error (exit 2) in both modes. Fault and recovery runs stay in bench mode.
- The two servers of an M0 or M1 session need `--server-id 0` and `1`, and the client lists them in that order in `--connect`.
- A shutdown with idle clients still connected can take a couple of seconds, because the server drains and closes each connection cleanly.
