#!/usr/bin/env bash
# Run one verification step from the repo root and record it as evidence.
#
#   export VERIFY_RUN=logs/verify/$(date -u +%Y%m%dT%H%M%SZ)
#   bash .claude/skills/verify-vpir-bench/scripts/step.sh [--expect N] <step-name> <command> [args...]
#
# Writes $VERIFY_RUN/<step-name>.log: the command, git state of the repo and every submodule
# before and after, combined output, exit code, and wall time. Exits 0 only when the command's
# exit code equals --expect (default 0).
set -uo pipefail

: "${VERIFY_RUN:?set VERIFY_RUN first, e.g. export VERIFY_RUN=logs/verify/\$(date -u +%Y%m%dT%H%M%SZ)}"
expect=0
if [ "${1:-}" = "--expect" ]; then expect=$2; shift 2; fi
[ $# -ge 2 ] || { echo "usage: step.sh [--expect N] <step-name> <command> [args...]" >&2; exit 64; }
name=$1; shift

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$repo"
mkdir -p "$VERIFY_RUN"
log="$VERIFY_RUN/$name.log"

state() {
	echo "## repo"; git status --short
	for d in third_party/*/; do
		[ -e "$d.git" ] || continue
		echo "## ${d%/}"; git -C "$d" status --short
	done
}

{
	echo "command: $*"
	echo "expect: $expect"
	echo "start: $(date -u +%FT%TZ)"
	echo "--- state before"; state
	echo "--- output"
} > "$log"
start=$(date +%s)
"$@" >> "$log" 2>&1
code=$?
{
	echo "--- exit: $code"
	echo "end: $(date -u +%FT%TZ) ($(( $(date +%s) - start )) s)"
	echo "--- state after"; state
} >> "$log"

if [ "$code" -eq "$expect" ]; then
	echo "ok       $name: exit $code ($log)"
	exit 0
fi
echo "MISMATCH $name: exit $code, expected $expect ($log)"
exit 1
