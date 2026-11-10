"""P8 serve-mode judge (spec 11 `test_concurrent_correctness`, gate G6), written from
docs/serve_protocol.md, spec 9 and 11, and src/adapters/README.md only. It speaks the wire protocol
with a small pure-Python client, so it judges every adapter's `--mode serve` and `--mode client`
without looking at adapter code.

Sizes. As in tests/test_recovery.py: every arm runs at 1 MiB except S4 at 64 KiB, and S3 and S4 take
`--record-bytes 0`. M0 and M1 run two server processes (`--server-id 0` and `1`).

Counts come from env vars: VPIR_SERVE_WORKERS (server workers W, default 4, at most 6),
VPIR_SERVE_CLIENTS (concurrent Python clients, default 16), VPIR_SERVE_CORPUS_QUERIES (corpus size,
default 64, S4 16), VPIR_SERVE_PER_CLIENT (queries each client replays, default 64, S4 8, replayed
round-robin from a per-client offset so the corpus is replayed several times over),
VPIR_SERVE_READY_TIMEOUT (seconds to wait for the ready line, default 900),
VPIR_SERVE_IO_TIMEOUT (socket timeout, default 120),
VPIR_SERVE_TIMEOUT (one-shot subprocess timeout, default 3600), VPIR_SERVE_STOP_TIMEOUT (seconds a
server may take to exit after SIGTERM, default 60). VPIR_ADAPTER_DIR and VPIR_ADAPTER_MEM_CAP_GIB
are as in the other adapter tests. Every adapter process runs under the memory cap: one-shot runs
through memcap.run, servers through a watchdog thread on memcap.footprint_bytes.

VPIR_SERVE_FAKE=/path/to/fake.py replaces every adapter binary with `python fake.py <same args>`.
It exists to validate this judge against fake servers and is unset in normal runs.
"""

import hashlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from cs4406_sem_project.harness import memcap

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = Path(os.environ.get("VPIR_ADAPTER_DIR", ROOT / "build" / "adapters"))
FAKE = os.environ.get("VPIR_SERVE_FAKE")
WORKERS = min(int(os.environ.get("VPIR_SERVE_WORKERS", "4")), 6)
CLIENTS = min(int(os.environ.get("VPIR_SERVE_CLIENTS", "16")), 16)
READY_TIMEOUT_S = float(os.environ.get("VPIR_SERVE_READY_TIMEOUT", "900"))
TIMEOUT_S = float(os.environ.get("VPIR_SERVE_TIMEOUT", "3600"))
STOP_TIMEOUT_S = float(os.environ.get("VPIR_SERVE_STOP_TIMEOUT", "60"))
IO_TIMEOUT_S = float(os.environ.get("VPIR_SERVE_IO_TIMEOUT", "120"))
MEM_CAP = memcap.cap_from_env(2)
SEED = 1234

T_HELLO, T_INFO, T_QUERY, T_ANSWER, T_STATS_REQ, T_STATS, T_ERROR, T_EXPECT = (
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x10)
MAX_FRAME = 1 << 30
READY_RE = re.compile(r"^vpir-serve ready (\S+):(\d+)$")
INFO_KEYS = ("protocol", "arm", "server_id", "servers", "db_bytes", "record_bytes", "n_records",
             "seed", "workers", "upstream_sha", "db_sha256", "public_seed", "scheme_params")
STATS_KEYS = ("wall_s", "cpu_user_s", "cpu_sys_s", "peak_rss_bytes", "answered", "queued")
PIPELINE_DEPTHS = (1, 1, 2, 4, 8, 16, 1, 3)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Arm:
    name: str
    binary: str
    servers: int = 1
    db_bytes: int = 1 << 20
    record_bytes: int = 32
    corpus_q: int = 64
    per_client: int = 64

    @property
    def fused(self) -> bool:
        return self.name in ("S3", "S4", "M1")

    def counts(self) -> tuple[int, int]:
        return (_env_int("VPIR_SERVE_CORPUS_QUERIES", self.corpus_q),
                _env_int("VPIR_SERVE_PER_CLIENT", self.per_client))


ARMS = (
    Arm("S0-go", "simplepir-adapter"),
    Arm("S0-cpp", "vsp-adapter"),
    Arm("S1", "vsp-adapter"),
    Arm("S2", "vsp-adapter"),
    Arm("S3", "apir-adapter", record_bytes=0),
    Arm("S4", "apir-adapter", db_bytes=1 << 16, record_bytes=0, corpus_q=16, per_client=8),
    Arm("M0", "apir-adapter", servers=2),
    Arm("M1", "apir-adapter", servers=2),
)
FAST_ARMS = tuple(a for a in ARMS if a.name in ("S0-go", "S0-cpp", "M0"))


def _ids(arms):
    return [a.name for a in arms]


def _base_argv(arm: Arm) -> list[str]:
    if FAKE:
        return [sys.executable, FAKE]
    binary = ADAPTER_DIR / arm.binary
    if not binary.is_file():
        pytest.skip(f"{arm.binary} not built; run make build")
    return [str(binary)]


def _common_args(arm: Arm, seed: int = SEED) -> list[str]:
    return ["--arm", arm.name, "--db-bytes", str(arm.db_bytes),
            "--record-bytes", str(arm.record_bytes), "--seed", str(seed)]


