"""A stand-in adapter for the runner tests. It takes the spec 9 command line and writes rows built from
one real adapter row in results/raw/, so they pass the schema exactly as real rows do.

Behaviour per call comes from FAKE_ADAPTER (JSON): {"<arm>_<db_bytes>_<record_bytes>_<phase> prefix": "crash" | "short" | "hang" |
"unsupported" | "oom"}. Every call appends its --arm/--db-bytes/--record-bytes/--phase to FAKE_LOG.
"""

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "results" / "raw" / "p7-findings-20261110T105629Z" / "peak-s0-go-honest.jsonl"
ADAPTER_OF = {"S0-go": "simplepir_go", "S0-cpp": "vsp_cpp", "S1": "vsp_cpp", "S2": "vsp_cpp",
              "S3": "apir_go", "S4": "apir_go", "M0": "apir_go", "M1": "apir_go"}


def _template(phase: str) -> dict:
    for line in FIXTURE.read_text().splitlines():
        r = json.loads(line)
        if r["phase"] == phase:
            return r
    raise SystemExit(f"fixture has no {phase} row")


def row(a, rep: int) -> dict:
    r = copy.deepcopy(_template(a.phase))
    one_bit = a.arm in ("S3", "S4")
    servers = 2 if a.arm in ("M0", "M1") else 1
    rec = None if one_bit else a.record_bytes
    r.update(run_id=os.environ.get("VPIR_RUN_ID"), git_sha_harness=os.environ.get("VPIR_GIT_SHA") or None,
             platform=os.environ.get("VPIR_PLATFORM"), adapter=ADAPTER_OF[a.arm], arm=a.arm,
             db_bytes=a.db_bytes, record_bytes=rec, seed=a.seed, rep=rep, is_warmup=rep < a.warmup,
             retrieved_bits=1 if one_bit else 8 * rec,
             n_records=8 * a.db_bytes if one_bit else a.db_bytes // rec)
    m = r["metrics"]
    if a.phase == "online":
        m["verify_fused"] = a.arm in ("M1", "S3", "S4")
        m["client_verify_s"] = None if m["verify_fused"] else 0.001
        m["client_verify_cpu_s"] = m["client_verify_s"]
        m["up_bytes_per_server"] = [m["up_bytes"]] * servers
        m["down_bytes_per_server"] = [m["down_bytes"]] * servers
        m["up_bytes"], m["down_bytes"] = m["up_bytes"] * servers, m["down_bytes"] * servers
        m["server_answer_s"] = m["server_answer_s"][:1] * servers
        m["server_answer_cpu_s"] = m["server_answer_cpu_s"][:1] * servers
        r["detected"] = None if a.arm in ("S0-go", "S0-cpp", "M0") else False
    r["analytical"] = None
    return r


def main() -> int:
    p = argparse.ArgumentParser()
    for flag in ("--arm", "--phase", "--fault", "--recovery", "--out"):
        p.add_argument(flag)
    for flag in ("--db-bytes", "--record-bytes", "--reps", "--warmup", "--seed"):
        p.add_argument(flag, type=int)
    _binary, *rest = sys.argv[1:]
    a = p.parse_args(rest)
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(f"{a.arm} {a.db_bytes} {a.record_bytes} {a.phase}\n")
    behaviour = ""
    cell = f"{a.arm}_{a.db_bytes}_{a.record_bytes}_{a.phase}"
    for prefix, b in json.loads(os.environ.get("FAKE_ADAPTER", "{}")).items():
        if cell.startswith(prefix):
            behaviour = b
    n = a.warmup + a.reps
    with open(a.out, "a") as out:
        for rep in range(n):
            if behaviour in ("crash", "short", "hang", "oom") and rep == 1:
                break
            r = row(a, rep)
            if behaviour == "unsupported":
                r.update(status="unsupported_params", error="no suitable params", correct=None)
                out.write(json.dumps(r) + "\n")
                break
            out.write(json.dumps(r) + "\n")
            time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    if behaviour == "hang":
        time.sleep(3600)
    if behaviour == "oom":
        _hog = bytearray(64 << 20)
        time.sleep(3600)
    return 1 if behaviour == "crash" else 0


if __name__ == "__main__":
    sys.exit(main())
