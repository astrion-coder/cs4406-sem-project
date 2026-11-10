"""Run an adapter and kill it, with its children, if its memory footprint passes a cap.

macOS reports a process's physical footprint (resident plus compressed memory) through
proc_pid_rusage. RSS alone misses compressed pages: an M1 run that reached a 217 GB
footprint never showed more than 5.6 GB RSS.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

import psutil

GIB = 1 << 30
_libproc = ctypes.CDLL("/usr/lib/libproc.dylib")


class _RusageInfoV0(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups",
            "ri_pageins", "ri_wired_size", "ri_resident_size", "ri_phys_footprint",
            "ri_proc_start_abstime", "ri_proc_exit_abstime",
        )
    ]


def footprint_bytes(pid: int) -> int:
    """The physical footprint of one process, or 0 once it has exited."""
    info = _RusageInfoV0()
    if _libproc.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
        return 0
    return info.ri_phys_footprint


def cap_from_env(default_gib: float) -> int:
    return int(float(os.environ.get("VPIR_ADAPTER_MEM_CAP_GIB", default_gib)) * GIB)


@dataclass(frozen=True)
class Finished:
    returncode: int
    stdout: str
    stderr: str
    peak_footprint_bytes: int  # the largest sum over the process tree that a poll saw
    over_cap: bool  # killed because the footprint passed the cap


def _tree(proc: psutil.Process) -> list[psutil.Process]:
    try:
        return [proc, *proc.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def _kill(tree: list[psutil.Process]) -> None:
    for p in reversed(tree):
        try:
            p.send_signal(signal.SIGKILL)
        except psutil.NoSuchProcess:
            pass


def run(argv, *, timeout_s: float, cap_bytes: int, env: dict | None = None,
        poll_s: float = 0.1) -> Finished:
    """Like subprocess.run(capture_output=True, text=True), plus the footprint cap.

    Raises subprocess.TimeoutExpired after killing the tree, as subprocess.run does.
    """
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        child = subprocess.Popen(argv, stdout=out, stderr=err, text=True, env=env)
        proc = psutil.Process(child.pid)
        deadline = time.monotonic() + timeout_s
        peak, over = 0, False
        while child.poll() is None:
            tree = _tree(proc)
            peak = max(peak, sum(footprint_bytes(p.pid) for p in tree))
            if peak > cap_bytes:
                over = True
                _kill(tree)
                break
            if time.monotonic() > deadline:
                _kill(tree)
                child.wait()
                raise subprocess.TimeoutExpired(argv, timeout_s)
            time.sleep(poll_s)
        child.wait()
        out.seek(0)
        err.seek(0)
        return Finished(child.returncode, out.read(), err.read(), peak, over)
