# Run the adapter test suites

`make conformance` runs the adapter conformance test, `tests/test_adapters.py`, which is gate G1. `make recovery` runs the P7 fault and recovery test, `tests/test_recovery.py`. Both first run `make adapters`, then `harness/conformance.py`, which checks that its partitions cover every collected test exactly once, runs the partitions in parallel, and prints one line per partition. It writes each partition's log, its JUnit XML and a `SUMMARY.md` to `logs/p1/<UTC time>/` (G1) or `logs/p7/<UTC time>/` (P7), and exits 0 only when every test passed. Every adapter process the tests start runs under a memory cap.

## Sub-features

- `suites-g1` runs the conformance test in five partitions. At the defaults it passes 56 of 56 tests.
- `suites-p7` runs the recovery test in five partitions. At the defaults it passes 159 of 159 tests.
- `suites-summary` writes `SUMMARY.md` with one row per partition (selection, passed, failed, skipped, exit code, wall time, log) and the line `<N> of <M> tests passed.`
- `suites-memcap` kills an adapter whose memory footprint, resident plus compressed, passes `VPIR_ADAPTER_MEM_CAP_GIB` (default 2), and fails that test with the cap in the message.

## How to get to it (user POV)

- `make conformance` and `make recovery`.
- `uv run python -m cs4406_sem_project.harness.conformance [--suite g1|recovery] [--logs-root DIR]`, which the make targets call.
- `uv run pytest tests/test_adapters.py -k ...` or `tests/test_recovery.py -k ...` for any subset, with `VPIR_ADAPTER_DIR` to test another build directory.
- `notebooks/02_g1_conformance.ipynb` cell 1 and `notebooks/03_p7_recovery.ipynb` cell 1, which run the make targets at the defaults (see `notebooks.md`).

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0.
- Nothing else is running. Each suite runs five partitions, one CPU-bound adapter each.

- **G1, smoke counts.** Run `bash $S/step.sh suites-g1 env VPIR_CONFORMANCE_REPS=10 make conformance`. Exit 0. After the adapter build, five lines `<partition>: <passed> of <total> passed (exit 0, <s>s) <log>` for `go-rest`, `vsp`, `s4-honest`, `s4-flip_answer_bit` and `s4-wrong_record`, in the order they finish. It takes about 30 minutes: `s4-honest` runs several S4 cells, and each first builds a 1 MiB digest (about 6 minutes).
- **G1 summary.** Run `bash $S/step.sh suites-g1-summary bash -c 'd=$(ls -td logs/p1/*/ | head -1); cp "${d}SUMMARY.md" "$VERIFY_RUN/g1.SUMMARY.md"; grep "tests passed" "${d}SUMMARY.md"'`. It prints `` `tests/test_adapters.py`: 56 of 56 tests passed. ``
- **P7, smoke counts.** Run `bash $S/step.sh suites-p7 env VPIR_RECOVERY_REPS=10 VPIR_RECOVERY_HONEST_REPS=50 make recovery`. Exit 0. Five partition lines, for `s0`, `go-rest`, `s1`, `s2` and `s4`, each `exit 0`, in about 15 minutes.
- **P7 summary.** Run `bash $S/step.sh suites-p7-summary bash -c 'd=$(ls -td logs/p7/*/ | head -1); cp "${d}SUMMARY.md" "$VERIFY_RUN/p7.SUMMARY.md"; grep "tests passed" "${d}SUMMARY.md"'`. It prints `` `tests/test_recovery.py`: 159 of 159 tests passed. ``
- **Memory cap.** Run `bash $S/step.sh --expect 1 suites-memcap env VPIR_ADAPTER_MEM_CAP_GIB=0.002 uv run pytest tests/test_adapters.py -q -p no:cacheprovider -k "record_fields and M0"`. Pytest exits 1, and the log contains `memory footprint passed the 0.002 GiB cap (VPIR_ADAPTER_MEM_CAP_GIB)`. The M0 adapter peaks near 10 MiB, so a 2 MiB cap must kill it.

## Gotchas

- Smoke counts exercise the runner and every test, but they are not the gates. G1 and the P7 claim in README.md need the defaults: `make conformance` takes about 70 minutes and `make recovery` about 2 hours, both dominated by S4, S1 and S2.
- Keep `VPIR_RECOVERY_BYTES_REPS` at its default of 20. At 10, `test_re_preprocess_pays_offline_bytes` fails by chance for seed 1234, because no faulted repetition recovers after exactly one attempt.
- `make conformance` and `make recovery` rebuild `build/adapters/` first. Do not run either while another run uses those binaries, a notebook included.
- The runner refuses to start when its partitions miss or double-count a test, and prints both lists. A new test whose id contains no arm name falls into no partition.
- The memory cap watches the whole process tree, so it covers VeriSimplePIR's forked children. Its poll every 0.1 s can overshoot by up to one poll's worth of allocation before the kill.
