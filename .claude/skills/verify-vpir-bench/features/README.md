# vpir benchmark verification map

This directory is the maintained source for verifying the benchmark's command-line pipeline. Read this index before driving anything, then use the matching feature file as the recipe.

## Baseline preconditions

- Work from the repo root of a checkout where `make setup` has run.
- `bash .claude/skills/verify-vpir-bench/scripts/doctor.sh` exits 0.
- `export VERIFY_RUN=logs/verify/$(date -u +%Y%m%dT%H%M%SZ)` and `S=.claude/skills/verify-vpir-bench/scripts` are set in the shell that drives.
- No other verification run, `make test`, or notebook run is active on this checkout.

## Driving conventions

- Start every recipe from the baseline state unless its preconditions say otherwise.
- Run every command through `bash $S/step.sh`, with `--expect` when the command must fail.
- Treat every command as literal. Keep quoted names, flags and paths unchanged.
- Match the runner's `<suite>: <status> (exit N, Xs) <log>` lines, not any line that contains a suite name, because `make` echoes its recipe first.
- When a recipe breaks something on purpose, run its restore step before the next recipe. `cleanup.sh` is the fallback, not the plan.

## Proof and skip reporting

- Every step leaves `$VERIFY_RUN/<step-name>.log` with the command, the output, the exit code, and the repo and submodule state before and after.
- Copy each produced artifact into `$VERIFY_RUN` with the command the recipe gives.
- Record the feature ID and entry point with each claim.
- Report an entry point you could not drive with the command you tried and the unmet precondition.
- Do not report one entry point as verified through another.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the behavior a user sees. It then has exactly four H2 sections, in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every entry point.
3. `Driving it with step.sh` starts with `Preconditions:`, then pairs each action with an exact command and its observable result.
4. `Gotchas` lists traps that waste or invalidate a run.

## Features

- [Run the upstream test suites](./upstream-tests.md) covers `make test`, suite selection, unknown suites, failure reporting and fresh Go runs.
- [Apply build patches](./patch.md) covers applying, skipping and drift detection in `make patch`.
- [Build the upstream code](./build.md) covers `make build` from a clean VeriSimplePIR tree and the arm64 outputs.
- [Record the machine and toolchain](./sysinfo.md) covers `make sysinfo` and the environment notebook.

## Not mapped yet

- `make setup` clones from GitHub and stages the submodule links in the repo index. Drive it only on a fresh clone, and only when setup itself is under test.
- `docs/tools/md2pdf.py` renders the one-page reports. Its page-limit check is its own proof (`--max-pages 1` exits 1 on overflow).
- The measurement adapters and grid runner arrive in spec phase P1. Add one feature file per adapter.
