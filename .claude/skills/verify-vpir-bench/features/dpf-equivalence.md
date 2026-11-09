# Check the dpf-go arm64 port

`check.sh` shows that the arm64 port of dpf-go's AES helpers computes what the original x86 assembly computes. It builds an x86-64 binary, which runs under Rosetta 2, and a native arm64 binary. It generates DPF keys on each, evaluates them on the other, and passes only if every output matches bit for bit in both directions.

## Sub-features

- `dpf-cross` checks keys made on amd64 against arm64, and keys made on arm64 against amd64.
- `dpf-semantics` checks each instance's DPF property: the two parties' full evaluations XOR to a single 1 bit at the chosen index.
- `dpf-negative` fails when the port computes a wrong output.

## How to get to it (user POV)

- `bash src/adapters/apir_go/tools/dpfvectors/check.sh` from the repo root.
- `make test ONLY=dpf-equivalence`.

## Driving it with step.sh

Preconditions:

- The baseline in `README.md` holds, and `doctor.sh` exits 0, which includes the dpf-go pin, both dpf-go patches and Rosetta 2.

- **Pass.** Run `bash $S/step.sh dpf-pass bash src/adapters/apir_go/tools/dpfvectors/check.sh`. Exit 0. The output has `gen: GOARCH=amd64 wrote 66 vectors ...`, `check: GOARCH=arm64 matched all 66 vectors from GOARCH=amd64`, the same two lines with the architectures swapped, and `dpf-equivalence: PASS (amd64->arm64 and arm64->amd64 outputs bit-identical)`.
- **Break the port.** Save the file, then flip one bit of the port's output. Run `cp third_party/dpf-go/dpf/aes_other.go "$VERIFY_RUN/aes_other.go.orig"` and `sed -i '' 's/binary.LittleEndian.Uint64(d\[:8\])^lo)/binary.LittleEndian.Uint64(d[:8])^lo^1)/' third_party/dpf-go/dpf/aes_other.go`. Then `grep -c 'lo^1)' third_party/dpf-go/dpf/aes_other.go` prints `1`.
- **Fail.** Run `bash $S/step.sh --expect 1 dpf-negative bash src/adapters/apir_go/tools/dpfvectors/check.sh`. Exit 1. The output has a line ending `bits set, want only bit alpha` and ends with `dpf-equivalence: FAIL`.
- **Restore.** Run `cp "$VERIFY_RUN/aes_other.go.orig" third_party/dpf-go/dpf/aes_other.go`, then `bash $S/doctor.sh`. Doctor exits 0. Run the **Pass** step again and require `PASS`.

## Gotchas

- The amd64 binary runs the upstream assembly, so it is the reference. Without Rosetta 2 the script fails at the first amd64 run.
- The script sets `GOPROXY=off` and builds the amd64 binary with `CGO_ENABLED=0`, so it needs no network and no x86 C compiler.
- A sed that deletes the XOR outright leaves the variable `lo` unused, and Go refuses to compile. That is a build failure, not a negative control. Use the one-bit flip above.
- Keys come from `crypto/rand`, so the vector files differ from run to run. Compare verdicts, not files.
- If the restore step is skipped, `cleanup.sh` resets dpf-go to its pin, saves the edit to `$VERIFY_RUN`, and re-applies its patches.
