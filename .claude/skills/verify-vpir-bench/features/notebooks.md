# Re-run the notebooks behind README claims

Every number in README.md cites a notebook cell (CLAUDE.md). Notebooks 02 to 06 cover the adapters. Re-running one reproduces its claim from scratch: the gate notebooks run the make targets, and the findings notebooks run adapters themselves and keep their raw records, stderr and a `meta.json` (HEAD, a clean-tree flag and each binary's SHA-256) under `results/raw/<run_id>/`. Notebook 00 is in `sysinfo.md`, and 01 is in `upstream-tests.md` and `build.md`.

## Sub-features

- `nb-02` runs `make conformance` at the defaults and asserts G1 (about 70 minutes).
- `nb-03` runs `make recovery` at the defaults and asserts 159 of 159 (about 2 hours).
- `nb-04` reruns the M1 decode-guard finding (cell 2) and the adapters' peak memory footprints (cell 3), in about 20 minutes.
- `nb-05` builds instrumented adapters into `build/race/`, proves the Go race detector and ThreadSanitizer catch a deliberate race (cell 2), then loads every arm in serve mode for 60 s (cell 3), in about 15 minutes.
- `nb-06` times S4 at 64 KiB, 256 KiB and 1 MiB and extrapolates its grid cells (about 25 minutes). It refuses to start unless the Mac is on AC power with the lid open, and each run fails if the Mac slept or lost power (`harness/hygiene.py`).
- `nb-raw` gives each findings run its own `results/raw/<run_id>/`.

## How to get to it (user POV)

- Open the notebook and run all cells, or `uv run jupyter nbconvert --to notebook --execute notebooks/<name>.ipynb`.
- README.md's claims table names the notebook and cell behind each number.

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, `doctor.sh` exits 0, and `build/adapters/` is current (`make adapters`).
- Nothing else is running. Notebook 06 also needs AC power and the lid open, and the runs must not overlap each other.

- **Findings, memory.** Run `bash $S/step.sh nb-04 uv run jupyter nbconvert --to notebook --execute notebooks/04_p7_memory_findings.ipynb --output-dir "$VERIFY_RUN"`. Exit 0. In `$VERIFY_RUN/04_p7_memory_findings.ipynb`, cell 2 prints `decodes refused: 218` and `184 of 218` (deterministic from the seed), and cell 3 ends `largest: S3 honest, 0.2x GiB`. The footprint is sampled every 0.1 s, so the peak moves by a few hundredths of a GiB between runs (0.29 and 0.27 on 10 November).
- **Thread safety.** Run `bash $S/step.sh nb-05 uv run jupyter nbconvert --to notebook --execute notebooks/05_serve_thread_safety.ipynb --output-dir "$VERIFY_RUN"`. Exit 0. Cell 2 prints `True` for both canaries, and cell 3 prints `CLEAN` for all eight arms and `0 race reports; 0 arms not clean`.
- **S4 cost.** Only on AC power with the lid open. Run `bash $S/step.sh nb-06 uv run jupyter nbconvert --to notebook --execute notebooks/06_s4_cost.ipynb --output-dir "$VERIFY_RUN"`. Exit 0. Cell 2 prints three sizes, and cell 3 prints `offline no` for every size.
- **Keep the raw records out of git.** Run `bash $S/step.sh nb-raw bash -c 'for d in $(git ls-files --others --exclude-standard --directory results/raw); do mv "$d" "$VERIFY_RUN/"; done; git status --short results/'`. It prints nothing, so the run left no untracked results behind.

## Gotchas

- Always pass `--output-dir "$VERIFY_RUN"`. Executing in place rewrites the committed outputs that README.md cites.
- Every findings notebook writes a new `results/raw/<run_id>/`. A verification run's records are evidence, not results, so move them into `$VERIFY_RUN` and never commit them.
- The wall times the notebooks print are not measurements unless the run had the Mac to itself. Only notebook 06's timed spans are cited, and only from a run that passed its hygiene checks.
- `caffeinate` cannot stop clamshell sleep. A timing run with the lid closed sleeps, macOS's monotonic clock stops, and the timed spans look normal while the run takes hours. Notebook 06 checks for this and fails.
- Notebook 05 builds into `build/race/`, and a 60 s run per arm with S4's slow queries takes about 15 minutes in total. It refuses nothing, so do not run it beside notebook 06.
- Notebooks 02 and 03 run `make conformance` and `make recovery` at the defaults, which takes hours. `suites.md` drives the same targets at smoke counts.
