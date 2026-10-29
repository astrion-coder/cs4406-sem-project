# Build patches for third_party

Layout is `patches/<submodule>/NN-<desc>.patch`, for example `patches/apir-code/01-matrix-cgo-flags.patch`.

`make patch` applies them in lexical order with `git -C third_party/<submodule> apply`. A patch that is already applied is skipped. A patch that neither applies nor reverse-applies fails the build, which means upstream drifted from the pin.

Patches are build-only, per spec section 10 rule 1: x86-only flags, Darwin paths, removed Linux-only link flags, and include guards. Nothing that changes what the benchmarked code computes. The dpf-go arm64 helpers are the one crypto-adjacent exception (spec section 10 rule 1a): they use Go `crypto/aes` and must be bit-identical to the amd64 build.

Every patch gets one row below.

| Patch | What | Why | Affects performance? |
|---|---|---|---|
