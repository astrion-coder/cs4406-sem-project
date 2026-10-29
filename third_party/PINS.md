# Pinned upstream repositories

| name | URL | SHA | pinned on |
|---|---|---|---|
| simplepir | https://github.com/ahenzinger/simplepir | e9020b03bf2872c75b8954e749e32408b5db87ed | 2026-10-30 |
| verisimplepir | https://github.com/leodec/verisimplepir | 3643bb7cbaae02da98a195c4d004d4d083e3ab88 | 2026-10-30 |
| apir-code | https://github.com/dedis/apir-code | 7a7f2041bc1e0907ef089f8e8e55252ef7f18e0f | 2026-10-30 |
| dpf-go | https://github.com/dkales/dpf-go | 6eae873488482fc6f01307d55f74b33862de6840 (the commit in apir-code's `go.mod` pseudo-version `v0.0.0-20210304170054-6eae87348848`) | 2026-10-30 |

## Updating a pin

1. Change the SHA in the `PINS` list in the `Makefile` and in the table above.
2. Run `git -C third_party/<name> fetch && git -C third_party/<name> checkout <sha>`, then `git add third_party/<name>`.
3. Run `make patch`. A patch that no longer applies fails loudly and must be regenerated against the new SHA.
4. Run `make build test` and update `patches/README.md` if the patch set changed.
