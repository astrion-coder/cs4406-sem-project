"""RQ1: our measured ratios and values against the published ones (table T3).

The targets live in configs/reproduction_targets.yaml, copied from docs/reproduction_targets.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from cs4406_sem_project.analysis import aggregate
from cs4406_sem_project.harness.grid import fmt_size, parse_size

ROOT = Path(__file__).resolve().parents[3]
TARGETS_PATH = ROOT / "configs" / "reproduction_targets.yaml"

UNITS = {"x": 1.0, "s": 1.0, "ms": 1e-3, "KiB": 1024.0, "MiB": float(1 << 20)}
TOLERANCE = {"text": 1.05, "plot": 1.2}  # spec H1 (+-5 %); docs/reproduction_targets.md (log-axis plot reading)
CHECKS = ("ratio", "max_ratio", "trend_up", "less", "abs")
ONLINE = set(aggregate.METRICS["online"])
T3_COLUMNS = ["id", "ref", "where", "claim", "cell", "ours", "ci_lo", "ci_hi", "published", "target",
              "deviation", "verdict", "rule", "check", "unit", "target_lo", "target_hi"]


@dataclass(frozen=True)
class Side:
    arm: str
    record_bytes: int  # 0 for one-bit arms, as in frame.py

    def label(self, db: int) -> str:
        rec = "1 bit" if self.record_bytes == 0 else fmt_size(self.record_bytes)
        return f"{self.arm} {fmt_size(db)} x {rec}"


@dataclass(frozen=True)
class Target:
    id: str
    ref: str
    where: str
    claim: str
    published: str
    check: str
    rule: str
    precision: str
    metric: str
    db: tuple[int, ...]
    num: Side
    den: Side | None
    value: float | None  # value, lo and hi are in unit, as published
    lo: float | None
    hi: float | None
    unit: str
    context: bool

    @property
    def phase(self) -> str:
        return "online" if self.metric in ONLINE else "offline"


def _side(d: dict | None) -> Side | None:
    if d is None:
        return None
    return Side(d["arm"], 0 if d.get("record") is None else parse_size(d["record"]))


def load_targets(path: Path = TARGETS_PATH) -> list[Target]:
    out = []
    for t in yaml.safe_load(path.read_text(encoding="utf-8"))["targets"]:
        if t["check"] not in CHECKS:
            raise ValueError(f"{t['id']}: unknown check {t['check']!r}")
        if t["unit"] not in UNITS:
            raise ValueError(f"{t['id']}: unknown unit {t['unit']!r}")
        lo, hi = t.get("range") or (None, None)
        out.append(Target(
            id=t["id"], ref=t["ref"], where=t["where"], claim=t["claim"], published=t["published"],
            check=t["check"], rule=t["rule"], precision=t["precision"], metric=t["metric"],
            db=tuple(parse_size(s) for s in t["db"]), num=_side(t["num"]), den=_side(t.get("den")),
            value=None if t.get("value") is None else float(t["value"]),
            lo=None if lo is None else float(lo), hi=None if hi is None else float(hi), unit=t["unit"],
            context=bool(t.get("context"))))
    return out


def _values(df: pd.DataFrame, side: Side, db: int, phase: str, metric: str) -> pd.Series:
    sel = df[(df.arm == side.arm) & (df.db_bytes == db) & (df.record_bytes == side.record_bytes)
             & (df.phase == phase)]
    return sel.set_index("rep")[metric].dropna()


def measure(df: pd.DataFrame, t: Target, db: int) -> tuple[float, float, float] | None:
    """The median (abs) or median ratio (all others) at one db size, with its bootstrap 95 % CI, in t.unit."""
    num = _values(df, t.num, db, t.phase, t.metric)
    if t.den is None:
        if num.empty:
            return None
        m = aggregate.paired_bootstrap(num.to_numpy(), np.ones(len(num)), seed=aggregate.seed_for(t.id, db))
        return tuple(v / UNITS[t.unit] for v in m)
    both = pd.concat([num, _values(df, t.den, db, t.phase, t.metric)], axis=1, join="inner")
    if both.empty:
        return None
    return aggregate.paired_bootstrap(both.iloc[:, 0].to_numpy(), both.iloc[:, 1].to_numpy(),
                                      seed=aggregate.seed_for(t.id, db))


def _within(x: float, lo: float | None, hi: float | None, tol: float) -> bool:
    return (lo is None or x >= lo / tol) and (hi is None or x <= hi * tol)


def _deviation(x: float, lo: float | None, hi: float | None) -> float:
    if lo is not None and x < lo:
        return x / lo - 1
    if hi is not None and x > hi:
        return x / hi - 1
    return 0.0


def _target_text(t: Target) -> str:
    if t.check == "trend_up":
        return "non-decreasing in db size"
    if t.check == "less":
        return "< 1"
    if t.check == "max_ratio":
        return f"max <= {t.value:g} {t.unit}"
    if t.value is not None:
        return f"{t.value:g} {t.unit}"
    return f"[{'' if t.lo is None else f'{t.lo:g}'}, {'' if t.hi is None else f'{t.hi:g}'}] {t.unit}"


def _verdict(t: Target, ok: bool) -> str:
    if t.context:
        return "context (different baseline)"
    if t.check == "abs" and t.rule == "hw":
        return "hw factor"
    return "pass" if ok else "explain"


def compare(df: pd.DataFrame, targets: list[Target]) -> pd.DataFrame:
    """Table T3: one row per (target, db size), plus one summary row for max_ratio and trend_up targets."""
    rows = []
    for t in targets:
        tol = TOLERANCE[t.precision]
        base = {"id": t.id, "ref": t.ref, "where": t.where, "claim": t.claim, "published": t.published,
                "target": _target_text(t), "rule": t.rule, "check": t.check, "unit": t.unit,
                "target_lo": t.value if t.check in ("ratio", "abs") and t.value is not None else t.lo,
                "target_hi": t.value if t.value is not None else t.hi}
        measured = {db: measure(df, t, db) for db in t.db}
        for db, m in measured.items():
            cell = t.num.label(db) + ("" if t.den is None else f" / {t.den.label(db)}")
            if m is None:
                rows.append(base | {"cell": cell, "verdict": "missing"})
                continue
            ours, lo, hi = m
            if t.check in ("ratio", "abs"):
                lo_t, hi_t = (t.value, t.value) if t.value is not None else (t.lo, t.hi)
                ok, dev = _within(ours, lo_t, hi_t, tol), _deviation(ours, lo_t, hi_t)
                verdict = _verdict(t, ok)
            elif t.check == "less":
                ok, dev, verdict = ours < 1, np.nan, _verdict(t, ours < 1)
            else:
                dev, verdict = np.nan, "see summary row"
            if t.check == "abs" and t.rule == "hw":
                dev = ours / t.value - 1
            rows.append(base | {"cell": cell, "ours": ours, "ci_lo": lo, "ci_hi": hi, "deviation": dev,
                                "verdict": verdict})
        if t.check in ("max_ratio", "trend_up"):
            got = [m[0] for m in measured.values() if m is not None]
            if len(got) < len(t.db):
                rows.append(base | {"cell": "all listed sizes", "verdict": "missing"})
            elif t.check == "max_ratio":
                top = max(got)
                rows.append(base | {"cell": "max over listed sizes", "ours": top,
                                    "deviation": _deviation(top, None, t.value),
                                    "verdict": _verdict(t, _within(top, None, t.value, tol))})
            else:
                rows.append(base | {"cell": "trend over listed sizes",
                                    "verdict": _verdict(t, bool(np.all(np.diff(got) >= 0)))})
    return pd.DataFrame(rows).reindex(columns=T3_COLUMNS)
