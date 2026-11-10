"""Schema-valid synthetic rows for building and testing the analysis before real runs exist (plan SG-5).

The cost model below is invented. Its numbers are not estimates of any scheme and must never reach a
figure or claim: every row carries platform "synthetic", which the reference-platform check refuses.
What the model does guarantee is a known answer. Each verifiable arm's online server time is exactly
TRUE_SERVER_OVERHEAD times its baseline's before noise, so tests can check that the analysis
recovers a planted ratio. Likewise each emulated-link client row's end-to-end latency is
TRUE_EMULATION_FACTOR[profile] times the noise-free modelled latency (amortise.latency_e2e) of its cell.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from cs4406_sem_project.analysis import amortise
from cs4406_sem_project.harness import loadgen
from cs4406_sem_project.harness.grid import Cell
from cs4406_sem_project.harness.validate import ADAPTER_OF, BASELINE_ARMS, FUSED_ARMS, TWO_SERVER_ARMS

PLATFORM = "synthetic"
UPSTREAM_SHA = "0" * 40


@dataclass(frozen=True)
class Model:
    answer_s_per_gib: float  # per server
    comm_factor: float  # online bytes per sqrt(db bits), each direction
    query_gen_s: float
    recover_s: float
    verify_s: float | None  # None when verification is fused into recover
    preproc_s_per_gib: float
    proof_s: float | None
    client_offline_s: float
    hint_factor: float  # offline bytes per sqrt(db bits) x sqrt(db bytes)


MODELS = {
    "S0-go":  Model(0.10, 8.0, 1e-3, 2e-4, 0.0, 30.0, None, 0.0, 1.0),
    "S0-cpp": Model(0.12, 8.0, 1e-3, 2e-4, 0.0, 35.0, None, 0.0, 2.0),
    "S1":     Model(0.12, 8.3, 1.1e-3, 2e-4, 1e-4, 35.0, 2.0, 0.5, 4.0),
    "S2":     Model(0.156, 11.0, 1.2e-3, 2e-4, 2e-4, 35.0, 2.0, 0.6, 5.0),
    "S3":     Model(3.0, 200.0, 5e-2, 1e-2, None, 60.0, None, 0.0, 6.0),
    "S4":     Model(17_000.0, 300.0, 9e-2, 4e-2, None, 340_000.0, None, 0.0, 0.01),
    "M0":     Model(0.30, 0.0, 2e-4, 1e-4, 0.0, 0.0, None, 0.0, 0.0),
    "M1":     Model(0.60, 0.0, 2e-4, 3e-4, None, 25.0, None, 0.0, 0.0),
}
BASELINE_OF = {"S1": "S0-cpp", "S2": "S0-cpp", "M1": "M0"}
TRUE_SERVER_OVERHEAD = {v: MODELS[v].answer_s_per_gib / MODELS[b].answer_s_per_gib for v, b in BASELINE_OF.items()}
GIB = 1 << 30
NOISE_SIGMA = 0.03
TRUE_EMULATION_FACTOR = {"lan": 1.6, "broadband": 1.3, "mobile": 1.15}  # invented, per network profile


def _online_bytes(cell: Cell) -> tuple[list[int], list[int]]:
    m, servers = MODELS[cell.arm], 2 if cell.arm in TWO_SERVER_ARMS else 1
    if cell.arm in TWO_SERVER_ARMS:
        up = math.ceil(cell.n_records / 8)
        proof = 32 * math.ceil(math.log2(max(cell.n_records, 2))) if cell.arm == "M1" else 0
        return [up] * servers, [cell.record_bytes + proof] * servers
    side = int(m.comm_factor * math.sqrt(cell.db_bits))
    return [side], [side + 8 * (cell.record_bytes or 0)]


def _metrics(cell: Cell, rng: np.random.Generator) -> tuple[dict, dict]:
    m = MODELS[cell.arm]
    jitter = lambda x: None if x is None else float(x * rng.lognormal(0.0, NOISE_SIGMA))  # noqa: E731
    keys = ("public_params_s", "server_preproc_s", "server_preproc_proof_s", "client_offline_s",
            "query_gen_s", "client_recover_s", "client_verify_s", "recovery_s")
    out: dict = {k: None for k in keys} | {f"{k[:-2]}_cpu_s": None for k in keys}
    out |= dict.fromkeys(("offline_down_bytes", "offline_down_bytes_hint_only", "offline_up_bytes",
                          "client_storage_bytes", "up_bytes", "up_bytes_per_server", "server_answer_s",
                          "server_answer_cpu_s", "down_bytes", "down_bytes_per_server", "verify_fused",
                          "recovery_up_bytes", "recovery_down_bytes"))
    gib = cell.db_bytes / GIB
    analytical = {"up_bytes": None, "down_bytes": None, "offline_down_bytes_hint_only": None}
    if cell.phase == "offline":
        hint = int(m.hint_factor * math.sqrt(cell.db_bits) * math.sqrt(cell.db_bytes))
        out |= {"public_params_s": jitter(0.01), "server_preproc_s": jitter(m.preproc_s_per_gib * gib),
                "server_preproc_proof_s": jitter(m.proof_s * gib) if m.proof_s else None,
                "client_offline_s": jitter(m.client_offline_s * gib),
                "offline_down_bytes_hint_only": hint, "offline_down_bytes": hint + 32,
                "offline_up_bytes": int(hint * 0.01) if m.proof_s else 0, "client_storage_bytes": hint}
        analytical["offline_down_bytes_hint_only"] = hint
    else:
        up, down = _online_bytes(cell)
        answer = [jitter(m.answer_s_per_gib * gib) for _ in up]
        fused = cell.arm in FUSED_ARMS
        out |= {"query_gen_s": jitter(m.query_gen_s), "up_bytes": sum(up), "up_bytes_per_server": up,
                "server_answer_s": answer, "server_answer_cpu_s": answer, "down_bytes": sum(down),
                "down_bytes_per_server": down, "client_recover_s": jitter(m.recover_s),
                "client_verify_s": None if fused else jitter(m.verify_s), "verify_fused": fused}
        analytical |= {"up_bytes": sum(up), "down_bytes": sum(down)}
    for k in keys:
        out[f"{k[:-2]}_cpu_s"] = out[k]
    out["process_peak_rss_bytes"] = int(64 << 20) + 2 * cell.db_bytes
    return out, analytical


def rows_for(cell: Cell, *, run_id: str = "synthetic", seed: int = 0) -> list[dict]:
    rng = np.random.default_rng([seed, zlib.crc32(cell.cell_id.encode())])
    rows = []
    for rep in range(cell.warmup + cell.reps):
        metrics, analytical = _metrics(cell, rng)
        online = cell.phase == "online"
        rows.append({
            "run_id": run_id, "git_sha_harness": None, "platform": PLATFORM, "upstream_sha": UPSTREAM_SHA,
            "adapter": ADAPTER_OF[cell.arm], "arm": cell.arm, "scheme_params": {"synthetic": True},
            "db_bytes": cell.db_bytes, "record_bytes": cell.record_bytes, "retrieved_bits": cell.retrieved_bits,
            "n_records": cell.n_records, "phase": cell.phase, "rep": rep, "is_warmup": rep < cell.warmup,
            "seed": cell.seed, "fault": "none", "fault_prob": 0, "faulted": False, "recovery": "abort",
            "recovery_outcome": "accepted" if online else None, "recovery_attempts": 0 if online else None,
            "status": "ok", "metrics": metrics, "analytical": analytical,
            "correct": True if online else None,
            "detected": None if cell.arm in BASELINE_ARMS else False, "error": None,
        })
    return rows


def rows_for_cells(cells: Iterable[Cell], *, run_id: str = "synthetic", seed: int = 0) -> list[dict]:
    return [r for c in cells for r in rows_for(c, run_id=run_id, seed=seed)]


def modelled_latency(cell: Cell, profile: amortise.Profile) -> float:
    """amortise.latency_e2e of the cost model above without noise."""
    m = MODELS[cell.arm]
    up, down = _online_bytes(cell)
    return amortise.latency_e2e({
        "query_gen_s": m.query_gen_s, "up_bytes": sum(up), "server_online_s": m.answer_s_per_gib * cell.db_bytes / GIB,
        "down_bytes": sum(down), "client_recover_s": m.recover_s,
        "client_verify_s": None if cell.arm in FUSED_ARMS else m.verify_s}, profile)


def client_rows_for(cell: Cell, profile: amortise.Profile, *, run_id: str = "synthetic", seed: int = 0) -> list[dict]:
    """Client-mode rows as netem.py writes them under netem/<profile>/. Each round trip is what is left of
    TRUE_EMULATION_FACTOR x the modelled latency after the client's own work, with noise."""
    m = MODELS[cell.arm]
    client_s = m.query_gen_s + m.recover_s + (0.0 if cell.arm in FUSED_ARMS else m.verify_s)
    rtt_s = TRUE_EMULATION_FACTOR[profile.name] * modelled_latency(cell, profile) - client_s
    rng = np.random.default_rng([seed, zlib.crc32(f"{cell.cell_id}/{profile.name}".encode())])
    rows = rows_for(cell, run_id=run_id, seed=seed)
    for r in rows:
        servers = len(r["metrics"]["server_answer_s"])
        r["mode"] = "client"
        r["metrics"] |= {"rtt_s": [float(rtt_s * rng.lognormal(0.0, NOISE_SIGMA)) for _ in range(servers)],
                         "rtt_cpu_s": [None] * servers, "server_answer_cpu_s": [None] * servers}
    return rows


