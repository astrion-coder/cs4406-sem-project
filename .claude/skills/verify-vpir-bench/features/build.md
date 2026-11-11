# Build the upstream code

`make build` applies the patches, then builds VeriSimplePIR's library and its demo, test and bench binaries with Homebrew LLVM clang++ and OpenSSL 3, then compiles simplepir's `pir` package and apir-code's `lib/client`, `lib/server` and `lib/database`, including their cgo C code, with Homebrew clang. Those are the apir-code packages that build at the pin under Go 1.27 (spec §7). Last it runs `make adapters`, which builds the three measurement adapters into `build/adapters/` (see `adapters.md`). Everything it produces is native arm64.

## Sub-features

- `build-patches-first` runs `make patch` before compiling anything.
- `build-vsp` produces `bin/lib/libverisimplepir.dylib` and the binaries under `bin/demo/test/` and `bin/demo/bench/` in `third_party/verisimplepir`, all arm64.
- `build-go` compiles `third_party/simplepir/pir/...` and apir-code's `./lib/client ./lib/server ./lib/database`.
- `build-adapters` ends by building `simplepir-adapter`, `apir-adapter` and `vsp-adapter` into `build/adapters/`, plus `vsp-adapter.sha`, the VeriSimplePIR commit the binary embeds.

## How to get to it (user POV)

- `make build`.
- `notebooks/01_g0_upstream_tests.ipynb` cell 1, which runs `make patch` and `make build` and then `make test` (about 3 more minutes).

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0.

- **Clean VeriSimplePIR.** Run `bash $S/step.sh build-clean make -C third_party/verisimplepir clean`. Exit 0. `third_party/verisimplepir/bin` no longer exists, and `bash $S/doctor.sh` now fails on `pir_test` and on the dylib.
- **Build.** Run `bash $S/step.sh build make build`. Exit 0. The output starts with five `make patch` lines, all `already applied:`, followed by VeriSimplePIR's compile and link commands, the two `go build` commands, and the adapter builds: two `go build ... -o .../build/adapters/<name>-adapter` lines, the `clang++ ... vsp_adapter.cpp` line and `install_name_tool`. No line contains `error:`.
- **Architecture.** Run `bash $S/step.sh build-arch file third_party/verisimplepir/bin/lib/libverisimplepir.dylib third_party/verisimplepir/bin/demo/test/pir_test`. Both lines say `arm64`.
- **Binaries.** Run `bash $S/step.sh build-bins ls third_party/verisimplepir/bin/demo/bench third_party/verisimplepir/bin/demo/test`. The bench directory has `batch_pir_bench`, `pir_bench`, `preproc_pir_bench`, `proof_bench`, `simplepir_bench` and `verify_bench`. The test directory has `pir_test`, `crt_test`, `database_test`, `lhe_test`, `mat_packed_test` and `mat_test`.
- **Adapters.** Run `bash $S/step.sh build-adapters bash -c 'file build/adapters/simplepir-adapter build/adapters/apir-adapter build/adapters/vsp-adapter && test "$(cat build/adapters/vsp-adapter.sha)" = "$(git -C third_party/verisimplepir rev-parse HEAD)"'`. Exit 0. All three are `Mach-O 64-bit executable arm64`, and `vsp-adapter.sha` holds the VeriSimplePIR pin.
- **Ready again.** Run `bash $S/doctor.sh`. It exits 0.

## Gotchas

- VeriSimplePIR prints about 100 compiler and linker warnings: `-Wnontrivial-memcall`, deprecated OpenSSL `SHA256_*` calls, unused variables, and `-undefined suppress is deprecated`. Only `error:` lines are failures.
- The build uses Homebrew LLVM from `brew --prefix llvm`. Do not pass `CC=/usr/bin/clang`; the project uses one compiler for all three codebases.
- `go build ./...` writes no binaries for library packages, and an unchanged tree rebuilds from Go's cache in seconds. Exit 0 proves the packages compile. It does not prove they were recompiled.
- The `clean` step removes `bin/`, so the VeriSimplePIR test suite reports `not_built` until `make build` finishes. It also leaves `build/adapters/vsp-adapter` pointing at a missing dylib. `make build` relinks it, because the adapter's Makefile depends on the dylib.
- The two Go adapters rebuild from Go's cache in seconds on every `make build`, like the upstream packages.
