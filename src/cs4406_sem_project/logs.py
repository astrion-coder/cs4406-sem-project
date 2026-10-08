"""Progress logging for long-running commands.

Every CLI command appends to ``data/logs/<command>.log`` (follow it with ``tail -f``):

* ``Progress`` logs done/total, percent, rate and ETA at most every ``interval`` seconds.
* ``heartbeat`` logs "still running" lines (elapsed time, memory) for steps that report no
  progress of their own, such as bm25s building its score matrix.
"""

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .paths import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
DEFAULT_INTERVAL = 30.0

_configured: set[Path] = set()


def setup(command: str, extra_loggers: tuple[str, ...] = ()) -> Path:
    """Send the project's log records (and ``extra_loggers``, e.g. ir_datasets) to
    ``data/logs/<command>.log`` and to stderr. Returns the log path."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{command}.log"
    if path in _configured:
        return path
    _configured.add(path)
    file_handler = logging.FileHandler(path)
    file_handler.setFormatter(logging.Formatter(FORMAT))
    for name in ("cs4406", *extra_loggers):
        logger = logging.getLogger(name)
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
    root = logging.getLogger("cs4406")
    if not any(getattr(h, "_cs4406_console", False) for h in root.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(FORMAT))
        console._cs4406_console = True
        root.addHandler(console)
    root.info("=" * 20 + f" {' '.join(sys.argv)} (pid {os.getpid()})")
    return path


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"cs4406.{name}")


def memory() -> str:
    """Resident memory of this process and system-wide available memory."""
    import psutil

    rss = psutil.Process().memory_info().rss
    avail = psutil.virtual_memory().available
    return f"rss {rss / 2**30:.2f} GB, avail {avail / 2**30:.2f} GB"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


class Progress:
    def __init__(self, log: logging.Logger, label: str, total: int | None = None,
                 unit: str = "items", interval: float = DEFAULT_INTERVAL):
        self.log, self.label, self.total, self.unit, self.interval = log, label, total, unit, interval
        self.done = 0
        self.t0 = self._last = time.time()
        log.info(f"START {label}" + (f" ({total:,} {unit})" if total else ""))

    def update(self, n: int = 1) -> None:
        self.done += n
        now = time.time()
        if now - self._last >= self.interval:
            self._last = now
            self.log.info(self._status(now))

    def _status(self, now: float) -> str:
        elapsed = now - self.t0
        rate = self.done / elapsed if elapsed > 0 else 0.0
        msg = f"{self.label}: {self.done:,}"
        if self.total:
            msg += f" / {self.total:,} {self.unit} ({100 * self.done / self.total:.1f}%)"
        else:
            msg += f" {self.unit}"
        msg += f", {rate:,.1f} {self.unit}/s, elapsed {fmt_duration(elapsed)}"
        if self.total and rate > 0:
            msg += f", ETA {fmt_duration((self.total - self.done) / rate)}"
        return f"{msg}, {memory()}"

    def finish(self, detail: str = "") -> None:
        elapsed = time.time() - self.t0
        self.log.info(f"DONE  {self.label}: {self.done:,} {self.unit} in {fmt_duration(elapsed)}"
                      + (f"; {detail}" if detail else "") + f", {memory()}")

    def track(self, iterable: Iterable) -> Iterator:
        for item in iterable:
            yield item
            self.update()
        self.finish()


@contextmanager
def heartbeat(log: logging.Logger, label: str, interval: float = DEFAULT_INTERVAL,
              status: Callable[[], str] | None = None):
    """Log START/DONE around a block, and a 'still running' line every ``interval`` seconds."""
    stop = threading.Event()
    t0 = time.time()

    def beat():
        while not stop.wait(interval):
            extra = f", {status()}" if status else ""
            log.info(f"... {label}: running {fmt_duration(time.time() - t0)}{extra}, {memory()}")

    log.info(f"START {label}, {memory()}")
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    except BaseException:
        log.exception(f"FAIL  {label} after {fmt_duration(time.time() - t0)}")
        raise
    finally:
        stop.set()
        thread.join()
    log.info(f"DONE  {label} in {fmt_duration(time.time() - t0)}, {memory()}")
