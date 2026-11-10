"""A stand-in for build/loadgen/loadgen, for the loadgen driver tests. It takes the same command line.

It connects to every --connect server and reads its INFO, so the row's workers are the live servers' W,
checks that --corpus and --client-records exist, and appends one schema-valid row to --out. Its numbers
are a queueing caricature, not measurements: each worker answers `per_worker_qps` queries per second, a
lone query takes `service_s`, so throughput is min(C / service_s, W x per_worker_qps) and latency C over
that. FAKE_LOADGEN (JSON) sets per_worker_qps, service_s, cpu (the loadgen CPU %), and crash_clients (exit
3 without a row at that client count). Every call appends its argv to FAKE_LOG.
"""

import argparse
import json
import os
import socket
import struct
import sys
from pathlib import Path


def server_info(addr: str) -> dict:
    host, port = addr.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=10) as c:
        c.sendall(struct.pack(">IB", 16, 1) + b'{"protocol": 1}')
        head = b""
        while len(head) < 5:
            head += c.recv(5 - len(head))
        length, t = struct.unpack(">IB", head)
        body = b""
        while len(body) < length - 1:
            body += c.recv(length - 1 - len(body))
    assert t == 2, t
    return json.loads(body)


def main() -> int:
    p = argparse.ArgumentParser()
    for flag in ("--corpus", "--client-records", "--connect", "--loop", "--out", "--run-id", "--platform",
                 "--git-sha"):
        p.add_argument(flag)
    for flag in ("--clients", "--run", "--seed"):
        p.add_argument(flag, type=int)
    for flag in ("--rate", "--warmup-s", "--window-s", "--drain-s", "--p95-limit-s"):
        p.add_argument(flag, type=float)
    _binary, *rest = sys.argv[1:]
    a = p.parse_args(rest)
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(rest) + "\n")
    knobs = {"per_worker_qps": 20.0, "service_s": 0.01, "cpu": 5.0, **json.loads(os.environ.get("FAKE_LOADGEN", "{}"))}
    if a.clients is not None and a.clients == knobs.get("crash_clients"):
        return 3
    assert Path(a.corpus).exists() and Path(a.client_records).exists()
    infos = [server_info(addr) for addr in a.connect.split(",")]
    w = infos[0]["workers"]
    capacity = w * knobs["per_worker_qps"]
    if a.loop == "closed":
        tput = min(a.clients / knobs["service_s"], capacity)
        p50 = a.clients / tput
        offered, status = None, "ok"
    else:
        tput = min(a.rate, capacity)
        p50 = knobs["service_s"] * (10 if a.rate > capacity else 1)
        offered, status = a.rate, "saturated" if a.rate > capacity else "ok"
    if a.p95_limit_s is not None and 1.2 * p50 > a.p95_limit_s:
        status = "saturated"
    completed = int(tput * a.window_s)
    row = {
        "mode": "loadgen", "run_id": a.run_id, "git_sha_harness": a.git_sha or None, "platform": a.platform,
        "arm": infos[0]["arm"], "db_bytes": infos[0]["db_bytes"], "record_bytes": infos[0]["record_bytes"],
        "seed": infos[0]["seed"], "workers": w, "loop": a.loop,
        "clients": a.clients if a.loop == "closed" else None, "offered_load_qps": offered, "run": a.run,
        "warmup_s": a.warmup_s, "window_s": a.window_s, "sent": completed, "completed": completed,
        "mismatched": 0, "errors": 0, "throughput_qps": completed / a.window_s,
        "lat_p50_s": p50, "lat_p95_s": 1.2 * p50, "lat_p99_s": 1.5 * p50,
        "e2e_lat_p50_s": p50 + 0.01, "e2e_lat_p95_s": 1.2 * p50 + 0.01, "e2e_lat_p99_s": 1.5 * p50 + 0.01,
        "server_cpu_util": 100.0 * w, "server_peak_rss_bytes": 1, "loadgen_cpu_util": knobs["cpu"],
        "status": status, "error": None,
    }
    with open(a.out, "a") as f:
        f.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
