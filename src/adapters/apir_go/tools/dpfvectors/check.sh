#!/usr/bin/env bash
# Cross-architecture check for the dpf-go arm64 port (spec section 10 rule 1a).
# amd64 runs the upstream AES-NI assembly under Rosetta 2; arm64 runs aes_other.go.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export GOTOOLCHAIN=local GOPROXY=off GOOS=darwin
(cd "$here" && GOARCH=amd64 CGO_ENABLED=0 go build -o "$tmp/dpfvectors-amd64" .)
(cd "$here" && GOARCH=arm64 go build -o "$tmp/dpfvectors-arm64" .)

file "$tmp/dpfvectors-amd64" | grep -q x86_64 || { echo "dpf-equivalence: FAIL (amd64 binary is not x86_64)"; exit 1; }
file "$tmp/dpfvectors-arm64" | grep -q arm64 || { echo "dpf-equivalence: FAIL (arm64 binary is not arm64)"; exit 1; }

status=0
"$tmp/dpfvectors-amd64" gen "$tmp/amd64.json" && "$tmp/dpfvectors-arm64" check "$tmp/amd64.json" || status=1
"$tmp/dpfvectors-arm64" gen "$tmp/arm64.json" && "$tmp/dpfvectors-amd64" check "$tmp/arm64.json" || status=1

if [ "$status" -eq 0 ]; then
  echo "dpf-equivalence: PASS (amd64->arm64 and arm64->amd64 outputs bit-identical)"
else
  echo "dpf-equivalence: FAIL"
fi
exit "$status"
