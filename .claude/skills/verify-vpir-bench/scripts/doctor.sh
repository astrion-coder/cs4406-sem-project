#!/usr/bin/env bash
# Read-only check: is this checkout worth driving? Prints one ok/FAIL line per check and exits
# non-zero if any check fails.
#
#   bash .claude/skills/verify-vpir-bench/scripts/doctor.sh
set -u

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$repo"
fail=0

check() {
	local label=$1; shift
	if "$@" >/dev/null 2>&1; then echo "ok   $label"; else echo "FAIL $label"; fail=1; fi
}

check "macOS on arm64" test "$(uname -sm)" = "Darwin arm64"
check "go on PATH" command -v go
llvm="$(brew --prefix llvm 2>/dev/null)"
check "Homebrew LLVM clang ($llvm/bin/clang)" test -x "$llvm/bin/clang"
check "Homebrew openssl@3 headers" test -d "$(brew --prefix openssl@3 2>/dev/null)/include/openssl"
check ".venv runs Python 3.11" .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))'

# Pins come from the Makefile so this check never drifts from `make setup`.
while read -r name sha; do
	check "third_party/$name at pinned $sha" test "$(git -C "third_party/$name" rev-parse HEAD 2>/dev/null)" = "$sha"
done < <(sed -n 's/^[[:space:]]*\([a-z-]*\),\(https:[^,]*\),\([0-9a-f]*\).*/\1 \3/p' Makefile)

for p in patches/*/*.patch; do
	[ -e "$p" ] || continue
	sub="$(basename "$(dirname "$p")")"
	check "applied $p" git -C "third_party/$sub" apply --reverse --check "$repo/$p"
done

check "VeriSimplePIR pir_test is built" test -x third_party/verisimplepir/bin/demo/test/pir_test
check "libverisimplepir.dylib is arm64" sh -c 'file third_party/verisimplepir/bin/lib/libverisimplepir.dylib | grep -q arm64'

# The bracketed dots keep pgrep from matching this check's own command line.
check "no other test run is running" sh -c '! pgrep -f "cs4406_sem_project[.]harness[.]upstream_tests"'

exit $fail
