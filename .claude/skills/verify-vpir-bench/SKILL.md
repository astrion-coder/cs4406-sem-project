---
name: verify-vpir-bench
description: Drive and prove the verifiable-PIR benchmark's command-line pipeline on this Mac (make patch, build, test, sysinfo, adapters, conformance, recovery; the adapters' bench, serve and client modes; the RQ4 recovery driver; the notebooks behind README claims), recording exit codes, logs and before/after repo state as evidence. Use before claiming that a change to the Makefile, patches/, the upstream test runner, sysinfo, an adapter, the adapter test suites, serve mode or the RQ4 driver works, and to re-check gates G0, G1 and G6.
---

# Verify the vpir benchmark

The project is a command-line build-and-measure pipeline. People drive it through `make` targets at the repo root, the adapter binaries and the notebooks. It runs natively on an Apple M1 Pro (darwin/arm64). Only serve mode listens on a port, always `127.0.0.1:0`, and only for the life of one step.

- **Primary entry.** `make setup | patch | build | test | sysinfo | adapters | conformance | recovery` from the repo root.
- **Secondary entries.** The adapter binaries in `build/adapters/` (bench, `--mode serve` and `--mode client`), `uv run pytest tests/test_*.py`, `uv run python -m cs4406_sem_project.harness.upstream_tests`, `...harness.conformance` and `...harness.recovery` (the RQ4 driver), `uv run python docs/tools/md2pdf.py`, and the notebooks in `notebooks/`.
- **Not built yet.** The grid runner (SG's phase P2), the load generator and the analysis. Add a feature file for each when it lands.

The recipes live in `features/`. Read `features/README.md` before driving anything.

## Launch

There is no process to keep alive. The pipeline is ready when the toolchain is installed and the checkout is pinned, patched and built.

From a fresh clone, run these once. They need the network, and the installs are the human's to run (CLAUDE.md).

```sh
brew install go llvm openssl@3 iperf3
uv sync
make setup     # clones the three pinned submodules into third_party/, go mod download, uv sync
make build     # make patch, builds VeriSimplePIR, simplepir and apir-code, then make adapters
```

On an existing checkout, run `make build` alone. Ready means `doctor.sh` exits 0. A passing `make build` is not enough, because doctor also checks the pins and that every patch is applied.

Teardown is `cleanup.sh` (below). The build itself needs none.

## Doctor

```sh
bash .claude/skills/verify-vpir-bench/scripts/doctor.sh
```

Read-only. It prints one `ok` or `FAIL` line per check and exits non-zero on any failure. It checks:

- macOS on arm64, `go`, Homebrew LLVM clang, Homebrew `openssl@3`, `.venv` on Python 3.11.
- Each submodule's `HEAD` equals its pin in the Makefile's `PINS` list.
- Every `patches/<submodule>/*.patch` is applied.
- VeriSimplePIR's `pir_test` exists and `libverisimplepir.dylib` is arm64.
- `build/adapters/` holds the three adapter binaries.
- No other `upstream_tests`, conformance or recovery run, adapter test, notebook run, RQ4 driver run or adapter process is running.

Run it first, and again whenever a step surprises you. Do not drive while it reports another run. Two runs share the submodule working trees and build outputs (`make conformance` and `make recovery` rebuild `build/adapters/`), `make test` names its log directory to the second, and timing runs need the Mac to themselves, so concurrent runs corrupt each other. There is no way to isolate them on one checkout.

## Drive

Run every step through `step.sh`, which records the evidence.

```sh
export VERIFY_RUN=logs/verify/$(date -u +%Y%m%dT%H%M%SZ)
S=.claude/skills/verify-vpir-bench/scripts
bash $S/step.sh [--expect N] <step-name> <command> [args...]
```

`step.sh` runs the command from the repo root. It writes `$VERIFY_RUN/<step-name>.log` with the command, the `git status` of the repo and every submodule before and after, the combined output, the exit code and the wall time. It prints one `ok` or `MISMATCH` line and exits 0 only when the exit code equals `--expect` (default 0). `make` exits 2 when its recipe fails, so a make target that must fail takes `--expect 2`. Wrap pipelines and `&&` chains in `bash -c '...'`.

Each feature file gives the exact commands and what each step must show.

## Evidence

Evidence goes in `$VERIFY_RUN`, a directory under `logs/verify/`. It is gitignored and survives cleanup. Each recipe ends with the commands that copy its artifacts (a `SUMMARY.md`, the `env/` files) into `$VERIFY_RUN`.

Proof standards:

- **Drive the user path.** Use the `make` targets and the documented tool commands. Do not call `run()` in `upstream_tests.py` or rebuild things by hand inside `third_party/`. The exceptions are recipe steps that set up a failure on purpose and say how to restore it.
- **Capture the action and the result.** The step log is the action. The artifact the feature produces is the result.
- **Check side effects.** The before and after `git status` in each step log shows what a command changed. A step that changes files nobody expected fails, even with the right exit code.
- **No mocks.** The upstream suites, the toolchain and the builds are all real.
- **Go tests must really run.** The runner passes `-count=1`. A suite log line ending in `(cached)` is not proof.
- **Logs are not results.** Wall times in logs are not benchmark numbers. CLAUDE.md requires every number in README.md to come from a notebook cell.

## Cleanup

```sh
bash .claude/skills/verify-vpir-bench/scripts/cleanup.sh
```

It removes any `patches/*/99-verify-*.patch` scaffolding and checks every submodule. A submodule whose patches neither apply nor reverse gets its edits saved first: tracked-file changes to `$VERIFY_RUN/<submodule>.restored.diff`, and untracked files, including those a patch adds, to `$VERIFY_RUN/<submodule>.restored-untracked.tgz`. Then it is reset to its pin. Finally it runs `make patch`, so the checkout ends at pin plus every patch. It never touches `logs/`. No recipe leaves a background process behind: `serve_session.sh` and the serve judge start servers and stop the ones they started. If you start one yourself, kill that PID, never a process name, because the adapters share binary names with other runs. Run `doctor.sh` after cleanup and require exit 0.

## Helpers

All four live in `.claude/skills/verify-vpir-bench/scripts/` and run from any directory inside the repo.

| Script | Use |
|---|---|
| `doctor.sh` | `bash $S/doctor.sh`. A read-only readiness check, exiting non-zero on any `FAIL`. |
| `step.sh` | `bash $S/step.sh [--expect N] <step-name> <command> [args...]`. Runs one step and logs it to `$VERIFY_RUN`. |
| `cleanup.sh` | `bash $S/cleanup.sh`. Restores pin plus patches and keeps `logs/`. |
| `serve_session.sh` | `bash $S/serve_session.sh ARM [QUERIES]`. Starts the arm's server(s), runs one client session that writes records and a corpus, and stops the servers with SIGTERM. Writes `$VERIFY_RUN/serve-<ARM>/`. |
