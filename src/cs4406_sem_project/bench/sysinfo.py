"""Machine state that must be recorded (and in some cases enforced) for every measurement run."""

import os
import platform
import socket
from glob import glob
from importlib import metadata
from pathlib import Path

THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
    "RAYON_NUM_THREADS",
)
TRACKED_PACKAGES = ("bm25s", "numpy", "numba", "ir_datasets", "faiss-cpu", "pyseismic-lsr", "torch", "sentence-transformers")


def governors() -> set[str]:
    return {Path(p).read_text().strip() for p in glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")}


def check_governor(allow_unpinned: bool = False) -> None:
    govs = governors()
    if govs != {"performance"} and not allow_unpinned:
        raise RuntimeError(
            f"CPU governor is {sorted(govs)}, not 'performance'. Run "
            "`sudo cpupower frequency-set -g performance` or pass allow_unpinned=True "
            "(results will then be labelled unpinned)."
        )


def apply_thread_limits(threads: int) -> None:
    """Cap intra-op threads. Set in the parent before workers are spawned so they inherit it."""
    for var in THREAD_ENV_VARS:
        os.environ[var] = str(threads)


def _cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor()


def _versions() -> dict[str, str]:
    out = {}
    for pkg in TRACKED_PACKAGES:
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    return out


def snapshot() -> dict:
    import psutil

    return {
        "hostname": socket.gethostname(),
        "cpu_model": _cpu_model(),
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpus": psutil.cpu_count(logical=False),
        "governors": sorted(governors()),
        "mem_total_bytes": psutil.virtual_memory().total,
        "mem_available_bytes": psutil.virtual_memory().available,
        "loadavg_1m": os.getloadavg()[0],
        "python": platform.python_version(),
        "kernel": platform.release(),
        "thread_env": {v: os.environ.get(v) for v in THREAD_ENV_VARS},
        "packages": _versions(),
    }


def pss_bytes(pids: list[int]) -> int:
    """Proportional set size summed over processes: shared mmap'd index pages are counted once
    in total rather than once per worker (which RSS would do)."""
    import psutil

    total = 0
    for pid in pids:
        try:
            total += psutil.Process(pid).memory_full_info().pss
        except psutil.Error:
            pass
    return total


def drop_page_cache(files: list[Path]) -> None:
    """Evict files from the OS page cache (no root needed). Only effective for pages that no
    live process has mapped, so call it before workers load the index."""
    for f in files:
        fd = os.open(f, os.O_RDONLY)
        try:
            os.fsync(fd)  # dirty pages are not evicted by DONTNEED
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
