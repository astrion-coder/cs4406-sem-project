# Run the upstream test suites

`make test` runs each upstream codebase's own test suite, plus the dpf-go equivalence check. It prints one line per suite, writes a log per suite and a `SUMMARY.md` under `logs/p0/<UTC timestamp>/`, and exits 0 only if every selected suite passed. All five passing is gate G0 in the spec.

## Sub-features

- `tests-all` runs the five suites in order: simplepir, apir-code, dpf-go, verisimplepir, dpf-equivalence.
- `tests-subset` runs only the suites named in `ONLY`.
- `tests-fresh` reruns Go tests instead of reusing Go's test cache.
- `tests-unknown` rejects an unknown suite name before running anything.
- `tests-failure` reports a suite that fails as `failed` with its exit code, and makes `make test` fail.

## How to get to it (user POV)

- `make test` from the repo root.
- `make test ONLY="<suite> [<suite> ...]"`.
- `uv run python -m cs4406_sem_project.harness.upstream_tests [--only NAME ...] [--logs-root DIR]`, which is what `make test` calls.
- `notebooks/01_g0_upstream_tests.ipynb` cell 1, which runs `make patch`, `make build` and `make test`.

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0.
- `make build` has run since the last change to `patches/` or `third_party/`.

- **Subset.** Run `bash $S/step.sh tests-subset make test ONLY="dpf-go dpf-equivalence"`. Exit 0. The output has exactly two suite lines, `dpf-go: passed` and `dpf-equivalence: passed`, and both name the same new `logs/p0/<ts>/` directory.
- **Fresh Go run.** Run `bash $S/step.sh tests-fresh bash -c '! grep -q "(cached)" "$(ls -td logs/p0/*/ | head -1)dpf-go.log"'`. Exit 0, meaning the dpf-go log has no `(cached)` result.
- **Unknown suite.** Run `bash $S/step.sh --expect 2 tests-unknown make test ONLY=nope`. Make exits 2. The output contains `error: unknown suite(s) ['nope']` followed by the five known names. Compare `ls logs/p0` before and after: no new directory.
- **Failure.** Run `bash $S/step.sh --expect 2 tests-failure make test ONLY=simplepir CC=/nonexistent/clang`. Make exits 2. The output has a `simplepir: failed (exit 1, ...)` line, and that suite's log names the missing C compiler.
- **All suites.** Run `bash $S/step.sh tests-all make test`. Exit 0. The output has five suite lines, each `passed (exit 0, ...)`, in the order listed above. On the M1 Pro it took 190 s on 2026-11-09, mostly simplepir and apir-code.
- **Proof.** Run `cp "$(ls -td logs/p0/*/ | head -1)SUMMARY.md" "$VERIFY_RUN/tests-all.SUMMARY.md"`. The copy has five rows, each with status `passed` and exit code `0`.

## Gotchas

- `make test` does not build. After changing a patch or upstream code, run `make build` first, or the VeriSimplePIR suite runs a stale `pir_test`.
- The apir-code suite runs with `-skip ^TestReal`. Those tests read the SKS PGP key dump in `third_party/apir-code/data/sks/`, which `make setup` does not download. A pass does not cover them.
- `logs/p0/<ts>/` is named to the second. Two runs started in the same second share a directory.
- `not_built` appears when a suite's directory or entry point is missing. That needs a fresh clone before `make setup`, so these recipes do not drive it. `tests/test_upstream_tests.py` covers it.
- Pass `CC=` as a make argument after `make test`. The Makefile's `export CC :=` overrides a `CC` set in the environment.
