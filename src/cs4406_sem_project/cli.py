"""Command line entry point: ``uv run cs4406-sem-project <command>``.

Every command appends progress to ``data/logs/<command>.log`` (``tail -f`` it).

  download       fetch all datasets
  build-index    build a first-stage index
  effectiveness  nDCG@10 / R@1000 for grid configs on TREC DL 19/20
  run-grid       latency load tests for grid configs x concurrency x repetitions
  summarize      rebuild results/latency_summary.parquet
"""

import argparse
import sys
import time

from . import config as cfg
from . import logs

log = logs.get("cli")


def _grid(arms: list[str]) -> list[cfg.PipelineConfig]:
    return list(cfg.grid(tuple(arms)))


def cmd_download(args) -> None:
    from .download import download_all

    if not download_all(include_beir=not args.skip_beir, interval=args.interval):
        sys.exit(1)


def cmd_build_index(args) -> None:
    if args.index == "bm25":
        from .first_stage import bm25

        out = bm25.build(args.dataset, name=args.name or "bm25", limit=args.limit)
        log.info(f"index written to {out}")
    else:
        sys.exit(f"index type {args.index!r} is not implemented yet")


def cmd_effectiveness(args) -> None:
    from .eval import evaluate_config
    from .pipeline import Pipeline
    from .results import save_effectiveness

    for config in _grid(args.arms):
        pipeline = Pipeline(config, mmap=not args.no_mmap)
        for eval_set in args.sets:
            per_query, _ = evaluate_config(config, eval_set, pipeline=pipeline)
            row = save_effectiveness(config, eval_set, per_query)
            metrics = {k: round(v, 4) for k, v in row.items() if "@" in k}
            log.info(f"{config.config_id} {eval_set}: {metrics}")


def _run_upper_bound_s(load: cfg.LoadConfig) -> float:
    # Every run stops at max_seconds; warm-up is assumed to run at the same rate; +30 s startup.
    return load.max_seconds * (1 + load.warmup_queries / load.max_queries) + 30


def cmd_run_grid(args) -> None:
    from . import data
    from .results import latency_run_exists, run_name

    serving = cfg.ServingConfig(args.workers, args.threads, args.backend)
    configs = _grid(args.arms)
    plan = [
        (config, cfg.LoadConfig(c, args.warmup, args.max_queries, args.max_seconds, cache, seed=rep, serving=serving), rep)
        for config in configs
        for c in args.concurrency
        for cache in args.cache
        for rep in range(args.reps)
    ]
    mmap = not args.no_mmap
    todo = [p for p in plan if args.overwrite or not latency_run_exists(p[0].config_id, run_name(p[1].concurrency, p[1].cache, p[2], mmap))]
    bound_h = sum(_run_upper_bound_s(load) for _, load, _ in todo) / 3600
    log.info(f"{len(configs)} configs, {len(plan)} runs, {len(todo)} not yet measured; upper bound {bound_h:.1f} h")
    if args.dry_run:
        return

    from .bench.load import run_load
    from .results import save_latency_run

    pool = data.queries(data.LOAD_QUERIES)
    t_grid = time.perf_counter()
    for i, (config, load, rep) in enumerate(todo, 1):
        name = run_name(load.concurrency, load.cache, rep, mmap)
        log.info(f"[{i}/{len(todo)}] START {config.config_id} {name}")
        t0 = time.perf_counter()
        df, meta = run_load(config, load, pool, mmap=mmap, allow_unpinned=args.allow_unpinned)
        save_latency_run(config.config_id, name, df, meta)
        p95 = df["latency_ns"].quantile(0.95) / 1e6
        elapsed = time.perf_counter() - t_grid
        eta = elapsed / i * (len(todo) - i)
        log.info(f"[{i}/{len(todo)}] DONE  {config.config_id} {name}: n={len(df)} p95={p95:.1f} ms "
                 f"qps={meta['achieved_qps']:.1f} ({time.perf_counter() - t0:.0f} s); "
                 f"grid elapsed {logs.fmt_duration(elapsed)}, ETA {logs.fmt_duration(eta)}")
    cmd_summarize(args)


def cmd_summarize(args) -> None:
    from .results import build_latency_summary

    summary = build_latency_summary()
    log.info(f"latency_summary.parquet: {len(summary)} runs")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cs4406-sem-project")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="fetch all datasets")
    d.add_argument("--skip-beir", action="store_true")
    d.add_argument("--interval", type=float, default=30.0, help="seconds between progress lines")
    d.set_defaults(func=cmd_download)

    b = sub.add_parser("build-index", help="build a first-stage index")
    b.add_argument("index", choices=["bm25"])
    b.add_argument("--dataset", default="msmarco-passage")
    b.add_argument("--name", help="index directory name (default: the index type)")
    b.add_argument("--limit", type=int, help="index only the first N documents")
    b.set_defaults(func=cmd_build_index)

    e = sub.add_parser("effectiveness", help="effectiveness on TREC DL")
    e.add_argument("--arms", nargs="+", default=["A2"])
    e.add_argument("--sets", nargs="+", default=["dl19", "dl20"])
    e.add_argument("--no-mmap", action="store_true")
    e.set_defaults(func=cmd_effectiveness)

    r = sub.add_parser("run-grid", help="latency load tests")
    r.add_argument("--arms", nargs="+", default=["A2"])
    r.add_argument("--concurrency", nargs="+", type=int, default=list(cfg.CONCURRENCY_LEVELS))
    r.add_argument("--cache", nargs="+", choices=["warm", "cold"], default=["warm"])
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--warmup", type=int, default=500)
    r.add_argument("--max-queries", type=int, default=2000)
    r.add_argument("--max-seconds", type=float, default=600.0)
    r.add_argument("--workers", type=int, default=cfg.ServingConfig.workers)
    r.add_argument("--threads", type=int, default=cfg.ServingConfig.threads_per_worker)
    r.add_argument("--backend", choices=["process", "thread"], default=cfg.ServingConfig.backend)
    r.add_argument("--no-mmap", action="store_true", help="load indexes fully into RAM")
    r.add_argument("--overwrite", action="store_true", help="re-measure runs that already exist")
    r.add_argument("--allow-unpinned", action="store_true", help="run even if the CPU governor is not 'performance'")
    r.add_argument("--dry-run", action="store_true", help="only print the plan and its time bound")
    r.set_defaults(func=cmd_run_grid)

    s = sub.add_parser("summarize", help="rebuild results/latency_summary.parquet")
    s.set_defaults(func=cmd_summarize)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # ir_datasets logs its own downloads/extraction, which any command may trigger.
    path = logs.setup(args.command, extra_loggers=("ir_datasets",))
    print(f"logging to {path}", file=sys.stderr)
    args.func(args)
