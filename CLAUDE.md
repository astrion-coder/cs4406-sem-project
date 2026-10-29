# Reference Guide for Claude Code
This document contains the necessary information about the project that will allow Claude Code to help with the project.

# Important Files and Folders
1. `vpir_bench_spec.md` - This is the project we are planning to implement
2. `src/` - This is the directory where the main code of the project goes. Its layout follows spec §8.
3. `notebooks/` - This should contain only .ipynb files that are necessary for evaluation numbers. When you want to evaluate like the latency or some other kind of numeric value, place a quick script here and let me verify the numbers.
4. `Makefile` - `setup`, `patch`, `build`, `test`, `sysinfo` targets.
5. `third_party/` - the upstream repos, pinned as git submodules.
6. `patches/` - build-only patches applied to `third_party/` by `make patch`.

# Tech Stack
Python 3.11 via uv for the harness and analysis, Go and C++ adapters, and a Homebrew toolchain (go, llvm, openssl@3, iperf3).

# Other instructions
1. You are allowed to install software through official channels only. `uv add` commands are allowed but no `curl`/`wget`-ing random software from the internet.
2. Anytime a numeric claim is made, it should be referenced in README.md with the file and the cell number that indicates which cell the numeric claim was made from. I want to run that cell again to verify the claims.