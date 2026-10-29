"""Machine and toolchain snapshot for every measurement run (macOS only).

Data shape: `snapshot() -> dict`, JSON-serialisable, with sections
  machine    cpu_model, arch, physical_cpus, performance_cores, efficiency_cores, mem_total_bytes (required)
  os         version, build, kernel
  power      source, low_power_mode
  load       loadavg_1m, mem_available_bytes
  toolchain  go, clang, openssl, python, uv
  upstream   {name: {"sha": str, "dirty": bool}} per git checkout under <repo>/third_party/
  thread_env {var: value or None} for THREAD_ENV_VARS
  captured_at  UTC ISO-8601
Optional fields whose command fails are None. Required machine fields raise RuntimeError.
"""

import argparse
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
    "RAYON_NUM_THREADS",
)
REPO_ROOT = Path(__file__).resolve().parents[3]


def _run(cmd: list[str], cwd: Path | None = None) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _sysctl(key: str) -> str | None:
    return _run(["sysctl", "-n", key])


def _required_sysctl(key: str) -> str:
    value = _sysctl(key)
    if value is None:
        raise RuntimeError(f"cannot read required machine fact `sysctl -n {key}` (is this Apple Silicon macOS?)")
    return value


def apply_thread_limits(threads: int) -> None:
    """Cap intra-op threads. Set in the parent before workers are spawned so they inherit it."""
    for var in THREAD_ENV_VARS:
        os.environ[var] = str(threads)


def _machine() -> dict:
    return {
        "cpu_model": _required_sysctl("machdep.cpu.brand_string"),
        "arch": platform.machine(),
        "physical_cpus": int(_required_sysctl("hw.physicalcpu")),
        "performance_cores": int(_required_sysctl("hw.perflevel0.physicalcpu")),
        "efficiency_cores": int(_required_sysctl("hw.perflevel1.physicalcpu")),
        "mem_total_bytes": int(_required_sysctl("hw.memsize")),
    }


def _os() -> dict:
    return {
        "version": _run(["sw_vers", "-productVersion"]),
        "build": _run(["sw_vers", "-buildVersion"]),
        "kernel": platform.release(),
    }


def _power() -> dict:
    batt = _run(["pmset", "-g", "batt"])
    source = None
    if batt:
        first = batt.splitlines()[0]
        if "'" in first:
            source = first.split("'")[1]
    low_power = None
    for line in (_run(["pmset", "-g"]) or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "lowpowermode":
            low_power = parts[1] == "1"
    return {"source": source, "low_power_mode": low_power}


def _load() -> dict:
    import psutil

    return {"loadavg_1m": os.getloadavg()[0], "mem_available_bytes": psutil.virtual_memory().available}


def _brew_prefix(formula: str) -> str | None:
    return _run(["brew", "--prefix", formula])


def _first_line(text: str | None) -> str | None:
    return text.splitlines()[0] if text else None


def _clang() -> str | None:
    cc = os.environ.get("CC")
    if cc:
        return _first_line(_run([cc, "--version"]))
    llvm = _brew_prefix("llvm")
    return _first_line(_run([f"{llvm}/bin/clang", "--version"])) if llvm else None


def _openssl() -> str | None:
    prefix = _brew_prefix("openssl@3")
    return _run([f"{prefix}/bin/openssl", "version"]) if prefix else None


def _toolchain() -> dict:
    return {
        "go": _run(["go", "version"]),
        "clang": _clang(),
        "openssl": _openssl(),
        "python": platform.python_version(),
        "uv": _run(["uv", "--version"]),
    }


def _upstream(repo_root: Path) -> dict:
    third_party = repo_root / "third_party"
    if not third_party.is_dir():
        return {}
    out = {}
    for d in sorted(third_party.iterdir()):
        if not (d / ".git").exists():
            continue
        porcelain = _run(["git", "status", "--porcelain"], cwd=d)
        out[d.name] = {
            "sha": _run(["git", "rev-parse", "HEAD"], cwd=d),
            "dirty": None if porcelain is None else bool(porcelain),
        }
    return out


def snapshot(repo_root: Path = REPO_ROOT) -> dict:
    return {
        "machine": _machine(),
        "os": _os(),
        "power": _power(),
        "load": _load(),
        "toolchain": _toolchain(),
        "upstream": _upstream(repo_root),
        "thread_env": {v: os.environ.get(v) for v in THREAD_ENV_VARS},
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def platform_id(snap: dict | None = None) -> str:
    machine = (snap or snapshot())["machine"]
    return f"{machine['arch']}-darwin-{machine['cpu_model']}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", type=Path, help="write the snapshot JSON here")
    p.add_argument("--platform-file", type=Path, help="write platform_id() here")
    args = p.parse_args()
    snap = snapshot()
    text = json.dumps(snap, indent=2) + "\n"
    if not (args.json or args.platform_file):
        print(text, end="")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text)
    if args.platform_file:
        args.platform_file.parent.mkdir(parents=True, exist_ok=True)
        args.platform_file.write_text(platform_id(snap) + "\n")


if __name__ == "__main__":
    main()
