# Re-run the notebooks behind README claims

Every number in README.md cites a notebook cell (CLAUDE.md). Notebooks 02 to 06 cover the adapters. Notebooks 07 to 09 measure what the grid and RQ4 cost on this Mac. Notebooks 10 to 13 run RQ4 and analyse it. Re-running one reproduces its claim. The gate notebooks run the make targets. The other notebooks run adapters themselves, or call the RQ4 driver (`rq4-driver.md`). They keep their raw records, stderr and a `meta.json` (HEAD, a clean-tree flag and each binary's SHA-256) under `results/raw/<run_id>/`. Notebook 00 is in `sysinfo.md`, and 01 is in `upstream-tests.md` and `build.md`.

## Sub-features

- `nb-02` runs `make conformance` at the defaults and asserts G1 (about 70 minutes).
- `nb-03` runs `make recovery` at the defaults and asserts 159 of 159 (about 2 hours).
- `nb-04` reruns the M1 decode-guard finding (cell 2) and the adapters' peak memory footprints (cell 3), in about 20 minutes.
- `nb-05` builds instrumented adapters into `build/race/`, proves the Go race detector and ThreadSanitizer catch a deliberate race (cell 2), then loads every arm in serve mode for 60 s (cell 3), in about 15 minutes.
- `nb-06` times S4 at 64 KiB, 256 KiB and 1 MiB and extrapolates its grid cells (about 25 minutes). It refuses to start unless the Mac is on AC power with the lid open, and each run fails if the Mac slept or lost power (`harness/hygiene.py`).
- `nb-07` runs one `--phase all` repetition of every arm but S4 at 16 MiB, 128 MiB and 1 GiB under a 10 GiB cap. It writes per-cell time estimates to `results/raw/grid-feasibility-<UTC time>/estimates.jsonl` (about 1.6 hours, AC power and lid open).
- `nb-08` reruns the cells notebook 07 stopped at its cap, at 14 GiB, and reads swap before and after (about 30 minutes, AC power and lid open).
- `nb-09` prices RQ4 from notebook 07's estimates with the driver's `dry_run`, in seconds, and writes nothing. Cell 2 prints `spec as written ... 1115.5 h ( 46.5 days)`.
- `nb-10` runs the reduced RQ4 plan, 116 Runs, as run id `rq4-day-20261111` (6.8 hours of adapter time).
- `nb-11` merges the three RQ4 runs into the detection table, F12 and F13, and rewrites `docs/figures/F12_recovery_cost.png` and `F13_expected_cost.png`, in seconds.
- `nb-12` and `nb-13` rerun the Runs that commits `a053272` and `b353acc` changed, as run ids `rq4-rerun-20261111` and `rq4-rerun-s3-20261111`. They rebuild those Runs from notebook 10's `plan.jsonl`.
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
- **RQ4 plan.** Run `bash $S/step.sh nb-09 uv run jupyter nbconvert --to notebook --execute notebooks/09_rq4_plan.ipynb --output-dir "$VERIFY_RUN"`. Exit 0 in seconds. Cell 2 in `$VERIFY_RUN/09_rq4_plan.ipynb` prints `spec as written ... 1115.5 h ( 46.5 days)` and `spec's shrink ... 13.6 h`.
- **RQ4 analysis.** Run `shasum docs/figures/*.png > "$VERIFY_RUN/figures-before.sha"`, then `bash $S/step.sh nb-11 uv run jupyter nbconvert --to notebook --execute notebooks/11_rq4_analysis.ipynb --output-dir "$VERIFY_RUN"`. Exit 0 in seconds. Every cell's output equals the committed notebook's. Then run `shasum -c "$VERIFY_RUN/figures-before.sha"`. On 12 November both figures came out byte-identical, so it printed `OK` twice and `git status --short docs/figures` printed nothing. If they differ, restore them with `git checkout docs/figures`.
- **Keep the raw records out of git.** Run `bash $S/step.sh nb-raw bash -c 'for d in $(git ls-files --others --exclude-standard --directory results/raw); do mv "$d" "$VERIFY_RUN/"; done; git status --short results/'`. It prints nothing, so the run left no untracked results behind.

## Gotchas

- Always pass `--output-dir "$VERIFY_RUN"`. Executing in place rewrites the committed outputs that README.md cites.
- Every findings notebook writes a new `results/raw/<run_id>/`. A verification run's records are evidence, not results, so move them into `$VERIFY_RUN` and never commit them.
- The wall times the notebooks print are not measurements unless the run had the Mac to itself. Only notebook 06's timed spans are cited, and only from a run that passed its hygiene checks.
- `caffeinate` cannot stop clamshell sleep. A timing run with the lid closed sleeps, macOS's monotonic clock stops, and the timed spans look normal while the run takes hours. Notebook 06 checks for this and fails.
- Notebook 05 builds into `build/race/`, and a 60 s run per arm with S4's slow queries takes about 15 minutes in total. It refuses nothing, so do not run it beside notebook 06.
- Notebooks 02 and 03 run `make conformance` and `make recovery` at the defaults, which takes hours. `suites.md` drives the same targets at smoke counts.
- Do not re-execute notebooks 07, 08, 10, 12 or 13 to verify the skill.
  - Notebook 07 takes 1.6 hours and notebook 08 half an hour, and they push the Mac to 10 and 14 GiB.
  - Each run of 07 or 08 writes a new timestamped directory, while notebooks 08 and 09 read the committed `grid-feasibility-20261111T064956Z`.
  - Notebooks 10, 12 and 13 resume fixed run ids under `results/raw/`. Every Run in them is settled, so a re-run executes nothing and only rewrites the notebook's outputs. A Run that were somehow unsettled would execute at the current commit, but land under the first run's `meta.json` (`rq4-driver.md`, Gotchas).
  - To check the driver, use its own recipe instead.
- Notebooks 07, 08, 10, 12 and 13 refuse to start unless the Mac is on AC power with the lid open.
- The raw directories of the cited runs are tracked in git under `results/raw/`. Executing a notebook in place, as README.md's rerun command does, rewrites committed outputs, and a findings notebook adds a new untracked directory.
