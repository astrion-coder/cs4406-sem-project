"""Check that a timing run kept the measurement conditions of spec §7: awake, on AC power, lid open.

`caffeinate` cannot stop clamshell sleep when no external display is attached, and macOS's monotonic
clock (mach_absolute_time) stops while the machine sleeps. So a run that slept shows normal timed
spans while taking hours of wall time. The run on 10 November that hit this slept from 18:07 with the
lid closed and on battery. `Watch` compares the wall clock, which counts sleep, with the monotonic
clock, which does not, and reads the power source and lid at both ends.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field

# Slack between the two clocks that is not sleep: NTP slews and scheduling noise.
SLEEP_TOLERANCE_S = 2.0


def power_source() -> str:
    """'AC Power' or 'Battery Power', as `pmset -g ps` reports it."""
    out = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True).stdout
    m = re.search(r"drawing from '([^']+)'", out)
    return m.group(1) if m else "unknown"


def lid_closed() -> bool | None:
    out = subprocess.run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"],
                         capture_output=True, text=True).stdout
    m = re.search(r'"AppleClamshellState" = (Yes|No)', out)
    return None if m is None else m.group(1) == "Yes"


@dataclass
class Watch:
    """Wrap one timing run: `with Watch() as w: ...`, then `w.problems` lists what went wrong."""

    wall0: float = 0.0
    mono0: float = 0.0
    wall_s: float = 0.0
    mono_s: float = 0.0
    power: list[str] = field(default_factory=list)
    lid: list[bool | None] = field(default_factory=list)

    def __enter__(self) -> "Watch":
        self.power.append(power_source())
        self.lid.append(lid_closed())
        self.wall0, self.mono0 = time.time(), time.monotonic()
        return self

    def __exit__(self, *exc) -> None:
        self.wall_s, self.mono_s = time.time() - self.wall0, time.monotonic() - self.mono0
        self.power.append(power_source())
        self.lid.append(lid_closed())

    @property
    def slept_s(self) -> float:
        return self.wall_s - self.mono_s

    @property
    def problems(self) -> list[str]:
        out = []
        if self.slept_s > SLEEP_TOLERANCE_S:
            out.append(f"the machine slept for about {self.slept_s:.0f} s during the run")
        if any(p != "AC Power" for p in self.power):
            out.append(f"not on AC power (start, end: {self.power})")
        if any(self.lid):
            out.append("the lid was closed at the start or end")
        return out

    def record(self) -> dict:
        return {"wall_s": self.wall_s, "monotonic_s": self.mono_s, "slept_s": self.slept_s,
                "power": self.power, "lid_closed": self.lid, "problems": self.problems}
