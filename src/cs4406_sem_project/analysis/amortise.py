"""Modelled end-to-end latency, amortised cost per query and break-even Q (spec 6, Derived quantities).

Every function takes per-cell costs with frame.py's column names: a frame row, a whole frame (one
latency per repetition), or medians() from aggregate.py (one value per cell).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[3]
PROFILES_PATH = ROOT / "configs" / "network_profiles.yaml"

ROUNDS = 1  # decision 15: one round trip per query for every arm, LWE+ included
QS = (1, 10, 100, 1000, 10_000)  # spec 5, amortisation horizon
BITS_PER_MBIT = 1e6


@dataclass(frozen=True)
class Profile:
    name: str
    down_mbps: float
    up_mbps: float
    rtt_ms: float


def load_profiles(path: Path = PROFILES_PATH) -> dict[str, Profile]:
    return {name: Profile(name, **values) for name, values in yaml.safe_load(path.read_text()).items()}


def _z(costs, col: str):
    """A component that is structurally absent (fused verify, no client offline work) counts as 0."""
    value = costs[col]
    return value.fillna(0.0) if isinstance(value, pd.Series) else (0.0 if value is None or pd.isna(value) else value)


def latency_e2e(costs, profile: Profile):
    return (costs["query_gen_s"]
            + 8 * costs["up_bytes"] / (profile.up_mbps * BITS_PER_MBIT)
            + profile.rtt_ms / 1e3 * ROUNDS
            + costs["server_online_s"]
            + 8 * costs["down_bytes"] / (profile.down_mbps * BITS_PER_MBIT)
            + costs["client_recover_s"]
            + _z(costs, "client_verify_s"))


def amortised_bytes(costs, q: int):
    return (costs["offline_down_bytes"] + _z(costs, "offline_up_bytes")) / q + costs["up_bytes"] + costs["down_bytes"]


def amortised_client_s(costs, q: int):
    return _z(costs, "client_offline_s") / q + costs["client_online_s"]


def amortised_server_s(costs, q: int):
    return costs["server_offline_s"] / q + costs["server_online_s"]


def amortised_total_s(costs, q: int):
    return amortised_client_s(costs, q) + amortised_server_s(costs, q)


AMORTISED = {"bytes": amortised_bytes, "client_s": amortised_client_s, "server_s": amortised_server_s,
             "total_s": amortised_total_s}


def break_even_Q(a, b, kind: str = "bytes") -> int | None:
    """Smallest integer Q >= 1 at which arm a's amortised cost is <= arm b's, or None if no Q is.

    Each cost is offline/Q + online, so a <= b is off_a - off_b <= Q (on_b - on_a), solved in closed form.
    """
    fn = AMORTISED[kind]
    on_a, on_b = fn(a, math.inf), fn(b, math.inf)
    off_a, off_b = fn(a, 1) - on_a, fn(b, 1) - on_b
    d_off, d_on = off_a - off_b, on_b - on_a
    if d_off <= d_on:
        return 1
    if d_on <= 0:
        return None
    q = math.ceil(d_off / d_on)
    return q if fn(a, q) <= fn(b, q) else q + 1  # guards a float rounding of the ceiling


def amortised_table(costs: pd.DataFrame, qs=QS) -> pd.DataFrame:
    keys = [c for c in ("arm", "db_bytes", "record_bytes") if c in costs]
    return pd.concat([costs[keys].assign(Q=q, **{k: fn(costs, q) for k, fn in AMORTISED.items()}) for q in qs],
                     ignore_index=True)


def latency_table(online: pd.DataFrame, profiles: dict[str, Profile]) -> pd.DataFrame:
    """p50 and p95 of the modelled latency per cell and profile, one latency per online repetition."""
    keys = ["arm", "db_bytes", "record_bytes"]
    parts = []
    for p in profiles.values():
        lat = online[keys].assign(latency_s=latency_e2e(online, p))
        g = lat.groupby(keys)["latency_s"]
        parts.append(pd.DataFrame({"p50": g.median(), "p95": g.quantile(0.95), "n": g.size()})
                     .reset_index().assign(profile=p.name))
    return pd.concat(parts, ignore_index=True)[["profile"] + keys + ["p50", "p95", "n"]]