def _run_once(argv: list[str]) -> memcap.Finished:
    try:
        res = memcap.run(argv, timeout_s=TIMEOUT_S, cap_bytes=MEM_CAP)
    except subprocess.TimeoutExpired:
        pytest.fail(f"timed out after {TIMEOUT_S}s: {' '.join(argv)}")
    if res.over_cap:
        pytest.fail(f"memory footprint passed the {MEM_CAP / memcap.GIB:g} GiB cap, killed: "
                    f"{' '.join(argv)}\nstderr tail:\n{res.stderr[-1500:]}")
    return res


# ---------------------------------------------------------------- wire protocol client

class ConnClosed(Exception):
    pass


class ProtocolFailure(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnClosed(f"peer closed after {len(buf)} of {n} bytes")
        buf += chunk
    return bytes(buf)


def pack_frame(ftype: int, body: bytes = b"") -> bytes:
    return struct.pack(">IB", 1 + len(body), ftype) + body


def parse_frames(data: bytes):
    pos = 0
    while pos < len(data):
        assert pos + 5 <= len(data), f"truncated frame header at byte {pos}"
        (length,) = struct.unpack_from(">I", data, pos)
        assert 1 <= length <= MAX_FRAME and pos + 4 + length <= len(data), (
            f"bad frame length {length} at byte {pos} of {len(data)}")
        yield data[pos + 4], data[pos + 5:pos + 4 + length]
        pos += 4 + length


@dataclass(frozen=True)
class Answer:
    req_id: int
    queue_ns: int
    answer_ns: int
    data: bytes


class Conn:
    def __init__(self, addr: tuple[str, int], timeout: float = IO_TIMEOUT_S):
        self.sock = socket.create_connection(addr, timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        self.sock.close()

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send(self, ftype: int, body: bytes = b"") -> None:
        self.send_raw(pack_frame(ftype, body))

    def recv(self) -> tuple[int, bytes]:
        (length,) = struct.unpack(">I", _recv_exact(self.sock, 4))
        if not 1 <= length <= MAX_FRAME:
            raise ProtocolFailure(f"server sent frame length {length}")
        payload = _recv_exact(self.sock, length)
        return payload[0], payload[1:]

    def hello(self) -> dict:
        self.send(T_HELLO, json.dumps({"protocol": 1}).encode())
        ftype, body = self.recv()
        if ftype != T_INFO:
            raise ProtocolFailure(f"HELLO answered with frame type {ftype:#x}: {body[:200]!r}")
        return json.loads(body)

    def send_query(self, req_id: int, query: bytes) -> None:
        self.send(T_QUERY, struct.pack(">Q", req_id) + query)

    def recv_answer(self) -> Answer:
        ftype, body = self.recv()
        if ftype == T_ERROR:
            raise ProtocolFailure(f"ERROR req_id={struct.unpack_from('>Q', body)[0]}: "
                                  f"{body[8:].decode(errors='replace')}")
        if ftype != T_ANSWER or len(body) < 24:
            raise ProtocolFailure(f"expected ANSWER, got type {ftype:#x} with {len(body)} bytes")
        rid, q_ns, a_ns = struct.unpack_from(">QQQ", body)
        return Answer(rid, q_ns, a_ns, body[24:])

    def stats(self) -> dict:
        self.send(T_STATS_REQ)
        ftype, body = self.recv()
        if ftype != T_STATS:
            raise ProtocolFailure(f"STATS_REQ answered with frame type {ftype:#x}: {body[:200]!r}")
        return json.loads(body)


def parse_error(body: bytes) -> tuple[int, str]:
    assert len(body) >= 8, f"ERROR body of {len(body)} bytes has no req_id"
    (rid,) = struct.unpack_from(">Q", body)
    return rid, body[8:].decode("utf-8")


# ---------------------------------------------------------------- corpus

@dataclass(frozen=True)
class Item:
    query: bytes
    answer_len: int
    sha256: bytes


@dataclass(frozen=True)
class Corpus:
    infos: list[dict]
    items: list[list[Item]]  # items[query][server]


def parse_corpus(data: bytes, servers: int) -> Corpus:
    frames = list(parse_frames(data))
    assert len(frames) >= servers, f"corpus has {len(frames)} frames, fewer than {servers} INFO"
    infos = []
    for s in range(servers):
        ftype, body = frames[s]
        assert ftype == T_INFO, f"corpus frame {s} has type {ftype:#x}, expected INFO"
        infos.append(json.loads(body))
    rest = frames[servers:]
    assert len(rest) % (2 * servers) == 0, (
        f"{len(rest)} QUERY/EXPECT frames is not a multiple of 2 x {servers} servers")
    items = []
    for q in range(len(rest) // (2 * servers)):
        row = []
        for s in range(servers):
            qtype, qbody = rest[(q * servers + s) * 2]
            etype, ebody = rest[(q * servers + s) * 2 + 1]
            where = f"query {q} server {s}"
            assert qtype == T_QUERY, f"{where}: expected QUERY, got {qtype:#x}"
            assert etype == T_EXPECT, f"{where}: expected EXPECT, got {etype:#x}"
            assert len(qbody) > 8 and len(ebody) == 8 + 1 + 4 + 32, (
                f"{where}: QUERY body {len(qbody)} B, EXPECT body {len(ebody)} B")
            (qid,) = struct.unpack_from(">Q", qbody)
            eid, esrv, alen = struct.unpack_from(">QBI", ebody)
            assert qid == q + 1 and eid == q + 1, f"{where}: req_id QUERY {qid}, EXPECT {eid}, want {q + 1}"
            assert esrv == s, f"{where}: EXPECT server_id {esrv}"
            assert alen > 0, f"{where}: EXPECT answer_len 0"
            row.append(Item(qbody[8:], alen, ebody[13:]))
        items.append(row)
    return Corpus(infos, items)


# ---------------------------------------------------------------- server processes

class Server:
    def __init__(self, arm: Arm, server_id: int, workers: int, out: Path | None = None):
        self.arm, self.server_id = arm, server_id
        argv = [*_base_argv(arm), "--mode", "serve", *_common_args(arm), "--workers", str(workers),
                "--listen", "127.0.0.1:0"]
        if arm.servers == 2:
            argv += ["--server-id", str(server_id)]
        if out is not None:
            argv += ["--out", str(out)]
        self.argv = argv
        self.lines: list[str] = []
        self.addr: tuple[str, int] | None = None
        self.over_cap = False
        self.peak = 0
        self._ready = threading.Event()
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True)
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            self.lines.append(line)
            m = READY_RE.match(line)
            if m and self.addr is None:
                self.addr = (m.group(1), int(m.group(2)))
                self._ready.set()

    def _watch(self) -> None:
        while self.proc.poll() is None:
            fp = memcap.footprint_bytes(self.proc.pid)
            self.peak = max(self.peak, fp)
            if fp > MEM_CAP:
                self.over_cap = True
                self.proc.kill()
                return
            time.sleep(0.1)

    def ready_lines(self) -> list[str]:
        return [ln for ln in list(self.lines) if "vpir-serve ready" in ln]

    def wait_ready(self, timeout: float = READY_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready.wait(0.1):
                return
            if self.proc.poll() is not None:
                break
        self.fail(f"no `vpir-serve ready HOST:PORT` line within {timeout:g}s"
                  if self.proc.poll() is None else "exited before it was ready")

    def tail(self) -> str:
        return "\n".join(self.lines[-25:])

    def describe(self) -> str:
        rc = self.proc.poll()
        state = "running" if rc is None else f"exited {rc}"
        return (f"server {self.server_id} ({' '.join(self.argv)}) {state}"
                f"{', killed over the memory cap' if self.over_cap else ''}\nstderr tail:\n{self.tail()}")

    def fail(self, why: str) -> None:
        pytest.fail(f"{why}: {self.describe()}")

    def assert_alive(self, when: str) -> None:
        if self.proc.poll() is not None or self.over_cap:
            self.fail(f"server died {when}")

    def stop(self, sig: int = signal.SIGTERM, timeout: float = STOP_TIMEOUT_S) -> int | None:
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


@dataclass
class Cluster:
    arm: Arm
    servers: list[Server]
    error: str | None = None

    @property
    def addrs(self) -> list[tuple[str, int]]:
        return [s.addr for s in self.servers]

    def assert_alive(self, when: str) -> None:
        for s in self.servers:
            s.assert_alive(when)


def start_servers(arm: Arm, workers: int, outs: list[Path | None] | None = None) -> list[Server]:
    outs = outs or [None] * arm.servers
    servers = [Server(arm, s, workers, outs[s]) for s in range(arm.servers)]
    try:
        for s in servers:
            s.wait_ready()
    except BaseException:
        for s in servers:
            s.kill()
        raise
    return servers


@pytest.fixture(scope="session")
def cluster(request):
    cache: dict = {}

    def _stop_all():
        for c in cache.values():
            if isinstance(c, Cluster):
                for s in c.servers:
                    s.stop()
                    s.kill()

    request.addfinalizer(_stop_all)

    def _get(arm: Arm) -> Cluster:
        if arm.name not in cache:
            _base_argv(arm)
            try:
                cache[arm.name] = Cluster(arm, start_servers(arm, WORKERS))
            except pytest.fail.Exception as e:
                cache[arm.name] = f"{e}"
        got = cache[arm.name]
        if isinstance(got, str):
            pytest.fail(f"{arm.name} servers did not start: {got}")
        return got

    return _get


@dataclass
class ClientRun:
    corpus: Corpus
    records: list[dict]
    returncode: int
    stderr: str


@pytest.fixture(scope="session")
def client_run(cluster, tmp_path_factory):
    cache: dict = {}

    def _get(arm: Arm) -> ClientRun:
        if arm.name not in cache:
            cl = cluster(arm)
            q, _ = arm.counts()
            d = tmp_path_factory.mktemp(f"client-{arm.name}")
            out, corpus_path = d / "out.jsonl", d / "corpus.bin"
            argv = [*_base_argv(arm), "--mode", "client", *_common_args(arm),
                    "--connect", ",".join(f"{h}:{p}" for h, p in cl.addrs),
                    "--session-queries", str(q), "--out", str(out),
                    "--corpus-out", str(corpus_path)]
            res = _run_once(argv)
            if res.returncode != 0 or not corpus_path.is_file():
                cache[arm.name] = (f"exit {res.returncode} from {' '.join(argv)}\n"
                                   f"stderr tail:\n{res.stderr[-1500:]}")
            else:
                records = [json.loads(ln) for ln in out.read_text().splitlines() if ln.strip()]
                corpus = parse_corpus(corpus_path.read_bytes(), arm.servers)
                cache[arm.name] = ClientRun(corpus, records, res.returncode, res.stderr)
        got = cache[arm.name]
        if isinstance(got, str):
            pytest.fail(f"corpus run failed: {got}")
        return got

    return _get


# ---------------------------------------------------------------- replay under load

@dataclass
class Sample:
    client: int
    server: int
    req_id: int
    rtt_ns: int
    queue_ns: int
    answer_ns: int
    problem: str | None


def replay_client(addrs, corpus: Corpus, client: int, count: int, depth: int,
                  out: list[Sample]) -> None:
    conns = [Conn(a) for a in addrs]
    try:
        for c in conns:
            c.hello()
        offset = client * 4
        idxs = [(offset + k) % len(corpus.items) for k in range(count)]
        for w in range(0, count, depth):
            window = [(((client + 1) << 32) | (w + j + 1), idxs[w + j]) for j in range(len(idxs[w:w + depth]))]
            sent = {}
            for rid, qi in window:
                for s, c in enumerate(conns):
                    sent[(s, rid)] = time.perf_counter_ns()
                    c.send_query(rid, corpus.items[qi][s].query)
            for s, c in enumerate(conns):
                pending = {rid: qi for rid, qi in window}
                while pending:
                    ans = c.recv_answer()
                    recv = time.perf_counter_ns()
                    qi = pending.pop(ans.req_id, None)
                    if qi is None:
                        raise ProtocolFailure(f"server {s} answered unknown or repeated req_id "
                                              f"{ans.req_id:#x}")
                    item = corpus.items[qi][s]
                    problem = None
                    if len(ans.data) != item.answer_len:
                        problem = f"length {len(ans.data)}, want {item.answer_len}"
                    elif hashlib.sha256(ans.data).digest() != item.sha256:
                        problem = "SHA-256 differs from EXPECT"
                    out.append(Sample(client, s, ans.req_id, recv - sent[(s, ans.req_id)],
                                      ans.queue_ns, ans.answer_ns,
                                      f"corpus entry {qi}: {problem}" if problem else None))
    finally:
        for c in conns:
            c.close()


@dataclass
class Replay:
    samples: list[Sample]
    failures: list[str]
    expected: int


@pytest.fixture(scope="session")
def replay(cluster, client_run):
    cache: dict = {}

    def _get(arm: Arm) -> Replay:
        if arm.name not in cache:
            cl, corpus = cluster(arm), client_run(arm).corpus
            _, per_client = arm.counts()
            outs: list[list[Sample]] = [[] for _ in range(CLIENTS)]
            failures: list[str] = []
            lock = threading.Lock()

            def work(i):
                try:
                    replay_client(cl.addrs, corpus, i, per_client,
                                  PIPELINE_DEPTHS[i % len(PIPELINE_DEPTHS)], outs[i])
                except Exception as e:
                    with lock:
                        failures.append(f"client {i} (depth {PIPELINE_DEPTHS[i % 8]}): "
                                        f"{type(e).__name__}: {e}")

            threads = [threading.Thread(target=work, args=(i,)) for i in range(CLIENTS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            cache[arm.name] = Replay([s for o in outs for s in o], failures,
                                     CLIENTS * per_client * arm.servers)
        return cache[arm.name]

    return _get


# ---------------------------------------------------------------- checks

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_info(arm: Arm, info: dict, server_id: int, workers: int) -> None:
    where = f"{arm.name} server {server_id} INFO"
    missing = [k for k in INFO_KEYS if k not in info]
    assert not missing, f"{where}: missing keys {missing}; got {sorted(info)}"
    assert info["protocol"] == 1, f"{where}: protocol {info['protocol']!r}"
    assert info["arm"] == arm.name, f"{where}: arm {info['arm']!r}"
    assert info["server_id"] == server_id, f"{where}: server_id {info['server_id']!r}"
    assert info["servers"] == arm.servers, f"{where}: servers {info['servers']!r}"
    assert info["db_bytes"] == arm.db_bytes, f"{where}: db_bytes {info['db_bytes']!r}"
    want_rb = arm.record_bytes or None  # null for the one-bit arms, as in bench records
    assert info["record_bytes"] == want_rb, f"{where}: record_bytes {info['record_bytes']!r}"
    assert info["seed"] == SEED, f"{where}: seed {info['seed']!r}"
    assert info["workers"] == workers, f"{where}: workers {info['workers']!r}, started with {workers}"
    n = info["n_records"]
    assert _is_int(n) and n > 0, f"{where}: n_records {n!r}"
    if arm.name in ("S3", "S4"):
        assert n == 8 * arm.db_bytes, f"{where}: n_records {n}, want 8 x db_bytes"
    if arm.name in ("M0", "M1"):
        assert n == arm.db_bytes // arm.record_bytes, f"{where}: n_records {n}"
    assert isinstance(info["upstream_sha"], str) and info["upstream_sha"], f"{where}: upstream_sha"
    assert re.fullmatch(r"[0-9a-f]{64}", str(info["db_sha256"])), f"{where}: db_sha256 {info['db_sha256']!r}"
    ps = info["public_seed"]
    assert ps is None or (isinstance(ps, str) and re.fullmatch(r"[0-9a-fA-F]+", ps)), (
        f"{where}: public_seed {ps!r} is neither null nor hex")
    sp = info["scheme_params"]
    assert isinstance(sp, dict) and sp, f"{where}: scheme_params {sp!r} is not a non-empty object"
    assert sp.get("parallelism") == "inter_query", f"{where}: scheme_params.parallelism {sp.get('parallelism')!r}"


def _fresh_infos(cl: Cluster) -> list[dict]:
    infos = []
    for s, addr in enumerate(cl.addrs):
        c = Conn(addr)
        try:
            infos.append(c.hello())
        finally:
            c.close()
    return infos


def _answers_correctly(cl: Cluster, corpus: Corpus, conn: Conn, s: int, rid: int, qi: int = 0):
    conn.send_query(rid, corpus.items[qi][s].query)
    ans = conn.recv_answer()
    item = corpus.items[qi][s]
    assert ans.req_id == rid, f"answer req_id {ans.req_id}, sent {rid}"
    assert len(ans.data) == item.answer_len and hashlib.sha256(ans.data).digest() == item.sha256, (
        "server answered a valid query wrongly after the abuse")


def _wait_closed(conn: Conn, what: str, timeout: float = 10.0) -> None:
    conn.sock.settimeout(timeout)
    try:
        ftype, _ = conn.recv()
    except ConnClosed:
        return
    except (ConnectionResetError, BrokenPipeError):
        return
    except socket.timeout:
        pytest.fail(f"{what}: connection stayed open for {timeout:g}s")
    pytest.fail(f"{what}: got another frame (type {ftype:#x}) instead of a close")


# ---------------------------------------------------------------- tests: session

@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_ready_line(arm, cluster):
    cl = cluster(arm)
    time.sleep(0.5)
    for s in cl.servers:
        lines = s.ready_lines()
        assert len(lines) == 1, f"server {s.server_id} printed {len(lines)} ready lines: {lines}"
        host, port = s.addr
        assert host == "127.0.0.1" and 1024 <= port <= 65535, f"ready address {host}:{port}"
        c = Conn(s.addr, timeout=10)
        try:
            c.hello()
        finally:
            c.close()
    ports = {s.addr for s in cl.servers}
    assert len(ports) == arm.servers, "two servers share one address"


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_session_info(arm, cluster):
    cl = cluster(arm)
    infos = _fresh_infos(cl)
    for s, info in enumerate(infos):
        check_info(arm, info, s, WORKERS)
    if arm.servers == 2:
        assert infos[0]["db_sha256"] == infos[1]["db_sha256"], "the two servers hold different databases"
        assert infos[0]["upstream_sha"] == infos[1]["upstream_sha"]
    again = _fresh_infos(cl)
    assert again == infos, "INFO changed between connections"


# ---------------------------------------------------------------- tests: client mode and corpus

@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_corpus_file(arm, cluster, client_run):
    run = client_run(arm)
    q, _ = arm.counts()
    assert len(run.corpus.items) == q, f"{len(run.corpus.items)} corpus queries, asked for {q}"
    served = _fresh_infos(cluster(arm))
    for s, info in enumerate(run.corpus.infos):
        check_info(arm, info, s, WORKERS)
        assert info == served[s], f"corpus INFO for server {s} differs from the server's INFO"
    for s in range(arm.servers):
        lengths = {len(row[s].query) for row in run.corpus.items}
        assert len(lengths) == 1, f"server {s} query lengths differ across the corpus: {lengths}"
        assert len({row[s].query for row in run.corpus.items}) > 1, "every corpus query is identical"
    if arm.servers == 2:
        # An XOR share pair is two different queries, one for each server.
        assert all(row[0].query != row[1].query for row in run.corpus.items), (
            "a two-server query equals its sibling")


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_client_records(arm, client_run):
    run = client_run(arm)
    recs = [r for r in run.records if r.get("phase") == "online" and not r.get("is_warmup")]
    q, _ = arm.counts()
    assert len(recs) == q, f"{len(recs)} online records, want one per query ({q})"
    if all(_is_int(r.get("rep")) for r in recs):
        recs.sort(key=lambda r: r["rep"])
    for i, r in enumerate(recs):
        where = f"{arm.name} record {i}"
        assert r["arm"] == arm.name and r["status"] == "ok", f"{where}: arm/status {r['arm']}/{r['status']}"
        assert r.get("mode") == "client", f"{where}: mode {r.get('mode')!r}"
        assert r["correct"] is True, f"{where}: correct={r['correct']!r}"
        m = r["metrics"]
        for key in ("query_gen_s", "client_recover_s", "client_verify_s", "verify_fused",
                    "rtt_s", "server_answer_s", "up_bytes", "down_bytes",
                    "up_bytes_per_server", "down_bytes_per_server"):
            assert key in m, f"{where}: metrics missing {key!r}"
        for key in ("query_gen_s", "client_recover_s"):
            assert _is_num(m[key]) and m[key] >= 0, f"{where}: {key}={m[key]!r}"
        # As in bench records: S1 and S2 time a separate check, the fused arms verify inside
        # reconstruction, and the baselines (S0-go, S0-cpp, M0) have no check at all.
        assert m["verify_fused"] is arm.fused, f"{where}: verify_fused {m['verify_fused']!r}"
        if arm.name in ("S1", "S2"):
            assert _is_num(m["client_verify_s"]) and m["client_verify_s"] >= 0, f"{where}: client_verify_s"
        else:
            assert m["client_verify_s"] is None, f"{where}: client_verify_s={m['client_verify_s']!r}, no separate check"
        for key in ("rtt_s", "server_answer_s"):
            v = m[key]
            assert isinstance(v, list) and len(v) == arm.servers and all(
                _is_num(x) and x > 0 for x in v), f"{where}: {key}={v!r}, want {arm.servers} positive times"
        # The contract's rule: every _s metric has a _cpu_s twin, both null or both set, lists alike.
        for key, v in m.items():
            if key.endswith("_s") and not key.endswith("_cpu_s"):
                twin = key[:-2] + "_cpu_s"
                assert twin in m, f"{where}: {key} has no {twin}"
                assert (v is None) == (m[twin] is None), f"{where}: {key}={v!r}, {twin}={m[twin]!r}"
                if isinstance(v, list):
                    assert isinstance(m[twin], list) and len(m[twin]) == len(v), f"{where}: {twin}={m[twin]!r}"
        sp = r.get("scheme_params") or {}
        assert sp.get("parallelism") == "inter_query", f"{where}: scheme_params.parallelism {sp.get('parallelism')!r}"
        for s in range(arm.servers):
            assert m["rtt_s"][s] >= m["server_answer_s"][s] * 0.999, (
                f"{where}: rtt_s {m['rtt_s'][s]} below server_answer_s {m['server_answer_s'][s]}")
        item = run.corpus.items[i]
        assert m["up_bytes_per_server"] == [len(it.query) for it in item], (
            f"{where}: up_bytes_per_server {m['up_bytes_per_server']} vs corpus query lengths")
        assert m["down_bytes_per_server"] == [it.answer_len for it in item], (
            f"{where}: down_bytes_per_server {m['down_bytes_per_server']} vs EXPECT lengths")
        assert m["up_bytes"] == sum(m["up_bytes_per_server"]) and m["down_bytes"] == sum(m["down_bytes_per_server"])


# ---------------------------------------------------------------- tests: G6

@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_concurrent_correctness(arm, cluster, replay):
    rep = replay(arm)
    cluster(arm).assert_alive("under concurrent load")
    assert not rep.failures, "client failures:\n" + "\n".join(rep.failures[:10])
    assert len(rep.samples) == rep.expected, f"{len(rep.samples)} answers, expected {rep.expected}"
    bad = [s for s in rep.samples if s.problem]
    assert not bad, (f"{len(bad)} of {len(rep.samples)} answers differ from the honest answer, e.g. "
                     + "; ".join(f"client {s.client} server {s.server} req {s.req_id:#x} {s.problem}"
                                 for s in bad[:5]))
    ids = [(s.client, s.server, s.req_id) for s in rep.samples]
    assert len(set(ids)) == len(ids), "an answer was delivered twice"


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_answer_and_queue_ns(arm, replay):
    rep = replay(arm)
    assert rep.samples, "no samples"
    slack_ns = 2_000_000
    for s in rep.samples:
        where = f"client {s.client} server {s.server} req {s.req_id:#x}"
        assert 0 < s.answer_ns < 60_000_000_000, f"{where}: answer_ns {s.answer_ns}"
        assert 0 <= s.queue_ns < 60_000_000_000, f"{where}: queue_ns {s.queue_ns}"
        assert s.queue_ns + s.answer_ns <= s.rtt_ns + slack_ns, (
            f"{where}: queue_ns+answer_ns {s.queue_ns + s.answer_ns} exceeds the client-observed "
            f"round trip {s.rtt_ns}")
    assert max(s.queue_ns for s in rep.samples) > 0, (
        f"queue_ns is 0 for all {len(rep.samples)} answers although {CLIENTS} clients share {WORKERS} workers")
    assert len({s.answer_ns for s in rep.samples}) > 1, "answer_ns is the same constant for every answer"


# ---------------------------------------------------------------- tests: robustness

def _servers_and_queries(arm, cluster, client_run):
    return cluster(arm), client_run(arm).corpus


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_wrong_length_query(arm, cluster, client_run):
    cl, corpus = _servers_and_queries(arm, cluster, client_run)
    for s, srv in enumerate(cl.servers):
        good = corpus.items[0][s].query
        variants = {"one byte short": good[:-1], "one byte long": good + b"\0", "empty": b""}
        conn = Conn(srv.addr)
        conn.hello()
        for k, (what, bad) in enumerate(variants.items()):
            bad_id, good_id = 0xBAD00 + k, 0x600D00 + k
            conn.send_query(bad_id, bad)
            conn.send_query(good_id, good)
            got = {}
            for _ in range(2):
                ftype, body = conn.recv()
                if ftype == T_ERROR:
                    got["error"] = parse_error(body)
                elif ftype == T_ANSWER:
                    got["answer"] = struct.unpack_from(">Q", body)[0]
                    assert hashlib.sha256(body[24:]).digest() == corpus.items[0][s].sha256
                else:
                    pytest.fail(f"{what}: unexpected frame type {ftype:#x}")
            assert got.get("error", (None,))[0] == bad_id, (
                f"server {s}, {what}: ERROR frame {got.get('error')}, want req_id {bad_id:#x}")
            assert got.get("answer") == good_id, f"server {s}, {what}: valid query on the same connection unanswered"
        srv.assert_alive("after wrong-length queries")
        _answers_correctly(cl, corpus, conn, s, 77)
        conn.close()
        other = Conn(srv.addr)
        other.hello()
        _answers_correctly(cl, corpus, other, s, 78)
        other.close()


# A server accepts only HELLO, QUERY and STATS_REQ; EXPECT and server-to-client types are unknown to it.
@pytest.mark.parametrize("ftype", [0x00, 0x04, 0x10, 0x7E], ids=["type-0x00", "type-0x04", "type-0x10", "type-0x7e"])
@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_unknown_frame_type(arm, ftype, cluster, client_run):
    cl, corpus = _servers_and_queries(arm, cluster, client_run)
    for s, srv in enumerate(cl.servers):
        conn = Conn(srv.addr, timeout=15)
        conn.hello()
        conn.send(ftype, b"junk")
        got, body = conn.recv()
        assert got == T_ERROR, f"server {s}: unknown type answered with frame type {got:#x}"
        rid, msg = parse_error(body)
        assert rid == 0, f"server {s}: ERROR req_id {rid}, want 0 for the connection"
        _wait_closed(conn, f"server {s} after an unknown frame type")
        conn.close()
        srv.assert_alive("after an unknown frame type")
        other = Conn(srv.addr)
        other.hello()
        _answers_correctly(cl, corpus, other, s, 5)
        other.close()


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_oversize_frame(arm, cluster, client_run):
    cl, corpus = _servers_and_queries(arm, cluster, client_run)
    for s, srv in enumerate(cl.servers):
        conn = Conn(srv.addr, timeout=5)
        conn.hello()
        conn.send_raw(struct.pack(">I", MAX_FRAME + 1))
        try:
            got, body = conn.recv()
        except socket.timeout:
            conn.send_raw(bytes([T_QUERY]))
            conn.sock.settimeout(10)
            try:
                got, body = conn.recv()
            except socket.timeout:
                pytest.fail(f"server {s}: no ERROR within 15s of a frame header claiming > 1 GiB")
        assert got == T_ERROR, f"server {s}: oversize frame answered with type {got:#x}"
        rid, _ = parse_error(body)
        assert rid == 0, f"server {s}: ERROR req_id {rid}, want 0"
        _wait_closed(conn, f"server {s} after an oversize frame")
        conn.close()
        srv.assert_alive("after a frame claiming more than 1 GiB")
        other = Conn(srv.addr)
        other.hello()
        _answers_correctly(cl, corpus, other, s, 6)
        other.close()


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_client_vanishes_mid_flight(arm, cluster, client_run):
    cl, corpus = _servers_and_queries(arm, cluster, client_run)
    for s, srv in enumerate(cl.servers):
        probe = Conn(srv.addr)
        probe.hello()
        before = probe.stats()["answered"]
        conn = Conn(srv.addr)
        conn.hello()
        for k in range(4):
            conn.send_query(1000 + k, corpus.items[k % len(corpus.items)][s].query)
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()
        deadline = time.monotonic() + (15 if arm.name == "S4" else 5)
        while time.monotonic() < deadline and probe.stats()["answered"] < before + 4:
            time.sleep(0.1)
        srv.assert_alive("after a client closed with queries in flight")
        _answers_correctly(cl, corpus, probe, s, 9)
        probe.close()


# ---------------------------------------------------------------- tests: stats

@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_stats_keys(arm, cluster):
    cl = cluster(arm)
    for s, srv in enumerate(cl.servers):
        c = Conn(srv.addr)
        c.hello()
        first = c.stats()
        time.sleep(0.05)
        second = c.stats()
        c.close()
        missing = [k for k in STATS_KEYS if k not in first]
        assert not missing, f"server {s}: STATS missing {missing}; got {sorted(first)}"
        for k in ("wall_s", "cpu_user_s", "cpu_sys_s"):
            assert _is_num(first[k]) and first[k] >= 0, f"server {s}: {k}={first[k]!r}"
        assert first["wall_s"] > 0 and first["cpu_user_s"] + first["cpu_sys_s"] > 0
        for k in ("peak_rss_bytes", "answered", "queued"):
            assert _is_int(first[k]) and first[k] >= 0, f"server {s}: {k}={first[k]!r}"
        assert first["peak_rss_bytes"] > 1 << 20, f"server {s}: peak_rss_bytes {first['peak_rss_bytes']}"
        assert second["wall_s"] > first["wall_s"], "wall_s did not advance"
        assert second["cpu_user_s"] + second["cpu_sys_s"] >= first["cpu_user_s"] + first["cpu_sys_s"]
        assert second["peak_rss_bytes"] >= first["peak_rss_bytes"]


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_stats_answered_counts_up(arm, cluster, client_run):
    cl, corpus = _servers_and_queries(arm, cluster, client_run)
    n = 3
    for s, srv in enumerate(cl.servers):
        c = Conn(srv.addr)
        c.hello()
        before = c.stats()
        for k in range(n):
            _answers_correctly(cl, corpus, c, s, 40 + k, k)
        deadline = time.monotonic() + 5
        after = c.stats()
        while after["answered"] < before["answered"] + n and time.monotonic() < deadline:
            time.sleep(0.05)
            after = c.stats()
        c.close()
        assert after["answered"] >= before["answered"] + n, (
            f"server {s}: answered {before['answered']} -> {after['answered']} after {n} answers")
        assert after["answered"] <= before["answered"] + n, (
            f"server {s}: answered rose by {after['answered'] - before['answered']} for {n} queries")
        assert after["queued"] == 0, f"server {s}: queued={after['queued']} with the server idle"


# ---------------------------------------------------------------- tests: shutdown

def _shutdown_case(arm, cluster, client_run, tmp_path, sig):
    corpus = client_run(arm).corpus
    outs = [tmp_path / f"serve{s}.jsonl" for s in range(arm.servers)]
    for o in outs:
        o.write_text('{"sentinel": true}\n')
    servers = start_servers(arm, 2, outs)
    idle = []
    try:
        for s, srv in enumerate(servers):
            c = Conn(srv.addr)
            c.hello()
            for k in range(3):
                _answers_correctly(None, corpus, c, s, 1 + k, k)
            c.close()
            idle.append(Conn(srv.addr))
            idle[-1].hello()
        for srv in servers:
            rc = srv.stop(sig)
            assert rc is not None, f"server {srv.server_id} still running {STOP_TIMEOUT_S:g}s after the signal"
            assert rc == 0, f"server {srv.server_id} exit code {rc}\nstderr tail:\n{srv.tail()}"
        for s, srv in enumerate(servers):
            lines = outs[s].read_text().splitlines()
            assert lines[0] == '{"sentinel": true}', "the server rewrote --out instead of appending"
            assert len(lines) == 2, f"server {s}: {len(lines) - 1} summary records appended, want exactly 1"
            rec = json.loads(lines[1])
            assert isinstance(rec, dict) and rec.get("arm") == arm.name, f"summary record: {rec}"
            assert rec.get("mode") == "serve", f"summary mode {rec.get('mode')!r}"
            want = [k for k in INFO_KEYS if k != "scheme_params"] + list(STATS_KEYS)
            missing = [k for k in want if k not in rec]
            assert not missing, f"server {s} summary record misses {missing}: {sorted(rec)}"
            assert rec["server_id"] == srv.server_id and rec["queued"] == 0, f"summary record: {rec}"
            assert _is_int(rec["answered"]) and rec["answered"] >= 3, f"summary answered {rec['answered']!r}"
            assert _is_int(rec["peak_rss_bytes"]) and rec["peak_rss_bytes"] > 0, f"summary peak {rec['peak_rss_bytes']!r}"
            with pytest.raises(OSError):
                socket.create_connection(srv.addr, timeout=3).close()
    finally:
        for c in idle:
            c.close()
        for srv in servers:
            srv.kill()


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_sigterm_shutdown(arm, cluster, client_run, tmp_path):
    _shutdown_case(arm, cluster, client_run, tmp_path, signal.SIGTERM)


@pytest.mark.parametrize("arm", FAST_ARMS, ids=_ids(FAST_ARMS))
def test_sigint_shutdown(arm, cluster, client_run, tmp_path):
    _shutdown_case(arm, cluster, client_run, tmp_path, signal.SIGINT)


# ---------------------------------------------------------------- tests: usage

@pytest.mark.parametrize("mode", ["serve", "client"])
@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_fault_is_usage_error(arm, mode):
    argv = [*_base_argv(arm), "--mode", mode, *_common_args(arm), "--fault", "flip_answer_bit"]
    if mode == "serve":
        argv += ["--workers", "1", "--listen", "127.0.0.1:0"]
    else:
        argv += ["--connect", "127.0.0.1:1", "--session-queries", "1", "--out", os.devnull]
    res = _run_once(argv)
    assert res.returncode == 2, f"exit {res.returncode}, want usage error 2: {' '.join(argv)}\n{res.stderr[-800:]}"
    assert "vpir-serve ready" not in res.stderr


@pytest.mark.parametrize("mode", ["serve", "client"])
def test_unsupported_params(mode, tmp_path):
    # LWE+ has a tECC value only for 1 MiB and up (adapter contract), so 64 KiB is unsupported.
    arm = next(a for a in ARMS if a.name == "S3")
    out = tmp_path / "out.jsonl"
    argv = [*_base_argv(arm), "--mode", mode, "--arm", "S3", "--db-bytes", str(1 << 16), "--record-bytes", "0",
            "--seed", str(SEED)]
    if mode == "serve":
        argv += ["--workers", "1", "--listen", "127.0.0.1:0", "--out", str(out)]
        want = 1
    else:
        argv += ["--connect", "127.0.0.1:1", "--session-queries", "3", "--out", str(out)]
        want = 3
    res = _run_once(argv)
    assert res.returncode == 0, f"exit {res.returncode}, want 0 as in bench mode\n{res.stderr[-800:]}"
    assert "vpir-serve ready" not in res.stderr, "unsupported parameters must not print the ready line"
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(recs) == want, f"{len(recs)} records, want {want}"
    for r in recs:
        assert r["status"] == "unsupported_params" and r.get("error"), f"record {r}"
        assert r.get("mode") == mode and r["arm"] == "S3", f"record {r}"


@pytest.mark.parametrize("arm", ARMS, ids=_ids(ARMS))
def test_seed_mismatch_exits_1(arm, cluster, tmp_path):
    cl = cluster(arm)
    probes = []
    for srv in cl.servers:
        c = Conn(srv.addr)
        c.hello()
        probes.append((c, c.stats()["answered"]))
    argv = [*_base_argv(arm), "--mode", "client", *_common_args(arm, SEED + 1),
            "--connect", ",".join(f"{h}:{p}" for h, p in cl.addrs),
            "--session-queries", "2", "--out", str(tmp_path / "out.jsonl")]
    res = _run_once(argv)
    assert res.returncode == 1, (f"exit {res.returncode}, want 1 (fingerprint mismatch): "
                                 f"{' '.join(argv)}\nstderr tail:\n{res.stderr[-800:]}")
    for c, before in probes:
        assert c.stats()["answered"] == before, "the client sent queries before its fingerprint check failed"
        c.close()
    cl.assert_alive("after a client with the wrong seed")
