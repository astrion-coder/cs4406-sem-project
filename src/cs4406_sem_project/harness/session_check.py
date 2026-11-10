"""RQ3a: validate the amortisation model with one session of Q = 100 consecutive queries per focus cell (spec 5).

    uv run python -m cs4406_sem_project.harness.session_check --grid-run <grid_run_id> --run-id <run_id>

The session runs in bench mode, one `--phase offline --reps 1 --warmup 0` process and then one
`--phase online --reps 100 --warmup 0` process whose 100 queries share one client state, because the computed
total comes from bench-mode medians and only bench mode times the offline phase, so the two sides use the same
timers and a disagreement can only come from what the model leaves out: the cold first offline phase and query,
drift over consecutive queries, and the median standing in for the mean. Client mode would add sockets and a
second process, which RQ3b measures on its own, and it rebuilds the offline state untimed, so it could not test
the offline-once term at all.

computed(Q) = median(offline) + Q x median(online) over the grid run's rows of the cell, and measured(Q) = the
session's one offline row + its first Q online rows, for server time, client time, their sum, and bytes. Spec 5
asks for an explanation of any disagreement above 5 % at Q = 100, so those rows are flagged; Q = 1 and 10 are
reported for F8. Results land in results/raw/<run_id>/session/, with comparison.json beside the rows.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import pandas as pd

from cs4406_sem_project.analysis import frame
from cs4406_sem_project.harness import grid as gridmod
from cs4406_sem_project.harness import run_grid, validate
from cs4406_sem_project.harness.grid import Cell
from cs4406_sem_project.harness.run_grid import ContractError, HygieneError, Runner

QUERIES = 100
REPORT_QS = (1, 10, 100)
TOLERANCE = 0.05
# quantity -> (offline column, online column) in analysis.frame's table
QUANTITIES = {
    "server_s": ("server_offline_s", "server_online_s"),
    "client_s": ("client_offline_s", "client_online_s"),
    "bytes": ("offline_bytes", "online_bytes"),
}


def session_cells(grid: dict, tier: str, queries: int) -> list[Cell]:
    """Per focus cell, one cold offline phase and `queries` online repetitions, none of them warm-ups.
    The online timeout grows with the repetitions over the grid's own count."""
    cells = []
    for c in gridmod.expand(grid, tier):
        if c.phase == "offline":
            cells.append(Cell(c.arm, c.db_bytes, c.record_bytes, "offline", 1, 0, c.seed, c.timeout_s, c.binary))
        else:
            scale = max(1.0, queries / c.expected_rows)
            cells.append(Cell(c.arm, c.db_bytes, c.record_bytes, "online", queries, 0, c.seed,
                              c.timeout_s * scale, c.binary))
    return cells


def compare(grid_df: pd.DataFrame, session_df: pd.DataFrame, queries: int = QUERIES,
            qs: tuple[int, ...] = REPORT_QS) -> pd.DataFrame:
    """One row per (cell, Q, quantity): computed and measured totals and their relative difference."""
    def by_cell(df: pd.DataFrame) -> dict[tuple, pd.DataFrame]:
        return {} if df.empty else {k: g for k, g in df.groupby(frame.KEY)}

    grids, sessions = by_cell(grid_df), by_cell(session_df)
    empty = pd.DataFrame(columns=["phase", "rep", *frame.KEY])
    out = []
    for key in sorted(grids.keys() | sessions.keys()):
        g, s = grids.get(key, empty), sessions.get(key, empty)
        g_off, g_on = g[g.phase == "offline"], g[g.phase == "online"]
        s_off, s_on = s[s.phase == "offline"], s[s.phase == "online"].sort_values("rep")
        note = None
        if g_off.empty or g_on.empty:
            note = "the grid run has no ok rows for both phases"
        elif len(s_off) != 1 or len(s_on) != queries:
            note = f"the session has {len(s_off)} offline and {len(s_on)} of {queries} online rows"
        for q in qs:
            totals = {}
            for name, (off_col, on_col) in QUANTITIES.items():
                if note:
                    totals[name] = (math.nan, math.nan)
                    continue
                computed = g_off[off_col].median() + q * g_on[on_col].median()
                measured = s_off[off_col].sum(min_count=1) + s_on[on_col].iloc[:q].sum(min_count=1)
                totals[name] = (computed, measured)
            totals["total_s"] = tuple(totals["server_s"][i] + totals["client_s"][i] for i in (0, 1))
            for name, (computed, measured) in totals.items():
                rel = (measured - computed) / computed if computed else math.nan
                out.append({**dict(zip(frame.KEY, key)), "q": q, "quantity": name, "computed": computed,
                            "measured": measured, "rel_diff": rel,
                            "flagged": bool(q == queries and not math.isnan(rel) and abs(rel) > TOLERANCE),
                            "note": note})
    return pd.DataFrame(out)


