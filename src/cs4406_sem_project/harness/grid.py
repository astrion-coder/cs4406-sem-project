"""Expand configs/grid.yaml into cells. The only place sizes become bytes or bits (spec 5).

    uv run python -m cs4406_sem_project.harness.grid --tier tier1
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
GRID_PATH = ROOT / "configs" / "grid.yaml"

ARMS = ("S0-go", "S0-cpp", "S1", "S2", "S3", "S4", "M0", "M1")
ONE_BIT_ARMS = frozenset({"S3", "S4"})
# S0-go also retrieves one bit, but only as the SimplePIR README anchor (decision 21).
ONE_BIT_CAPABLE = ONE_BIT_ARMS | {"S0-go"}
# vsp-adapter's arms, the only ones whose offline repetitions can leave the forked child (decision 27).
IN_PROCESS_CAPABLE = frozenset({"S0-cpp", "S1", "S2"})
PHASES = ("offline", "online")
UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30}
_SIZE = re.compile(r"^\s*(\d+)\s*(B|KiB|MiB|GiB)\s*$")
BITS_PER_BYTE = 8


def parse_size(text: str | int) -> int:
    """'16MiB' -> 16777216. Integers pass through as bytes."""
    if isinstance(text, int):
        return text
    m = _SIZE.match(text)
    if not m:
        raise ValueError(f"size {text!r} is not <int><B|KiB|MiB|GiB>")
    return int(m.group(1)) * UNITS[m.group(2)]


def fmt_size(n: int) -> str:
    for unit in ("GiB", "MiB", "KiB"):
        if n >= UNITS[unit] and n % UNITS[unit] == 0:
            return f"{n // UNITS[unit]}{unit}"
    return f"{n}B"


def to_bits(n_bytes: int) -> int:
    return n_bytes * BITS_PER_BYTE


@dataclass(frozen=True, order=True)
class Cell:
    """One adapter invocation: an arm, a database and record size, and one phase."""

    arm: str
    db_bytes: int
    record_bytes: int | None  # None for the one-bit arms S3 and S4
    phase: str
    reps: int
    warmup: int
    seed: int
    timeout_s: float
    binary: str
    offline_in_process: bool = False

    @property
    def cell_id(self) -> str:
        rec = "1bit" if self.record_bytes is None else fmt_size(self.record_bytes)
        return f"{self.arm}_{fmt_size(self.db_bytes)}_{rec}_{self.phase}"

    @property
    def db_bits(self) -> int:
        return to_bits(self.db_bytes)

    @property
    def retrieved_bits(self) -> int:
        return 1 if self.record_bytes is None else to_bits(self.record_bytes)

    @property
    def n_records(self) -> int:
        return self.db_bits if self.record_bytes is None else self.db_bytes // self.record_bytes

    @property
    def expected_rows(self) -> int:
        return self.warmup + self.reps

    def adapter_args(self, out: Path) -> list[str]:
        """The spec 9 command line after the binary. S3 and S4 take --record-bytes 0."""
        return [
            "--arm", self.arm,
            "--db-bytes", str(self.db_bytes),
            "--record-bytes", str(self.record_bytes or 0),
            "--phase", self.phase,
            "--reps", str(self.reps),
            "--warmup", str(self.warmup),
            "--seed", str(self.seed),
            "--fault", "none",
            "--recovery", "abort",
            "--out", str(out),
        ] + (["--offline-in-process"] if self.offline_in_process else [])

    def to_dict(self) -> dict:
        d = {"cell_id": self.cell_id, "arm": self.arm, "db_bytes": self.db_bytes,
             "record_bytes": self.record_bytes, "phase": self.phase, "reps": self.reps,
             "warmup": self.warmup, "seed": self.seed, "timeout_s": self.timeout_s,
             "binary": self.binary}
        # Only flagged cells carry the key, so plans written before decision 27 still match on resume.
        if self.offline_in_process:
            d["offline_in_process"] = True
        return d


def _by_size(table: dict | int | float, db_bytes: int) -> float:
    if not isinstance(table, dict):
        return table
    for key, value in table.items():
        if key != "default" and parse_size(key) == db_bytes:
            return value
    if "default" not in table:
        raise ValueError(f"no entry for {fmt_size(db_bytes)} and no default in {table}")
    return table["default"]


def load(path: Path = GRID_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def expand(grid: dict, tier: str) -> list[Cell]:
    if tier not in grid["tiers"]:
        raise KeyError(f"tier {tier!r} not in grid; have {sorted(grid['tiers'])}")
    t = grid["tiers"][tier]
    reps, seed, warmup = t["reps"], grid["seed"], grid["warmup"]

    def timeout(arm: str, db: int) -> float:
        table = grid["timeout_s"].get(arm, grid["timeout_s"]["default"])
        return float(_by_size(table, db))

    in_process = {arm: set(map(parse_size, sizes)) for arm, sizes in (grid.get("offline_in_process") or {}).items()}
    if set(in_process) - IN_PROCESS_CAPABLE:
        raise ValueError(f"offline_in_process lists {sorted(set(in_process) - IN_PROCESS_CAPABLE)};"
                         f" only {sorted(IN_PROCESS_CAPABLE)} can run it")

    def cell(arm: str, db: int, rec: int | None, phase: str) -> Cell:
        n = reps["online"] if phase == "online" else _by_size(reps["offline"], db)
        return Cell(arm, db, rec, phase, int(n), warmup, seed, timeout(arm, db), grid["adapters"][arm],
                    phase == "offline" and db in in_process.get(arm, set()))

    cells = []
    sweep = t.get("sweep", {})
    for arm in sweep.get("arms", []):
        if arm in ONE_BIT_ARMS:
            raise ValueError(f"{arm} retrieves one bit; list it under one_bit, not sweep")
        for db in map(parse_size, sweep["db_bytes"]):
            for rec in map(parse_size, sweep["record_bytes"]):
                if rec > db:
                    raise ValueError(f"record {fmt_size(rec)} larger than database {fmt_size(db)}")
                cells += [cell(arm, db, rec, ph) for ph in PHASES]
    for arm, sizes in (t.get("one_bit") or {}).items():
        if arm not in ONE_BIT_CAPABLE:
            raise ValueError(f"{arm} cannot retrieve one bit")
        for db in map(parse_size, sizes or []):
            cells += [cell(arm, db, None, ph) for ph in PHASES]
    for c in cells:
        if c.arm not in ARMS:
            raise ValueError(f"unknown arm {c.arm}")
    ids = [c.cell_id for c in cells]
    if len(ids) != len(set(ids)):
        raise ValueError(f"tier {tier} lists a cell twice")
    return cells


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--grid", type=Path, default=GRID_PATH)
    p.add_argument("--tier", default="tier1")
    args = p.parse_args()
    cells = expand(load(args.grid), args.tier)
    for c in cells:
        print(c.cell_id, c.reps, c.warmup, c.timeout_s)
    print(f"{len(cells)} (cell, phase) runs, {len({c.cell_id.rsplit('_', 1)[0] for c in cells})} cells")


if __name__ == "__main__":
    main()
