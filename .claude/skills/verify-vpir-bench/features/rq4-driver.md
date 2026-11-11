# Run the RQ4 recovery experiment

`harness/recovery.py` expands `configs/recovery_policies.yaml` into Runs and executes them one at a time. A Run is one adapter process: an arm at one cell, with one fault, policy and fault rate. Each Run goes through `caffeinate -dims` and the memory cap (`memory_cap_gib`, 14) under a hygiene watch. Each one appends a line to `manifest.jsonl` whether it succeeds or not. A run writes everything to `<raw-dir>/<run-id>/`:
- `meta.json` holds the commit, a clean-tree flag over `src`, `Makefile` and `configs`, the citable flag, each adapter's SHA-256 and a sysinfo snapshot.
- `manifest.jsonl` holds one line per attempt, with its outcome (`ok`, `oom`, `timeout` or `failed`) and whether it is settled.
- Each Run gets its own `<name>.jsonl` and `<name>.stderr.log`.

Notebooks 09, 10, 12 and 13 use it as a library.

## Sub-features

- `rq4-unit` runs `tests/test_recovery_driver.py`: planning, resume, the timeout rule, dry-run arithmetic, and one live smoke through `apir-adapter`.
- `rq4-plan` prints a plan's Run and repetition counts per kind and arm with `--dry-run`, and runs nothing. With `--estimates <notebook 07 estimates.jsonl>` it also prices the plan in hours.
- `rq4-run` executes the plan serially. It prints `[i/N] ok <name> (<s> s)` per Run and `N of N Runs settled (complete, oom or timeout)` at the end, and exits 0 only when every Run is settled.
- `rq4-resume` reuses a run id. It skips settled Runs with `skip (settled)` and reruns a failed one, renaming that Run's partial output to `<name>.failed-<k>.*`. `oom` and `timeout` count as settled, so they are never retried under the same id.
- `rq4-refuse` exits 1 before any Run starts in four cases:
  - an adapter binary is missing;
  - the run id's `meta.json` has the other citable flag;
  - the Mac is on battery, unless `--allow-unclean` is given;
  - the lid is closed, unless `--allow-unclean` is given.
- `rq4-unclean` is `--allow-unclean`. It skips the power and lid check, ignores hygiene problems during the run, and writes `"citable": false`.

## How to get to it (user POV)

- `uv run python -m cs4406_sem_project.harness.recovery [--config FILE] [--run-id ID] [--only ARM ...] [--kinds trials,offline,sweep] [--trials N] [--queries N] [--db-bytes N ...] [--record-bytes N ...] [--adapter-dir DIR] [--raw-dir DIR] [--dry-run [--estimates FILE]] [--allow-unclean]`. The defaults are `configs/recovery_policies.yaml`, `build/adapters`, `results/raw` and the run id `rq4-<UTC time>`.
- As a library, with `load_config`, `plan`, `prepare(runs, cfg, config_path, run_id=...)` and `run_and_report(runs, ex)`. Notebooks 10, 12 and 13 run it this way, and notebook 09 prices plans with `dry_run` and `expected_run_s` (see `notebooks.md`).

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, `doctor.sh` exits 0, and `build/adapters/` is current (`make adapters`).
- The Mac is on AC power with the lid open. Otherwise `rq4-run` refuses, and the smoke variant needs `--allow-unclean`.
- Nothing else is running. Runs are timing runs.

