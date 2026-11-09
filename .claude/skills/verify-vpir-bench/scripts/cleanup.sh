#!/usr/bin/env bash
# Return the checkout to "pinned commit + every patch applied" after a verification run.
# Never touches logs/, which holds the evidence.
#
#   bash .claude/skills/verify-vpir-bench/scripts/cleanup.sh
set -euo pipefail

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$repo"
saved="${VERIFY_RUN:-logs/verify/cleanup-$(date -u +%Y%m%dT%H%M%SZ)}"

find patches -name '99-verify-*.patch' -print -delete | sed 's/^/removed scaffolding: /'

# A submodule is broken when one of its patches neither applies nor reverses. That happens only
# after a recipe edited it by hand and did not restore it. Save the edits, then reset to the pin.
for sub in patches/*/; do
	name="$(basename "$sub")"
	dir="third_party/$name"
	broken=0
	for p in "$sub"*.patch; do
		[ -e "$p" ] || continue
		git -C "$dir" apply --reverse --check "$repo/$p" 2>/dev/null \
			|| git -C "$dir" apply --check "$repo/$p" 2>/dev/null \
			|| broken=1
	done
	[ "$broken" -eq 1 ] || continue
	mkdir -p "$saved"
	git -C "$dir" diff > "$saved/$name.restored.diff"
	# Files a patch adds are untracked in the submodule, so git diff misses edits to them.
	git -C "$dir" ls-files -z --others --exclude-standard \
		| tar -C "$dir" --null -T - -czf "$saved/$name.restored-untracked.tgz"
	git -C "$dir" checkout -- .
	git -C "$dir" clean -fd -q
	echo "reset $dir to its pin; its edits are in $saved/$name.restored.*"
done

make -s patch | grep -v '^already applied' || true
echo "evidence kept in logs/verify/: $(ls logs/verify 2>/dev/null | tr '\n' ' ')"