def report(table: pd.DataFrame, queries: int = QUERIES) -> str:
    if table.empty:
        return "no cells to compare"
    at_q = table[table.q == queries]
    lines = [f"{'arm':<7} {'db':>7} {'record':>7} {'quantity':<9} {'computed':>14} {'measured':>14} {'diff':>8}"]
    for r in at_q.itertuples():
        rec = gridmod.fmt_size(r.record_bytes) if r.record_bytes else "1bit"
        diff = "n/a" if math.isnan(r.rel_diff) else f"{r.rel_diff:+.1%}"
        mark = "  FLAG > 5 %" if r.flagged else (f"  ({r.note})" if r.note else "")
        lines.append(f"{r.arm:<7} {gridmod.fmt_size(r.db_bytes):>7} {rec:>7} {r.quantity:<9} "
                     f"{r.computed:>14.6g} {r.measured:>14.6g} {diff:>8}{mark}")
    n = int(at_q.flagged.sum())
    lines.append(f"Q = {queries}: {n} of {len(at_q)} totals disagree by more than {TOLERANCE:.0%}; "
                 "spec 5 asks for an explanation of each" if n else
                 f"Q = {queries}: every total agrees within {TOLERANCE:.0%}")
    return "\n".join(lines)


def load_frame(run_dir: Path, reference_platform: str) -> pd.DataFrame:
    df, _status = frame.load_runs([run_dir], reference_platform)
    return df


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--grid-run", required=True, help="results/raw/GRID_RUN: the run whose medians give computed")
    p.add_argument("--run-id", help="results/raw/RUN_ID; default a new id. Reuse it to resume")
    p.add_argument("--tier", default="focus")
    p.add_argument("--queries", type=int, default=QUERIES)
    p.add_argument("--compare-only", action="store_true", help="skip the runs, recompute the comparison")
    p.add_argument("--grid", type=Path, default=gridmod.GRID_PATH)
    p.add_argument("--results", type=Path, default=run_grid.RESULTS)
    p.add_argument("--adapter-dir", type=Path, default=run_grid.ADAPTER_DIR)
    p.add_argument("--reference-platform", type=Path, default=run_grid.REFERENCE_PLATFORM)
    p.add_argument("--launcher", default="", help="command prefix for every adapter run (tests use it)")
    args = p.parse_args(argv)

    grid_dir = args.results / args.grid_run
    if not grid_dir.is_dir():
        print(f"{grid_dir} does not exist", file=sys.stderr)
        return run_grid.EXIT_USAGE
    if not args.reference_platform.exists():
        print(f"{args.reference_platform} is missing; run `make sysinfo` on the reference Mac", file=sys.stderr)
        return run_grid.EXIT_USAGE
    reference = validate.read_reference_platform(args.reference_platform)
    grid = gridmod.load(args.grid)
    run_id = args.run_id or run_grid.new_run_id("p6")
    run_dir = args.results / run_id / "session"

    if not args.compare_only:
        cells = session_cells(grid, args.tier, args.queries)
        runner = Runner(
            cells=cells, run_dir=run_dir, run_id=run_id, platform=run_grid.current_platform(),
            reference_platform=reference, adapter_dir=args.adapter_dir,
            mem_cap_bytes=int(grid["mem_cap_gib"] * run_grid.GIB), harness_sha=run_grid.git_sha(),
            launcher=run_grid.split_command(args.launcher), program="session_check",
        )
        try:
            run_grid.write_plan(run_dir, cells, {
                "run_id": run_id, "tier": args.tier, "experiment": "RQ3a", "queries": args.queries,
                "grid_run": args.grid_run, "git_sha_harness": runner.harness_sha, "platform": runner.platform,
                "started": dt.datetime.now(dt.timezone.utc).isoformat()})
            counts = runner.run()
        except HygieneError as e:
            print(f"stopped, measurement hygiene broken: {e}\nfix it, then rerun with --run-id {run_id}",
                  file=sys.stderr)
            return run_grid.EXIT_HYGIENE
        except ContractError as e:
            print(f"stopped: {e}", file=sys.stderr)
            return run_grid.EXIT_ERROR
        print(f"{run_id} session: {counts}", file=sys.stderr)

    table = compare(load_frame(grid_dir, reference), load_frame(run_dir, reference), args.queries)
    (run_dir / "comparison.json").write_text(json.dumps(
        {"grid_run": args.grid_run, "session_run": run_id, "queries": args.queries, "tolerance": TOLERANCE,
         "rows": json.loads(table.to_json(orient="records"))}, indent=1) + "\n")
    print(report(table, args.queries))
    return run_grid.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