- **Unit.** Run `bash $S/step.sh rq4-unit uv run pytest tests/test_recovery_driver.py -q -p no:cacheprovider`. Exit 0, with `14 passed` in about 4 s.
- **Plan.** Run `bash $S/step.sh rq4-plan uv run python -m cs4406_sem_project.harness.recovery --only M0 --kinds trials --trials 2 --db-bytes 1048576 --record-bytes 32 --dry-run`. Exit 0. It prints `4 Runs, 12 adapter repetitions (reps + warmup per Run, summed)` and a `trials   M0   4   12` row.
- **Run.** Run `bash $S/step.sh rq4-run uv run python -m cs4406_sem_project.harness.recovery --only M0 --kinds trials --trials 2 --db-bytes 1048576 --record-bytes 32 --raw-dir "$VERIFY_RUN/raw" --run-id verify-rq4`. Exit 0 within seconds. It prints `run verify-rq4: 4 Runs into <dir>`, four `[i/4] ok trials-M0-db1048576-rb32-<fault>-abort-p1` lines for `flip_answer_bit`, `wrong_record`, `stale_record` and `drop_server`, and `4 of 4 Runs settled (complete, oom or timeout)`.
- **Files.** Run `bash $S/step.sh rq4-files uv run python -c "import json,sys; d=sys.argv[1]; m=json.load(open(d+'/meta.json')); ms=[json.loads(l) for l in open(d+'/manifest.jsonl')]; print(m['citable'], m['dirty'], len(ms), all(r['outcome']=='ok' and r['settled'] and r['records']==3 and not r['hygiene_problems'] for r in ms))" "$VERIFY_RUN/raw/verify-rq4"`. It prints `True False 4 True`. `dirty` is `False` only when `src`, `Makefile` and `configs` have no uncommitted change.
- **Resume.** Run the **Run** command again with step name `rq4-resume`. Exit 0. It prints four `skip (settled)` lines and `4 of 4 Runs settled (complete, oom or timeout) in 0 s`. `manifest.jsonl` still has 4 lines, and no `.failed-` file appears.
- **Refuse, citable flag.** Run the **Run** command with `--allow-unclean` added, through `bash $S/step.sh --expect 1 rq4-unclean-mismatch ...`. It prints `<dir>/meta.json was written by a run with allow_unclean=False; use a new --run-id`.
- **Refuse, binary.** Run `mkdir -p "$VERIFY_RUN/none"`, then the **Run** command with `--run-id verify-rq4b --adapter-dir "$VERIFY_RUN/none"`, through `bash $S/step.sh --expect 1 rq4-nobin ...`. It prints `missing adapter binaries in <dir>/none: ['apir-adapter']`, and no `verify-rq4b` directory appears.
- **Refuse, power.** This step is reachable only on battery. Run the **Run** command with a new run id and no `--allow-unclean`, through `--expect 1`. It prints `plug the Mac in first (Battery Power)`. A closed lid prints `open the lid first: a closed lid sleeps the Mac despite caffeinate`. Neither was driven on 12 November, because the Mac was on AC power with the lid open.

## Gotchas

- Always pass `--raw-dir "$VERIFY_RUN/raw"`. The default is `results/raw/`, where the cited RQ4 runs are tracked in git.
- Never resume a cited run id (`rq4-day-20261111`, `rq4-rerun-20261111`, `rq4-rerun-s3-20261111`) at a later commit. `write_meta` keeps the first `meta.json`, so a Run the resume executes would carry the first run's commit and adapter hashes.
- The driver records a dirty tree but does not refuse it. Notebook 11 refuses any run whose `meta.json` says `dirty: true` or `citable: false`.
- To retry an `oom` or `timeout` Run, use a new run id. The per-Run timeout is `timeout_s`, 14400 s.
- `--dry-run --estimates` gives the expected time and an upper bound, both from notebook 07's single repetitions. Neither counts untimed fault-injection work, such as the shadow query behind each `wrong_record` cheat, which doubles an f = 1 sweep. Neither counts `re_preprocess`'s spread over its queries either. The notebook 10 run ended 74 minutes after its estimate for these reasons.
- The notebooks write `plan.jsonl`, not the driver. Notebooks 12 and 13 rebuild their Runs from notebook 10's `plan.jsonl`.
- In zsh an unquoted variable holding several flags stays one argument, and argparse rejects it. Write the flags out, as above.