LOADGEN_CONTENTION = 0.08  # invented: each worker past the first slows every worker by this share


def service_s(arm: str, db_bytes: int) -> float:
    """One query's server answer time in the cost model; a two-server arm's servers answer in parallel."""
    return MODELS[arm].answer_s_per_gib * db_bytes / GIB


def planted_capacity_qps(arm: str, db_bytes: int, workers: int) -> float:
    """The throughput the synthetic server sustains with `workers` workers. A closed sweep at enough clients
    reaches it, so it is the saturation throughput F10 should mark. Baseline over verifiable capacity is
    TRUE_SERVER_OVERHEAD at every W, which F11 should recover."""
    return workers / (1 + LOADGEN_CONTENTION * (workers - 1)) / service_s(arm, db_bytes)


def _loadgen_row(t: loadgen.Target, workers: int, run: int, load: dict, rng: np.random.Generator, *,
                 clients: int | None, rate: float | None, run_id: str) -> dict:
    jitter = lambda x: float(x * rng.lognormal(0.0, NOISE_SIGMA))  # noqa: E731
    s, cap, window = service_s(t.arm, t.db_bytes), planted_capacity_qps(t.arm, t.db_bytes, workers), load["window_s"]
    if rate is None:  # closed loop with no think time: Little's law gives the latency
        qps = min(clients / s, cap)
        lat, sent = clients / qps, int(qps * window)
    else:
        rho = rate / cap
        qps = min(rate, cap)
        lat = s * (1 + min(rho, 0.95) / (workers * (1 - min(rho, 0.95)))) + window / 2 * max(0.0, 1 - 1 / rho)
        sent = int(rate * window)
    qps, lat = jitter(qps), jitter(lat)
    completed = int(qps * window)
    m = MODELS[t.arm]
    client_s = m.query_gen_s + m.recover_s + (m.verify_s or 0.0)
    return {
        "mode": "loadgen", "run_id": run_id, "git_sha_harness": None, "platform": PLATFORM, "arm": t.arm,
        "db_bytes": t.db_bytes, "record_bytes": t.record_bytes, "seed": t.seed, "workers": workers,
        "loop": "closed" if rate is None else "open", "clients": clients, "offered_load_qps": rate, "run": run,
        "warmup_s": load["warmup_s"], "window_s": window, "sent": sent, "completed": completed, "mismatched": 0,
        "errors": 0, "throughput_qps": qps, "lat_p50_s": lat, "lat_p95_s": 1.25 * lat, "lat_p99_s": 1.5 * lat,
        "e2e_lat_p50_s": lat + client_s, "e2e_lat_p95_s": 1.25 * lat + client_s,
        "e2e_lat_p99_s": 1.5 * lat + client_s, "server_cpu_util": 100.0 * min(qps * s, workers),
        "server_peak_rss_bytes": int(64 << 20) + t.db_bytes, "loadgen_cpu_util": 5.0, "status": "ok", "error": None,
    }


