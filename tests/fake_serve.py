"""A stand-in for an adapter's --mode serve and --mode client (docs/serve_protocol.md), for the netem tests.

The server listens where --listen says, prints the ready line, accepts connections until SIGTERM, then
appends a serve summary to --out. The client connects to every --connect address, so a wrong address
fails it, and writes --session-queries client rows built like fake_adapter's.

FAKE_SERVE (JSON) maps an arm to a server behaviour: "die" (exit 1 before ready), "unsupported" (exit 0
before ready, and the client writes unsupported rows), "silent" (never ready), "hog" (allocate 256 MiB
and never get ready). Every call appends one line to FAKE_LOG.
"""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fake_adapter  # noqa: E402


def behaviour(arm: str) -> str:
    return json.loads(os.environ.get("FAKE_SERVE", "{}")).get(arm, "")


def log(line: str) -> None:
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(line + "\n")


def serve(a) -> int:
    b = behaviour(a.arm)
    if b == "die":
        print("fake: crashing before ready", file=sys.stderr)
        return 1
    if b == "unsupported":
        print("unsupported parameters: fake", file=sys.stderr)
        return 0
    host, port = a.listen.rsplit(":", 1)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(port)))
    sock.listen()
    addr = f"{host}:{sock.getsockname()[1]}"
    log(f"serve {a.arm} {a.server_id} {addr}")
    t0 = time.monotonic()

    def stop(*_):
        with open(a.out, "a") as out:
            out.write(json.dumps({
                "mode": "serve", "protocol": 1, "arm": a.arm, "server_id": a.server_id or 0,
                "servers": 2 if a.arm in ("M0", "M1") else 1, "db_bytes": a.db_bytes,
                "record_bytes": a.record_bytes or None, "n_records": 1, "seed": a.seed, "workers": a.workers,
                "upstream_sha": "0" * 40, "db_sha256": "0" * 64, "public_seed": None,
                "wall_s": time.monotonic() - t0, "cpu_user_s": 0.0, "cpu_sys_s": 0.0, "peak_rss_bytes": 1,
                "answered": 0, "queued": 0}) + "\n")
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    if b == "hog":
        _hog = bytearray(256 << 20)
        for i in range(0, len(_hog), 4096):
            _hog[i] = 1
    if b not in ("silent", "hog"):
        print(f"vpir-serve ready {addr}", file=sys.stderr, flush=True)

    def drain(conn):
        while conn.recv(4096):
            pass

    while True:
        conn, _ = sock.accept()
        threading.Thread(target=drain, args=(conn,), daemon=True).start()


def client(a) -> int:
    addrs = a.connect.split(",")
    log(f"client {a.arm} {a.connect}")
    unsupported = behaviour(a.arm) == "unsupported"
    if not unsupported:
        for addr in addrs:
            host, port = addr.rsplit(":", 1)
            socket.create_connection((host, int(port)), timeout=10).close()
    a.phase, a.warmup = "online", 0
    with open(a.out, "a") as out:
        for rep in range(a.session_queries):
            r = fake_adapter.row(a, rep)
            r["mode"] = "client"
            m = r["metrics"]
            m["rtt_s"] = [0.002] * len(addrs)
            m["rtt_cpu_s"] = m["server_answer_cpu_s"] = [None] * len(addrs)
            if unsupported:
                r.update(status="unsupported_params", error="fake", correct=None)
            out.write(json.dumps(r) + "\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    for flag in ("--mode", "--arm", "--listen", "--connect", "--out"):
        p.add_argument(flag)
    for flag in ("--db-bytes", "--record-bytes", "--seed", "--workers", "--server-id", "--session-queries"):
        p.add_argument(flag, type=int)
    _binary, *rest = sys.argv[1:]
    a = p.parse_args(rest)
    return serve(a) if a.mode == "serve" else client(a)


if __name__ == "__main__":
    sys.exit(main())
