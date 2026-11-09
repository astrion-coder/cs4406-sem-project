# Apply build patches

`make patch` brings each submodule in `third_party/` from its pinned commit to the patched state the project builds. It applies `patches/<submodule>/NN-*.patch` in order, skips a patch that is already applied, and stops loudly when a patch no longer fits the code.

## Sub-features

- `patch-apply` applies a patch that is not applied yet and prints `applied: <path>`.
- `patch-skip` skips a patch that is already applied and prints `already applied: <path>`.
- `patch-drift` stops with `PATCH FAILED (upstream drift?)` when a patch neither applies nor reverses, and `make` exits 2.

## How to get to it (user POV)

- `make patch`.
- `make build`, which runs `make patch` first.

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0, so every patch starts applied.

- **Unapply.** Reverse the apir-code patch. Run `bash $S/step.sh patch-unapply bash -c 'git -C third_party/apir-code apply --reverse "$PWD/patches/apir-code/01-matrix-cgo-flags.patch"'`. Exit 0. The step log's state after shows `third_party/apir-code` with no changes. `bash $S/doctor.sh` now prints `FAIL` for the patch.
- **Apply.** Run `bash $S/step.sh patch-apply make patch`. Exit 0. The output has `applied: patches/apir-code/01-matrix-cgo-flags.patch` and `already applied:` for every other patch. The state after shows `M lib/matrix/matrix.go` and `M lib/matrix/matrix128.go` under `third_party/apir-code`.
- **Repeat.** Run `bash $S/step.sh patch-skip make patch`. Exit 0. Every patch line is `already applied:`, and the state before and after are identical.
- **Drift.** Add a scaffolding patch that cannot apply. Run `printf 'diff --git a/lib/matrix/matrix.c b/lib/matrix/matrix.c\n--- a/lib/matrix/matrix.c\n+++ b/lib/matrix/matrix.c\n@@ -1,1 +1,1 @@\n-verification scaffolding, not a line in matrix.c\n+verification scaffolding\n' > patches/apir-code/99-verify-drift.patch`. Then run `bash $S/step.sh --expect 2 patch-drift make patch`. Make exits 2, and the output contains `PATCH FAILED (upstream drift?): patches/apir-code/99-verify-drift.patch neither applies nor reverse-applies to third_party/apir-code`.
- **Restore.** Run `rm patches/apir-code/99-verify-drift.patch`, then `bash $S/doctor.sh`. Doctor exits 0.

## Gotchas

- Patches change the submodule working trees, so `git status` in the repo root shows `m third_party/<submodule>`. That is the expected state. Never commit inside a submodule.
- Each patch edits one file, so each patch's "already applied" check stands alone. A new patch that edits a file another patch already edits breaks that check. Merge the two instead (see the `block.h` row in `patches/README.md`).
- While the apir-code patch is reversed, apir-code does not build on arm64, because arm64 clang rejects the x86 cgo flags. Run `make patch` before any other recipe.
- `make patch` does not rebuild. Run `make build` before testing a patch change.
- `doctor.sh` fails while any `99-verify-*.patch` is present, which is the signal that a drift step was not restored. `cleanup.sh` deletes them.