def loadgen_points(targets: Iterable[loadgen.Target], load: dict, *, run_id: str = "synthetic-loadgen",
                   seed: int = 0) -> dict[tuple[str, str], list[dict]]:
    """Rows as harness/loadgen.py lays them out, keyed by (cell_id, point name). The closed sweep follows the
    harness's stop rule and `saturated` status, and the open loop's rates are fractions of the closed
    sweep's measured saturation throughput, both through the harness's own functions."""
    out: dict[tuple[str, str], list[dict]] = {}
    for t in targets:
        rng = np.random.default_rng([seed, zlib.crc32(f"{t.cell_id}/loadgen".encode())])
        done: dict[int, list[dict]] = {}
        while (c := loadgen.next_clients(load, done)) is not None:
            p = next(p for p in loadgen.closed_points(load) if p.clients == c)
            limit = loadgen.p95_limit(load, done)
            rows = [_loadgen_row(t, p.workers, r, load, rng, clients=c, rate=None, run_id=run_id)
                    for r in range(p.runs)]
            for r in rows:
                r["status"] = "saturated" if limit is not None and r["lat_p95_s"] > limit else "ok"
            done[c] = out[(t.cell_id, p.name)] = rows
        sat = loadgen.saturation_qps(done)
        for p in loadgen.open_points(load):
            rows = [_loadgen_row(t, p.workers, r, load, rng, clients=None, rate=p.fraction * sat, run_id=run_id)
                    for r in range(p.runs)]
            for r in rows:
                r["status"] = "saturated" if r["completed"] < 0.9 * r["sent"] else "ok"  # src/loadgen OpenSaturation
            out[(t.cell_id, p.name)] = rows
        for p in loadgen.multicore_points(load):
            out[(t.cell_id, p.name)] = [_loadgen_row(t, p.workers, r, load, rng, clients=p.clients, rate=None,
                                                     run_id=run_id) for r in range(p.runs)]
    return out
