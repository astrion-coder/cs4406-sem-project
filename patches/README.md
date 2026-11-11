# Build patches for third_party

Layout is `patches/<submodule>/NN-<desc>.patch`, for example `patches/apir-code/01-matrix-cgo-flags.patch`.

`make patch` applies them in lexical order with `git -C third_party/<submodule> apply`. A patch that is already applied is skipped. A patch that neither applies nor reverse-applies fails the build, which means upstream drifted from the pin.

Patches are build-only, per spec section 10 rule 1: x86-only flags, Darwin paths, removed Linux-only link flags, and include guards. Nothing that changes what the benchmarked code computes. Spec section 10 rule 1a no longer applies: apir-code is pinned before the DPF commit, so nothing imports dpf-go and no crypto-adjacent patch remains.

Three VeriSimplePIR fixes go beyond the rule 1 list: a missing standard include, a guard around an unused `__float128` typedef, and two `BigUnsigned` constructors. They are macOS portability fixes that do not change what the code computes, and they are marked below as needing human approval. Without them VeriSimplePIR does not build on macOS.

No two patches edit the same file, so each patch can be checked for "already applied" on its own. A patch may edit several files: `apir-code/01` edits `matrix.go` and `matrix128.go`, and `verisimplepir/04` edits `BigUnsigned.cpp` and `BigUnsigned.h`. A new change to a file that a patch already edits goes into that patch. Every patch gets one row below.

| Patch | What | Why | Affects performance? |
|---|---|---|---|
| `apir-code/01-matrix-cgo-flags.patch` | Splits the cgo line in `lib/matrix/matrix.go` and `matrix128.go` into `-std=c99 -O3 -march=native` for all targets and `-msse4.1 -maes -mavx2 -mavx` for amd64 only. | arm64 clang rejects the x86 flags. `matrix.c` is plain C. | amd64: no, same flags. arm64: the C builds with `-O3 -march=native` only. |
| `verisimplepir/01-darwin-arm64-makefile.patch` | Darwin branch takes clang++ and the rpath from `LLVM_PREFIX ?= $(shell brew --prefix llvm)`, adds OpenSSL `-I`/`-L` from `OPENSSL_PREFIX ?= $(shell brew --prefix openssl@3)`, drops `-lrt` on Darwin, and keeps `-maes -msse4` only when `uname -m` is x86. | The Darwin branch hard-coded Intel Homebrew `/usr/local/opt/llvm`. macOS has no `librt`. arm64 clang rejects the x86 flags. | No. `-O3` is unchanged. |
| `verisimplepir/02-block-h-arm64-portability.patch` | In `src/lib/math/block.h`, guards `<emmintrin.h>` and `<smmintrin.h>` with `#ifdef OC_ENABLE_SSE2` and adds `#include <type_traits>`. **The include is outside the rule 1 list and needs human approval.** | clang rejects the x86 headers on arm64. libc++ 23 no longer includes `<type_traits>` transitively, so `std::is_pod` failed. | No. On arm64 cryptoTools already selects its portable `block`. AES is the portable implementation on every platform. |
| `verisimplepir/03-backend-h-float128-guard.patch` | Wraps the unused `fl128` and `fv128` typedefs in `src/lib/math/backend.h` in `#if defined(__SIZEOF_FLOAT128__)`. **Outside the rule 1 list, needs human approval.** | arm64 Darwin has no `__float128`. Nothing else in the repo uses the typedefs. x86-64 Linux defines `__SIZEOF_FLOAT128__`, so it is unchanged there. | No. |
| `verisimplepir/04-biguint-long-long-ctors.patch` | Adds `BigUnsigned(unsigned long long)` and `BigUnsigned(long long)`, which call the same `initFromPrimitive` and `initFromSignedPrimitive` templates as the `unsigned long` and `long` constructors. **Outside the rule 1 list, needs human approval.** | macOS defines `uint64_t` as `unsigned long long`, so every `entry_t(uint64_t)` was ambiguous. Linux defines it as `unsigned long`. Both are 64 bits, so the value stored is the same. | No. |
