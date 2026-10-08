"""Download every dataset the project uses, with a progress log.

ir_datasets downloads lazily on first access; this module forces each access in turn and runs a
watcher thread that logs every in-flight download (bytes, rate, ETA) to ``data/logs/download.log``.

    uv run cs4406-sem-project download          # tail -f data/logs/download.log
"""

import json
import os
import threading
import time
from pathlib import Path

from . import data

from . import logs

log = logs.get("download")


def _size_hints() -> dict[str, int]:
    """Map download file name -> expected size in bytes, from ir_datasets' download registry."""
    import ir_datasets

    registry = json.loads((Path(ir_datasets.__file__).parent / "etc" / "downloads.json").read_text())
    hints = {}

    def walk(node):
        if isinstance(node, dict):
            if "cache_path" in node and "size_hint" in node:
                hints[Path(node["cache_path"]).name] = node["size_hint"]
            for v in node.values():
                walk(v)

    walk(registry)
    return hints


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024


def _watch(root: Path, stop: threading.Event, interval: float) -> None:
    """Log progress of every partially downloaded file (ir_datasets writes ``<name>.tmp``)."""
    hints = _size_hints()
    last: dict[Path, tuple[float, int]] = {}
    while not stop.wait(interval):
        for tmp in sorted(root.rglob("*.tmp")):
            try:
                size = tmp.stat().st_size
            except FileNotFoundError:
                continue
            now = time.time()
            prev_t, prev_size = last.get(tmp, (now, size))
            last[tmp] = (now, size)
            rate = (size - prev_size) / (now - prev_t) if now > prev_t else 0.0
            total = hints.get(tmp.name.removesuffix(".tmp"))
            msg = f"downloading {tmp.relative_to(root)}: {_fmt(size)}"
            if total:
                msg += f" / {_fmt(total)} ({100 * size / total:.1f}%)"
            msg += f" at {_fmt(rate)}/s"
            if total and rate > 0:
                msg += f", ETA {(total - size) / rate / 60:.0f} min"
            log.info(msg)


def _steps(include_beir: bool):
    def corpus(dataset_id):
        # Building the docstore needs the full collection: downloads, extracts and indexes it.
        store = data.docstore(dataset_id)
        n = data.docs_count(dataset_id)
        first = next(data.iter_docs(dataset_id))[0]
        store.get(first)
        return f"{n:,} docs"

    def topics(dataset_id):
        return f"{len(data.queries(dataset_id)):,} queries, {len(data.qrels(dataset_id)):,} qrels"

    yield "MS MARCO passage corpus", lambda: corpus(data.CORPUS)
    yield "MS MARCO dev/small (load-test queries)", lambda: f"{len(data.queries(data.LOAD_QUERIES)):,} queries"
    for name, dataset_id in data.EVAL_SETS.items():
        yield f"TREC {name}", lambda d=dataset_id: topics(d)
    if include_beir:
        for name, dataset_id in data.BEIR_SETS.items():
            yield f"BEIR {name}", lambda d=dataset_id: f"{corpus(d)}; {topics(d)}"


def download_all(include_beir: bool = True, interval: float = 30.0) -> bool:
    root = Path(os.environ["IR_DATASETS_HOME"])
    stop = threading.Event()
    watcher = threading.Thread(target=_watch, args=(root, stop, interval), daemon=True)
    watcher.start()
    ok = True
    t_all = time.time()
    try:
        for label, step in _steps(include_beir):
            log.info(f"START {label}")
            t0 = time.time()
            try:
                detail = step()
                log.info(f"DONE  {label}: {detail} ({(time.time() - t0) / 60:.1f} min)")
            except Exception:
                ok = False
                log.exception(f"FAIL  {label}")
    finally:
        stop.set()
        watcher.join()
    log.info(f"{'ALL DONE' if ok else 'FINISHED WITH FAILURES'} in {(time.time() - t_all) / 60:.1f} min")
    return ok
