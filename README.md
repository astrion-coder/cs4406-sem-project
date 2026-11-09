# CS4406 — The Price of Verifiability in PIR

The implementation spec is [vpir_bench_spec.md](./vpir_bench_spec.md). Code lives in `src/cs4406_sem_project/`, measurement notebooks in `notebooks/`.

## Quickstart

```sh
brew install go llvm openssl@3 iperf3
uv sync
make setup     # pins the upstream repos as submodules in third_party/ and downloads Go modules
make build     # applies patches/ and builds all upstream code
make test      # runs the upstream test suites, logs under logs/p0/
make sysinfo   # writes env/sysinfo.json and env/reference_platform.txt
```

## Layout

```
Makefile  README.md  CLAUDE.md  pyproject.toml
env/          hardware description and sysinfo output
third_party/  simplepir, verisimplepir, apir-code (pinned git submodules), PINS.md
patches/      build-only patches per submodule, README.md
configs/  results/  tests/  docs/  notebooks/  logs/ (gitignored)
src/
  cs4406_sem_project/   harness/, analysis/
  adapters/             simplepir_go/, apir_go/, vsp_cpp/
  loadgen/
```

## Numeric claims

Every number stated about this project must appear here with the notebook and cell that produced it. "Cell N" counts every cell in the notebook from 1, Markdown included; the execution count shown by Jupyter is given as `[exec M]`. Re-run with:
`uv run jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=-1 <notebook>`
(on AC power, with Low Power Mode off).

Measurement machine: Apple M1 Pro, 8 cores (6 performance + 2 efficiency), 17179869184 bytes (16 GiB) RAM, macOS 26.6.1, Python 3.11.17, Go 1.27.3, Homebrew clang 23.1.4, OpenSSL 3.5.9 ([00_environment.ipynb](./notebooks/00_environment.ipynb), cell 1 [exec 1]).

| Claim | Value | Source |
|---|---|---|
| Gate G0: every P0 upstream suite builds and passes natively on this Mac | 3 of 3 suites passed: simplepir `go test`, apir-code `go test` at `35cabd6` (`TestReal*` skipped, they need the SKS key dump), VeriSimplePIR `pir_test` | `01_g0_upstream_tests.ipynb`, cell 1 [exec 1] |

# Just for test
