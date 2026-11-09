# Record the machine and toolchain

`make sysinfo` writes a JSON snapshot of the machine, OS, power state, toolchain and pinned upstream commits to `env/sysinfo.json`. It also writes the platform tag that every result row will carry to `env/reference_platform.txt`.

## Sub-features

- `sysinfo-files` writes both files and creates `env/` if needed.
- `sysinfo-platform` writes `arm64-darwin-Apple M1 Pro` on the reference machine.
- `sysinfo-upstream` lists each submodule's commit and whether its working tree differs from the pin (`dirty`).
- `sysinfo-notebook` prints the same snapshot from `notebooks/00_environment.ipynb` cell 1, the cell README.md cites for the machine facts.

## How to get to it (user POV)

- `make sysinfo`.
- `uv run python -m cs4406_sem_project.harness.sysinfo`, which prints the JSON to stdout.
- `notebooks/00_environment.ipynb` cell 1.

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0.

- **Write.** Run `bash $S/step.sh sysinfo make sysinfo`. Exit 0. The step log's state after shows `env/` as new (`?? env/`) or its files as modified.
- **Platform tag.** Run `bash $S/step.sh sysinfo-platform cat env/reference_platform.txt`. It prints `arm64-darwin-Apple M1 Pro`.
- **Upstream.** Run `bash $S/step.sh sysinfo-upstream uv run python -c "import json; u = json.load(open('env/sysinfo.json'))['upstream']; [print(k, v['sha'], v['dirty']) for k, v in sorted(u.items())]"`. It prints apir-code, dpf-go, simplepir and verisimplepir, each with its pin from the Makefile. `dirty` is `True` for the three submodules that have patches.
- **Notebook.** Run `bash $S/step.sh sysinfo-notebook uv run jupyter nbconvert --to notebook --execute notebooks/00_environment.ipynb --output-dir "$VERIFY_RUN"`. Exit 0. The output of cell 1 in `$VERIFY_RUN/00_environment.ipynb` starts with `arm64-darwin-Apple M1 Pro`.
- **Proof.** Run `cp env/sysinfo.json env/reference_platform.txt "$VERIFY_RUN/"`.

## Gotchas

- `env/` is not gitignored. A verification run that creates it leaves untracked files behind. Do not commit them from a verification run.
- Executing the notebook in place rewrites its committed outputs, including the timestamp and load average. Always pass `--output-dir "$VERIFY_RUN"`.
- simplepir has no patches, so its `dirty` should be `False`. `True` means someone edited it by hand.
- `power.source` comes from `pmset`. On battery it reads `Battery Power`. The spec requires AC power for measurement runs, and sysinfo records the power source without refusing to run.
