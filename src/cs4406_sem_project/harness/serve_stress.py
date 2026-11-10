"""Load an adapter's servers with concurrent clients replaying a verified corpus, for the race checks in
docs/serve_protocol.md ("Threads").

The servers run from an instrumented build (Go `-race`, C++ `-fsanitize=thread`). A plain build's
`--mode client` writes the corpus, so every expected answer comes from a verified honest decode. Clients
pipeline queries to every server and check each answer's SHA-256 against its EXPECT frame, while a side
thread sends STATS requests, wrong-length queries and unknown frame types, and resets connections with
queries in flight. The run ends with SIGTERM under load.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from cs4406_sem_project.harness import memcap

HELLO, INFO, QUERY, ANSWER, STATS_REQ, STATS, ERROR, EXPECT = 1, 2, 3, 4, 5, 6, 7, 0x10
RACE_MARKERS = ("WARNING: DATA RACE", "WARNING: ThreadSanitizer")


def frame(t: int, body: bytes = b"") -> bytes:
    return struct.pack(">IB", 1 + len(body), t) + body


def _recv(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"closed after {len(buf)} of {n} bytes")
        buf += chunk
    return bytes(buf)


def read_frame(s: socket.socket) -> tuple[int, bytes]:
    length, t = struct.unpack(">IB", _recv(s, 5))
    return t, _recv(s, length - 1)


def connect(addr: tuple[str, int]) -> socket.socket:
    s = socket.create_connection(addr, timeout=120)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.sendall(frame(HELLO, b'{"protocol": 1}'))
    t, _ = read_frame(s)
    if t != INFO:
        raise RuntimeError(f"expected INFO, got frame type {t:#x}")
    return s


@dataclass
class Server:
    proc: subprocess.Popen
    addr: tuple[str, int] = ("", 0)
    stderr: list[str] = field(default_factory=list)
    peak_footprint_bytes: int = 0
    over_cap: bool = False

    def stop(self, sig: int = signal.SIGTERM, timeout_s: float = 900) -> int:
        self.proc.send_signal(sig)
        return self.proc.wait(timeout=timeout_s)


def start_server(binary: Path, arm: str, db_bytes: int, record_bytes: int, seed: int, workers: int,
                 server_id: int | None, out: Path, env: dict, cap_bytes: int) -> Server:
    argv = [str(binary), "--mode", "serve", "--arm", arm, "--db-bytes", str(db_bytes),
            "--record-bytes", str(record_bytes), "--seed", str(seed), "--workers", str(workers),
            "--listen", "127.0.0.1:0", "--out", str(out)]
    if server_id is not None:
        argv += ["--server-id", str(server_id)]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, **env})
    srv = Server(proc)
    ready = threading.Event()

    def pump():
        for line in proc.stderr:
            srv.stderr.append(line.rstrip("\n"))
            if line.startswith("vpir-serve ready "):
                host, port = line.split()[2].rsplit(":", 1)
                srv.addr = (host, int(port))
                ready.set()

    def watch():
        while proc.poll() is None:
            fp = memcap.footprint_bytes(proc.pid)
            srv.peak_footprint_bytes = max(srv.peak_footprint_bytes, fp)
            if fp > cap_bytes:
                srv.over_cap = True
                proc.kill()
                return
            time.sleep(0.1)

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    if not ready.wait(900):
        proc.kill()
        raise RuntimeError("server never printed its ready line:\n" + "\n".join(srv.stderr[-20:]))
    return srv


@dataclass(frozen=True)
class Expected:
    query: bytes  # the query bytes after the req_id
    answer_len: int
    sha256: bytes


def read_corpus(path: Path, servers: int) -> list[list[Expected]]:
    """Per query, per server: the query and its honest answer's length and hash."""
    data, i, frames = path.read_bytes(), 0, []
    while i < len(data):
        length, t = struct.unpack(">IB", data[i:i + 5])
        frames.append((t, data[i + 5:i + 4 + length]))
        i += 4 + length
    rest = frames[servers:]
    corpus = []
    for q in range(0, len(rest), 2 * servers):
        row = []
        for s in range(servers):
            (qt, qb), (et, eb) = rest[q + 2 * s], rest[q + 2 * s + 1]
            assert qt == QUERY and et == EXPECT, (qt, et)
            _, sid, alen = struct.unpack(">QBI", eb[:13])
            assert sid == s
            row.append(Expected(qb[8:], alen, eb[13:45]))
        corpus.append(row)
    return corpus


def make_corpus(client: Path, arm: str, db_bytes: int, record_bytes: int, seed: int, addrs: list, queries: int,
                work: Path, cap_bytes: int) -> list[list[Expected]]:
    corpus_path = work / "corpus.bin"
    argv = [str(client), "--mode", "client", "--arm", arm, "--db-bytes", str(db_bytes),
            "--record-bytes", str(record_bytes), "--seed", str(seed),
            "--connect", ",".join(f"{h}:{p}" for h, p in addrs), "--session-queries", str(queries),
            "--out", str(work / "client.jsonl"), "--corpus-out", str(corpus_path)]
    r = memcap.run(argv, timeout_s=3600, cap_bytes=cap_bytes)
    (work / "client.stderr.log").write_text(r.stderr)
    if r.returncode != 0:
        raise RuntimeError(f"corpus client exited {r.returncode}:\n{r.stderr[-1500:]}")
    return read_corpus(corpus_path, len(addrs))


