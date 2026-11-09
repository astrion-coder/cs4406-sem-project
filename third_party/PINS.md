# Pinned upstream repositories

| name | URL | SHA | pinned on |
|---|---|---|---|
| simplepir | https://github.com/ahenzinger/simplepir | e9020b03bf2872c75b8954e749e32408b5db87ed | 2026-10-30 |
| verisimplepir | https://github.com/leodec/verisimplepir | 3643bb7cbaae02da98a195c4d004d4d083e3ab88 | 2026-10-30 |
| apir-code | https://github.com/dedis/apir-code | 35cabd62fb898926f5bea6035caeae39091e091d (2024-10-15 commit, before DPF queries were added, so the two-server client sends plain XOR vectors as in the paper) | 2026-11-09 |

## Updating a pin

1. Change the SHA in the `PINS` list in the `Makefile` and in the table above.
2. Run `git -C third_party/<name> fetch && git -C third_party/<name> checkout <sha>`, then `git add third_party/<name>`.
3. Run `make patch`. A patch that no longer applies fails loudly and must be regenerated against the new SHA.
4. Run `make build test` and update `patches/README.md` if the patch set changed.
