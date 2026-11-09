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

- **Unapply dpf-go.** Reverse dpf-go's two patches, newest first. Run `bash $S/step.sh patch-unapply bash -c 'git -C third_party/dpf-go apply --reverse "$PWD/patches/dpf-go/02-arm64-aes-helpers.patch" && git -C third_party/dpf-go apply --reverse "$PWD/patches/dpf-go/01-go-mod.patch"'`. Exit 0. The step log's state after shows `third_party/dpf-go` with no changes. `bash $S/doctor.sh` now prints `FAIL` for both dpf-go patches.
- **Apply.** Run `bash $S/step.sh patch-apply make patch`. Exit 0. The output has `applied: patches/dpf-go/01-go-mod.patch` and `applied: patches/dpf-go/02-arm64-aes-helpers.patch`, and `already applied:` for every other patch. The state after shows `M dpf/aes.go`, `?? dpf/aes_other.go` and `?? go.mod` under `third_party/dpf-go`.
- **Repeat.** Run `bash $S/step.sh patch-skip make patch`. Exit 0. Every patch line is `already applied:`, and the state before and after are identical.
- **Drift.** Add a scaffolding patch that cannot apply. Run `printf 'diff --git a/dpf/dpf.go b/dpf/dpf.go\n--- a/dpf/dpf.go\n+++ b/dpf/dpf.go\n@@ -1,1 +1,1 @@\n-verification scaffolding, not a line in dpf.go\n+verification scaffolding\n' > patches/dpf-go/99-verify-drift.patch`. Then run `bash $S/step.sh --expect 2 patch-drift make patch`. Make exits 2, and the output contains `PATCH FAILED (upstream drift?): patches/dpf-go/99-verify-drift.patch neither applies nor reverse-applies to third_party/dpf-go`.
- **Restore.** Run `rm patches/dpf-go/99-verify-drift.patch`, then `bash $S/doctor.sh`. Doctor exits 0.

## Gotchas

- Patches change the submodule working trees, so `git status` in the repo root shows `m third_party/<submodule>`. That is the expected state. Never commit inside a submodule.
- Each patch edits one file, so each patch's "already applied" check stands alone. A new patch that edits a file another patch already edits breaks that check. Merge the two instead (see the `block.h` row in `patches/README.md`).
- While dpf-go's patches are reversed, apir-code does not build. Re-apply them before any other recipe.
- `make patch` does not rebuild. Run `make build` before testing a patch change.
- `doctor.sh` fails while any `99-verify-*.patch` is present, which is the signal that a drift step was not restored. `cleanup.sh` deletes them.