def replay(addrs: list, corpus: list[list[Expected]], clients: int, seconds: float, pipeline: int = 2) -> dict:
    """Each client pipelines `pipeline` queries to every server and checks every answer."""
    totals = {"matched": 0, "mismatched": 0, "errors": []}
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def one(cid: int):
        ok = bad = 0
        try:
            conns = [connect(a) for a in addrs]
            k, n = cid, 0
            while time.monotonic() < deadline:
                window = []
                for _ in range(pipeline):
                    rid = ((cid + 1) << 32) | (n + 1)
                    n += 1
                    row = corpus[k % len(corpus)]
                    k += 1
                    for s, c in enumerate(conns):
                        c.sendall(frame(QUERY, struct.pack(">Q", rid) + row[s].query))
                    window.append((rid, row))
                want = {rid: row for rid, row in window}
                for s, c in enumerate(conns):
                    for _ in window:
                        t, body = read_frame(c)
                        if t != ANSWER:
                            raise RuntimeError(f"server {s}: frame type {t:#x}: {body[8:]!r}")
                        rid = struct.unpack(">Q", body[:8])[0]
                        exp = want[rid][s]
                        ans = body[24:]
                        if len(ans) == exp.answer_len and hashlib.sha256(ans).digest() == exp.sha256:
                            ok += 1
                        else:
                            bad += 1
            for c in conns:
                c.close()
        except Exception as e:  # noqa: BLE001 - every failure is reported, none is retried
            with lock:
                totals["errors"].append(f"client {cid}: {e!r}")
        with lock:
            totals["matched"] += ok
            totals["mismatched"] += bad

    threads = [threading.Thread(target=one, args=(c,)) for c in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return totals


def disturb(addrs: list, corpus: list[list[Expected]], stop: threading.Event) -> dict:
    counts = {"stats": 0, "wrong_length": 0, "unknown_type": 0, "resets": 0, "errors": []}
    while not stop.is_set():
        for s, addr in enumerate(addrs):
            try:
                with connect(addr) as c:
                    c.sendall(frame(STATS_REQ))
                    assert read_frame(c)[0] == STATS
                    counts["stats"] += 1
                    c.sendall(frame(QUERY, struct.pack(">Q", 7) + b"\0" * 5))
                    assert read_frame(c)[0] == ERROR
                    counts["wrong_length"] += 1
                with connect(addr) as c:
                    c.sendall(frame(0x42, b"x"))
                    assert read_frame(c)[0] == ERROR
                    counts["unknown_type"] += 1
                # The server still answers what it read before the reset, so keep the backlog small:
                # S4 takes seconds per query, more under the race detector.
                c = connect(addr)
                for k in range(2):
                    c.sendall(frame(QUERY, struct.pack(">Q", k + 1) + corpus[k % len(corpus)][s].query))
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                c.close()
                counts["resets"] += 1
            except Exception as e:  # noqa: BLE001
                counts["errors"].append(repr(e))
        stop.wait(1.0)
    return counts


def stress(*, arm: str, servers: int, db_bytes: int, record_bytes: int, server_bin: Path, client_bin: Path,
           work: Path, seconds: float, workers: int = 6, clients: int = 16, corpus_queries: int = 64,
           seed: int = 1234, env: dict | None = None, cap_bytes: int = 4 * memcap.GIB) -> dict:
    """One arm: start its servers, write a corpus, replay it under disturbances, stop with SIGTERM under load."""
    work.mkdir(parents=True, exist_ok=True)
    outs = [work / f"server{s}.jsonl" for s in range(servers)]
    srvs = [start_server(server_bin, arm, db_bytes, record_bytes, seed, workers,
                         s if servers == 2 else None, outs[s], env or {}, cap_bytes) for s in range(servers)]
    try:
        addrs = [s.addr for s in srvs]
        corpus = make_corpus(client_bin, arm, db_bytes, record_bytes, seed, addrs, corpus_queries, work, cap_bytes)
        stop, side = threading.Event(), {}
        t = threading.Thread(target=lambda: side.update(disturb(addrs, corpus, stop)))
        t.start()
        t0 = time.monotonic()
        totals = replay(addrs, corpus, clients, seconds)
        stop.set()
        t.join()
        elapsed = time.monotonic() - t0
        load = threading.Thread(target=replay, args=(addrs, corpus, clients, 4.0))
        load.start()
        time.sleep(1)
        codes = [s.stop() for s in srvs]
        load.join()
    finally:
        for s in srvs:
            if s.proc.poll() is None:
                s.proc.kill()
    logs = "\n".join(line for s in srvs for line in s.stderr)
    tsan_files = sorted(work.glob("tsan*"))
    logs += "".join(f.read_text(errors="replace") for f in tsan_files)
    for s, srv in enumerate(srvs):
        (work / f"server{s}.stderr.log").write_text("\n".join(srv.stderr))
    result = {
        "arm": arm, "db_bytes": db_bytes, "workers": workers, "clients": clients, "replay_s": elapsed,
        "matched": totals["matched"], "mismatched": totals["mismatched"], "client_errors": totals["errors"][:5],
        "disturbances": {k: v for k, v in side.items() if k != "errors"}, "disturbance_errors": side.get("errors", [])[:5],
        "exit_codes": codes, "summary_records": [len(o.read_text().splitlines()) if o.exists() else 0 for o in outs],
        "peak_footprint_bytes": [s.peak_footprint_bytes for s in srvs], "over_cap": [s.over_cap for s in srvs],
        "race_reports": sum(logs.count(m) for m in RACE_MARKERS), "tsan_files": [f.name for f in tsan_files],
    }
    result["clean"] = (result["race_reports"] == 0 and result["mismatched"] == 0 and not totals["errors"]
                       and not result["disturbance_errors"] and all(c == 0 for c in codes)
                       and not any(result["over_cap"]) and all(n == 1 for n in result["summary_records"]))
    (work / "result.json").write_text(json.dumps(result, indent=1))
    return result
