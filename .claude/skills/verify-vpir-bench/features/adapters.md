# Build and run the measurement adapters

`make adapters` builds the three adapters that drive the upstream libraries for the eight benchmark configurations: `simplepir-adapter` (S0-go), `vsp-adapter` (S0-cpp, S1, S2) and `apir-adapter` (S3, S4, M0, M1), all in `build/adapters/`. In bench mode an adapter runs one cell, one configuration at one database size, and appends one JSON record per repetition and phase to `--out`. The contract is `src/adapters/README.md`: exit 0 on success, 1 on an internal error, 2 on a usage error. Parameters the library rejects still exit 0 and write `status: "unsupported_params"` records.

## Sub-features

- `adapters-build` builds all three binaries into `build/adapters/` from the pinned, patched submodules, with no network access (`GOPROXY=off`).
- `adapters-bench` runs one honest cell and writes offline and online records, with `correct: true` on every online record.
- `adapters-fault` injects a fault into a verifiable arm, which reports `detected: true` on every faulted repetition.
- `adapters-usage` rejects an invalid command line with exit 2, for example `--recovery retry_same` (decision 12).
- `adapters-unsupported` writes `unsupported_params` records and exits 0 for a size the scheme does not support (S3 below 1 MiB).

## How to get to it (user POV)

- `make adapters`. `make conformance` and `make recovery` run it first.
- `build/adapters/<adapter> --arm ARM --db-bytes N --record-bytes N --phase offline|online|all --reps N --warmup N --seed N --fault KIND [--fault-prob F] [--recovery POLICY] [--replicas-cheat] --out PATH.jsonl`.
- Each adapter's README in `src/adapters/<dir>/README.md` lists what its timers cover and how it injects faults.

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0. `make build` has run, so `third_party/verisimplepir/bin/lib/libverisimplepir.dylib` exists.

- **Build.** Run `bash $S/step.sh adapters-build make adapters`. Exit 0. `build/adapters/` holds `simplepir-adapter`, `vsp-adapter` and `apir-adapter`. The step log's state before and after is identical, so the build changes no tracked file.
- **Bench, one binary each.** Run each of these. Each exits 0.
  - `bash $S/step.sh adapters-bench-s0go build/adapters/simplepir-adapter --arm S0-go --db-bytes 1048576 --record-bytes 32 --phase all --reps 3 --warmup 1 --seed 1234 --fault none --out "$VERIFY_RUN/s0go.jsonl"`
  - `bash $S/step.sh adapters-bench-s1 build/adapters/vsp-adapter --arm S1 --db-bytes 1048576 --record-bytes 32 --phase all --reps 3 --warmup 1 --seed 1234 --fault none --out "$VERIFY_RUN/s1.jsonl"`
  - `bash $S/step.sh adapters-bench-m1 build/adapters/apir-adapter --arm M1 --db-bytes 1048576 --record-bytes 32 --phase all --reps 3 --warmup 1 --seed 1234 --fault none --out "$VERIFY_RUN/m1.jsonl"`
- **Check the records.** Run `bash $S/step.sh adapters-records uv run python -c "import json,sys; [print(f, sorted({(r['phase'], r['status']) for r in map(json.loads, open(f))}), all(r['correct'] for r in map(json.loads, open(f)) if r['phase'] == 'online')) for f in sys.argv[1:]]" "$VERIFY_RUN/s0go.jsonl" "$VERIFY_RUN/s1.jsonl" "$VERIFY_RUN/m1.jsonl"`. Each file prints `[('offline', 'ok'), ('online', 'ok')] True`, and each holds 8 lines (4 offline, 4 online).
- **Fault.** Run `bash $S/step.sh adapters-fault build/adapters/vsp-adapter --arm S2 --db-bytes 1048576 --record-bytes 32 --phase online --reps 5 --warmup 0 --seed 1234 --fault flip_answer_bit --out "$VERIFY_RUN/s2-fault.jsonl"`. Exit 0. Then `bash $S/step.sh adapters-fault-check uv run python -c "import json,sys; rs=[json.loads(l) for l in open(sys.argv[1])]; print(len(rs), all(r['faulted'] and r['detected'] is True for r in rs))" "$VERIFY_RUN/s2-fault.jsonl"` prints `5 True`.
- **Usage error.** Run `bash $S/step.sh --expect 2 adapters-usage build/adapters/apir-adapter --arm M1 --db-bytes 1048576 --record-bytes 32 --phase online --reps 1 --warmup 0 --seed 1234 --fault flip_answer_bit --recovery retry_same --out "$VERIFY_RUN/usage.jsonl"`. Exit 2, and the log names `retry_same`.
- **Unsupported.** Run `bash $S/step.sh adapters-unsupported build/adapters/apir-adapter --arm S3 --db-bytes 65536 --record-bytes 0 --phase online --reps 2 --warmup 0 --seed 1234 --fault none --out "$VERIFY_RUN/s3-unsupported.jsonl"`. Exit 0. `$VERIFY_RUN/s3-unsupported.jsonl` holds one line, because an unsupported cell writes one record per phase, not per repetition. It has `"status": "unsupported_params"` and an `error` naming the sizes LWE+ supports.

## Gotchas

- Keep cells small. S4 takes about 6 minutes to build its digest and 18 s per query at 1 MiB (`notebooks/06_s4_cost.ipynb`), so drive S4 at 64 KiB. S3 and S4 retrieve one bit and need `--record-bytes 0`.
- `--out` appends. Reusing a path mixes runs, so give every step its own file under `$VERIFY_RUN`.
- A faulted M1 answer can make upstream allocate tens of GiB. The adapter refuses those decodes and logs "reconstruction not called". That line in stderr is expected, not a failure.
- `vsp-adapter` finds `libverisimplepir.dylib` through an rpath relative to the binary. A copy outside `build/<dir>/` fails to load the library.
- The records carry wall and CPU times. They are not benchmark numbers: CLAUDE.md requires every number in README.md to come from a notebook cell.
