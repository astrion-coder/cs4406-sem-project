# Build the upstream code

`make build` applies the patches, then builds VeriSimplePIR's library and its demo, test and bench binaries with Homebrew LLVM clang++ and OpenSSL 3, then compiles every simplepir and apir-code package, including their cgo C code, with Homebrew clang. Everything it produces is native arm64.

## Sub-features

- `build-patches-first` runs `make patch` before compiling anything.
- `build-vsp` produces `bin/lib/libverisimplepir.dylib` and the binaries under `bin/demo/test/` and `bin/demo/bench/` in `third_party/verisimplepir`, all arm64.
- `build-go` compiles every package in `third_party/simplepir/pir` and `third_party/apir-code`.

## How to get to it (user POV)

- `make build`.
- `notebooks/01_g0_upstream_tests.ipynb` cell 1.

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0.

- **Clean VeriSimplePIR.** Run `bash $S/step.sh build-clean make -C third_party/verisimplepir clean`. Exit 0. `third_party/verisimplepir/bin` no longer exists, and `bash $S/doctor.sh` now fails on `pir_test`.
- **Build.** Run `bash $S/step.sh build make build`. Exit 0. The output starts with the `make patch` lines, all `already applied:`, followed by VeriSimplePIR's compile and link commands. No line contains `error:`.
- **Architecture.** Run `bash $S/step.sh build-arch file third_party/verisimplepir/bin/lib/libverisimplepir.dylib third_party/verisimplepir/bin/demo/test/pir_test`. Both lines say `arm64`.
- **Binaries.** Run `bash $S/step.sh build-bins ls third_party/verisimplepir/bin/demo/bench third_party/verisimplepir/bin/demo/test`. The bench directory has `batch_pir_bench`, `pir_bench`, `preproc_pir_bench`, `proof_bench`, `simplepir_bench` and `verify_bench`. The test directory has `pir_test`, `crt_test`, `database_test`, `lhe_test`, `mat_packed_test` and `mat_test`.
- **Ready again.** Run `bash $S/doctor.sh`. It exits 0.

## Gotchas

- VeriSimplePIR prints about 100 compiler and linker warnings: `-Wnontrivial-memcall`, deprecated OpenSSL `SHA256_*` calls, unused variables, and `-undefined suppress is deprecated`. Only `error:` lines are failures.
- The build uses Homebrew LLVM from `brew --prefix llvm`. Do not pass `CC=/usr/bin/clang`; the project uses one compiler for all three codebases.
- `go build ./...` writes no binaries for library packages, and an unchanged tree rebuilds from Go's cache in seconds. Exit 0 proves the packages compile. It does not prove they were recompiled.
- The `clean` step removes `bin/`, so the VeriSimplePIR test suite reports `not_built` until `make build` finishes.
