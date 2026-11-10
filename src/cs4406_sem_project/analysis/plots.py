"""Figures F1-F6 and tables T1-T4 (spec 12) from raw result rows, in one command.

    uv run python -m cs4406_sem_project.analysis.plots --runs results/raw/<run_id> ... [--out results/figures]
    uv run python -m cs4406_sem_project.analysis.plots --synthetic tier1 --out <dir>

--synthetic builds the same outputs from analysis/synthetic.py rows, and stamps every figure and
table "SYNTHETIC, not a result". Processed tables go to --processed as parquet.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.ticker import EngFormatter  # noqa: E402

from cs4406_sem_project.analysis import aggregate, amortise, frame, reproduce, synthetic  # noqa: E402
from cs4406_sem_project.harness import grid  # noqa: E402
from cs4406_sem_project.harness.grid import fmt_size  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
FIGURES = ROOT / "results" / "figures"
SYSINFO = ROOT / "env" / "sysinfo.json"
SYNTHETIC_MARK = "SYNTHETIC, not a result"

# dataviz reference palette (light): categorical slots in fixed order, one per arm, the same in every figure.
SLOTS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
COLOR = dict(zip(grid.ARMS, SLOTS))
SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
MISSING_FILL = "#f0efec"
DIVERGING = LinearSegmentedColormap.from_list(
    "overhead", ["#104281", "#2a78d6", "#86b6ef", "#f0efec", "#f1a3a2", "#e34948", "#9c2222"])
ONE_BIT = frozenset(grid.ONE_BIT_ARMS)
FOCUS_RECORD = 1 << 10  # spec 5: 1 KiB is the anchor record size
PRICE_PAIRS = [p for p in aggregate.PAIRS if p.role == "price"]
VERIFIABLE_1KIB = ("S1", "S2", "M1")


@dataclass(frozen=True)
class Tables:
    frame: pd.DataFrame
    status: pd.DataFrame
    summary: pd.DataFrame
    medians: pd.DataFrame
    overheads: pd.DataFrame
    amortised: pd.DataFrame
    latency: pd.DataFrame
    t3: pd.DataFrame
    breakdown: pd.DataFrame


def build(df: pd.DataFrame, status: pd.DataFrame) -> Tables:
    summary = aggregate.summarise(df)
    med = aggregate.medians(summary)
    return Tables(
        frame=df, status=status, summary=summary, medians=med, overheads=aggregate.overheads(df),
        amortised=amortise.amortised_table(med),
        latency=amortise.latency_table(df[df.phase == "online"], amortise.load_profiles()),
        t3=reproduce.compare(df, reproduce.load_targets()), breakdown=breakdown(med))


def processed(t: Tables) -> dict[str, pd.DataFrame]:
    return {"frame": t.frame, "status": t.status, "summary": t.summary, "overheads": t.overheads,
            "amortised": t.amortised, "latency": t.latency, "reproduction": t.t3, "breakdown": t.breakdown}


def _cell(med: pd.DataFrame, arm: str, db: int, rec: int) -> pd.Series | None:
    sel = med[(med.arm == arm) & (med.db_bytes == db) & (med.record_bytes == rec)]
    return None if sel.empty else sel.iloc[0]


def focus_db(med: pd.DataFrame, arms=VERIFIABLE_1KIB, rec: int = FOCUS_RECORD) -> int | None:
    """The largest database where every given arm has a 1 KiB cell: 1 GiB on a full grid."""
    have = [set(med[(med.arm == a) & (med.record_bytes == rec)].db_bytes) for a in arms]
    common = set.intersection(*have) if have else set()
    return max(common) if common else None


BREAKDOWN_TIME = {"offline proof (server)": "server_preproc_proof_s",
                  "offline verify (client)": "client_offline_s"}
BASELINE_OF = {p.num: p.den for p in PRICE_PAIRS}


def breakdown(med: pd.DataFrame) -> pd.DataFrame:
    """Where verification cost goes for each price pair at every cell (F6). Extra means verifiable minus
    baseline, and stays signed here; the figure clips a negative part to zero."""
    rows = []
    for v, b in BASELINE_OF.items():
        for _, cv in med[med.arm == v].iterrows():
            cb = _cell(med, b, cv.db_bytes, cv.record_bytes)
            if cb is None:
                continue
            fused = pd.isna(cv.get("client_verify_s"))
            online_verify = (cv.client_recover_s - cb.client_recover_s) if fused else cv.client_verify_s
            rows.append({"arm": v, "baseline": b, "db_bytes": cv.db_bytes, "record_bytes": cv.record_bytes,
                         **{k: _nz(cv.get(col)) for k, col in BREAKDOWN_TIME.items()},
                         "online verify (client)": online_verify, "verify_fused": fused,
                         "extra offline bytes": cv.offline_bytes - cb.offline_bytes,
                         "extra online bytes": cv.online_bytes - cb.online_bytes})
    return pd.DataFrame(rows)


def _nz(x) -> float:
    return 0.0 if x is None or pd.isna(x) else float(x)


def _style() -> None:
    plt.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": ["Segoe UI", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 9, "axes.facecolor": SURFACE, "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.titlecolor": INK, "axes.titlesize": 10,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
        "axes.spines.top": False, "axes.spines.right": False, "axes.axisbelow": True,
        "lines.linewidth": 2, "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
        "legend.frameon": False, "legend.fontsize": 8, "text.color": INK,
    })


def _db_axis(ax, sizes) -> None:
    ax.set_xscale("log", base=2)
    ax.set_xticks(sorted(sizes), [fmt_size(int(s)) for s in sorted(sizes)])
    ax.minorticks_off()


def _empty(ax, text: str = "no data") -> None:
    ax.text(0.5, 0.5, text, ha="center", va="center", color=MUTED, transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


def _arm_line(ax, arm: str, x, y, **kw) -> None:
    ax.plot(x, y, color=COLOR[arm], marker="o", markersize=5, markeredgecolor=SURFACE, markeredgewidth=1.5,
            linestyle="--" if arm in ONE_BIT else "-", label=arm + (" (1 bit)" if arm in ONE_BIT else ""), **kw)


def f1_reproduction(t: Tables) -> plt.Figure:
    rows = t.t3[(t.t3.unit == "x") & t.t3.ours.notna() & t.t3.check.isin(["ratio", "max_ratio"])
                & ((t.t3.check == "ratio") | t.t3.cell.str.startswith("max"))].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(7.5, 1.2 + 0.28 * max(len(rows), 1)))
    if rows.empty:
        _empty(ax)
        return fig
    y = np.arange(len(rows))[::-1]
    shown = pd.concat([rows.ours, rows.ci_lo, rows.target_lo, rows.target_hi]).dropna()
    floor, ceil = shown.min() / 1.5, shown.max() * 1.5
    ours_color = SLOTS[0]
    for yi, r in zip(y, rows.itertuples()):
        lo = r.target_lo if pd.notna(r.target_lo) else floor  # a published upper bound only
        hi = r.target_hi if pd.notna(r.target_hi) else ceil
        if lo == hi:
            ax.plot([lo], [yi], marker="|", markersize=12, color=INK_2, markeredgewidth=2)
        else:
            ax.plot([lo, hi], [yi, yi], color=AXIS, linewidth=6, solid_capstyle="butt")
        if pd.notna(r.ci_lo):
            ax.plot([r.ci_lo, r.ci_hi], [yi, yi], color=ours_color, linewidth=1.2)
        ax.plot([r.ours], [yi], marker="o", markersize=6, color=ours_color, markeredgecolor=SURFACE,
                markeredgewidth=1.5, linestyle="none")
    ax.set_yticks(y, [f"{r.id}  {r.cell}" for r in rows.itertuples()], fontsize=7)
    ax.set_xscale("log")
    ax.set_xlim(floor, ceil)
    ax.set_xlabel("ratio, log scale")
    ax.grid(axis="y", visible=False)
    ax.plot([], [], color=AXIS, linewidth=6, label="published range")
    ax.plot([], [], marker="|", color=INK_2, linestyle="none", markersize=10, label="published value")
    ax.plot([], [], marker="o", color=ours_color, label="ours, median with 95 % CI")
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3)
    ax.set_title("F1  Reproduction: published against measured ratios (configs/reproduction_targets.yaml)",
                 pad=24, loc="left")
    return fig


F2_METRICS = {"online_bytes": "online bytes", "server_online_s": "server time",
              "client_online_s": "client time incl. verify", "client_storage_bytes": "client storage"}


def f2_heatmaps(t: Tables) -> plt.Figure:
    ov = t.overheads
    swept = t.frame[t.frame.record_bytes > 0]
    dbs, recs = sorted(set(swept.db_bytes)), sorted(set(swept.record_bytes))
    fig, axes = plt.subplots(len(PRICE_PAIRS), len(F2_METRICS), figsize=(11, 2.6 * len(PRICE_PAIRS)),
                             squeeze=False)
    sel = ov[ov.metric.isin(list(F2_METRICS)) & ov.pair.isin([p.label for p in PRICE_PAIRS])]
    logs = np.log2(sel.ratio.replace(0, np.nan)).abs().dropna()
    vmax = max(float(logs.max()) if len(logs) else 1.0, 0.25)
    for i, pair in enumerate(PRICE_PAIRS):
        for j, (metric, title) in enumerate(F2_METRICS.items()):
            ax = axes[i][j]
            ax.grid(False)
            grid_vals = np.full((len(dbs), len(recs)), np.nan)
            padded = np.zeros_like(grid_vals, dtype=bool)
            cells = sel[(sel.pair == pair.label) & (sel.metric == metric)]
            for r in cells.itertuples():
                if r.db_bytes in dbs and r.record_bytes in recs:
                    grid_vals[dbs.index(r.db_bytes), recs.index(r.record_bytes)] = r.ratio
                    padded[dbs.index(r.db_bytes), recs.index(r.record_bytes)] = r.padded
            ax.imshow(np.ones_like(grid_vals), cmap=LinearSegmentedColormap.from_list("m", [MISSING_FILL] * 2),
                      aspect="auto")
            ax.imshow(np.log2(grid_vals), cmap=DIVERGING, vmin=-vmax, vmax=vmax, aspect="auto")
            for (yi, xi), val in np.ndenumerate(grid_vals):
                if np.isnan(val):
                    ax.text(xi, yi, "n/a", ha="center", va="center", fontsize=7, color=MUTED)
                    continue
                strong = abs(math.log2(val)) > 0.6 * vmax
                ax.text(xi, yi, f"{val:.2f}×" + ("*" if padded[yi, xi] else ""), ha="center", va="center",
                        fontsize=7, color="#ffffff" if strong else INK)
            ax.set_xticks(range(len(recs)), [fmt_size(r) for r in recs])
            ax.set_yticks(range(len(dbs)), [fmt_size(d) for d in dbs] if j == 0 else [])
            for side in ("left", "bottom"):
                ax.spines[side].set_visible(False)
            ax.tick_params(length=0)
            if i == 0:
                ax.set_title(title)
            if j == 0:
                ax.set_ylabel(f"{pair.label}\ndatabase size")
            if i == len(PRICE_PAIRS) - 1:
                ax.set_xlabel("record size")
    sm = plt.cm.ScalarMappable(cmap=DIVERGING, norm=plt.Normalize(-vmax, vmax))
    cb = fig.colorbar(sm, ax=axes, shrink=0.6, pad=0.02)
    ticks = [v for v in (-3, -2, -1, 0, 1, 2, 3) if abs(v) <= vmax] or [0]
    cb.set_ticks(ticks, labels=[f"{2.0 ** v:g}×" for v in ticks])
    cb.set_label("overhead, median ratio (log scale)")
    cb.outline.set_visible(False)
    fig.suptitle("F2  Overhead of verifiability per cell (* padding factor above 1.1, decision 19)",
                 x=0.02, ha="left", fontsize=11)
    return fig


F3_METRICS = {"server_online_s": "server answer time (s)", "online_bytes": "online bytes",
              "client_storage_bytes": "client storage (bytes)"}


def f3_absolute(t: Tables) -> plt.Figure:
    fig, axes = plt.subplots(1, len(F3_METRICS), figsize=(11, 3.6))
    med = t.medians
    shown = med[(med.record_bytes == FOCUS_RECORD) | med.arm.isin(ONE_BIT)]
    for ax, (metric, label) in zip(axes, F3_METRICS.items()):
        if metric not in shown or shown[metric].isna().all():
            _empty(ax)
            continue
        for arm in grid.ARMS:
            a = shown[shown.arm == arm].dropna(subset=[metric]).sort_values("db_bytes")
            if not a.empty:
                _arm_line(ax, arm, a.db_bytes, a[metric])
        _db_axis(ax, shown.db_bytes.unique())
        ax.set_yscale("log")
        ax.set_title(label, loc="left")
        ax.set_xlabel("database size")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=8, bbox_to_anchor=(0.5, 0.93))
    fig.suptitle("F3  Absolute cost against database size, 1 KiB records (S3 and S4 retrieve one bit)",
                 x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    return fig


F4_KINDS = {"bytes": "total bytes per query", "client_s": "client time per query (s)"}


def f4_amortised(t: Tables) -> plt.Figure:
    fig, axes = plt.subplots(1, len(F4_KINDS), figsize=(10, 3.8))
    db = focus_db(t.medians)
    if db is None:
        for ax in axes:
            _empty(ax)
        return fig
    cells = {a: _cell(t.medians, a, db, FOCUS_RECORD) for a in VERIFIABLE_1KIB}
    qs = np.logspace(0, math.log10(max(amortise.QS)), 200)
    for ax, (kind, label) in zip(axes, F4_KINDS.items()):
        fn = amortise.AMORTISED[kind]
        for arm, c in cells.items():
            ax.plot(qs, [fn(c, q) for q in qs], color=COLOR[arm], label=arm)
            ax.plot(amortise.QS, [fn(c, q) for q in amortise.QS], color=COLOR[arm], marker="o", markersize=5,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, linestyle="none")
        marked = 0
        for a, b in (("S1", "M1"), ("S2", "M1"), ("M1", "S1"), ("M1", "S2")):
            q = amortise.break_even_Q(cells[a], cells[b], kind)
            if q is not None and 1 < q <= max(amortise.QS):
                y = fn(cells[a], q)
                ax.plot([q], [y], marker="D", markersize=6, color=INK, linestyle="none")
                ax.annotate(f"{a} ≤ {b} from Q = {q:,}", (q, y), textcoords="offset points", xytext=(6, 6),
                            fontsize=7, color=INK_2)
                marked += 1
        if not marked:
            ax.text(0.02, 0.02, f"no break-even between S1, S2 and M1 for Q ≤ {max(amortise.QS):,}",
                    transform=ax.transAxes, fontsize=7, color=INK_2)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(amortise.QS, [f"{q:,}" for q in amortise.QS])
        ax.minorticks_off()
        ax.set_xlabel("queries per offline phase, Q")
        ax.set_title(label, loc="left")
    axes[0].legend(loc="upper right")
    fig.suptitle(f"F4  Amortised cost per query at {fmt_size(db)} x 1 KiB (two-server arms assume "
                 "non-colluding servers)", x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


def f5_network(t: Tables) -> plt.Figure:
    profiles = list(amortise.load_profiles())
    fig, axes = plt.subplots(1, len(profiles), figsize=(11, 3.6))
    arms = [a for a in grid.ARMS if a not in ONE_BIT]
    db = focus_db(t.medians, arms=[a for a in arms if a in set(t.medians.arm)])
    lat = t.latency
    for ax, profile in zip(axes, profiles):
        sel = lat[(lat.profile == profile) & (lat.db_bytes == db) & (lat.record_bytes == FOCUS_RECORD)]
        sel = sel.set_index("arm").reindex([a for a in arms if a in set(sel.arm)])
        if db is None or sel.empty:
            _empty(ax)
            continue
        x = np.arange(len(sel))
        ax.bar(x, sel.p50, width=0.6, color=[COLOR[a] for a in sel.index], edgecolor=SURFACE, linewidth=2)
        ax.errorbar(x, sel.p50, yerr=[np.zeros(len(sel)), (sel.p95 - sel.p50).clip(lower=0)], fmt="none",
                    ecolor=INK_2, elinewidth=1, capsize=3)
        ax.set_xticks(x, sel.index)
        ax.grid(axis="x", visible=False)
        ax.set_title(profile, loc="left")
        ax.set_ylabel("modelled latency (s), p50 with p95 whisker")
    title_db = fmt_size(db) if db else "n/a"
    fig.suptitle(f"F5  Modelled end-to-end latency at {title_db} x 1 KiB per network profile",
                 x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


F6_Q = 100  # spec 1's headline horizon: offline parts are spread over 100 queries
F6_PANELS = {"client and server seconds per query": ["offline proof (server)", "offline verify (client)",
                                                     "online verify (client)"],
             "extra bytes per query over the baseline": ["extra offline bytes", "extra online bytes"]}
F6_OFFLINE = {"offline proof (server)", "offline verify (client)", "extra offline bytes"}
F6_FILLS = ("#104281", "#3987e5", "#9ec5f4")
F6_UNITS = {"client and server seconds per query": "s", "extra bytes per query over the baseline": "B"}


def f6_breakdown(t: Tables) -> plt.Figure:
    fig, axes = plt.subplots(1, len(F6_PANELS), figsize=(11, 3.2))
    db = focus_db(t.medians)
    sel = t.breakdown
    sel = sel[(sel.db_bytes == db) & (sel.record_bytes == FOCUS_RECORD)].set_index("arm") if len(sel) else sel
    for ax, (label, parts) in zip(axes, F6_PANELS.items()):
        if db is None or sel.empty:
            _empty(ax)
            continue
        arms = [a for a in VERIFIABLE_1KIB if a in sel.index]
        y = np.arange(len(arms))[::-1]
        left = np.zeros(len(arms))
        for part, fill in zip(parts, F6_FILLS):
            width = sel.loc[arms, part].astype(float).clip(lower=0).fillna(0).to_numpy()
            if part in F6_OFFLINE:
                width = width / F6_Q
            ax.barh(y, width, left=left, height=0.5, color=fill, edgecolor=SURFACE, linewidth=2,
                    label=part + (f" / {F6_Q}" if part in F6_OFFLINE else ""))
            left += width
        fmt = EngFormatter(unit=F6_UNITS[label], places=1)
        for yi, total in zip(y, left):
            ax.text(total, yi, "  " + fmt(total), va="center", fontsize=7, color=INK_2)
        ax.set_yticks(y, [a + (" (verify fused)" if sel.loc[a, "verify_fused"] else "") for a in arms])
        ax.xaxis.set_major_formatter(EngFormatter(unit=F6_UNITS[label]))
        ax.grid(axis="y", visible=False)
        ax.set_xlabel(label)
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=len(parts))
    fig.suptitle(f"F6  Where verification cost goes at {fmt_size(db) if db else 'n/a'} x 1 KiB, "
                 f"offline parts spread over Q = {F6_Q} queries", x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    return fig


FIGURE_BUILDERS: dict[str, Callable[[Tables], plt.Figure]] = {
    "F1_reproduction": f1_reproduction, "F2_overhead_heatmaps": f2_heatmaps, "F3_absolute_cost": f3_absolute,
    "F4_amortised": f4_amortised, "F5_network": f5_network, "F6_breakdown": f6_breakdown,
}


def _stamp(fig: plt.Figure) -> None:
    fig.text(0.5, 0.5, SYNTHETIC_MARK, ha="center", va="center", rotation=20, fontsize=40, color="#d03b3b",
             alpha=0.25, zorder=100)
    fig.text(0.99, 0.01, SYNTHETIC_MARK, ha="right", va="bottom", fontsize=9, color="#d03b3b", weight="bold")


def t1_machine(sysinfo: Path) -> pd.DataFrame:
    if not sysinfo.exists():
        return pd.DataFrame([{"key": "env/sysinfo.json", "value": "missing; run make sysinfo on the reference Mac"}])
    flat = pd.json_normalize(json.loads(sysinfo.read_text()), sep=".").iloc[0]
    return pd.DataFrame({"key": flat.index, "value": [str(v) for v in flat.values]})


def t2_params(t: Tables) -> pd.DataFrame:
    rows = []
    for (arm, params), g in t.frame.groupby(["arm", "scheme_params"], sort=False):
        parsed = json.loads(params)
        cells = sorted({f"{fmt_size(int(d))}x{fmt_size(int(r)) if r else '1bit'}"
                        for d, r in zip(g.db_bytes, g.record_bytes)})
        rows.append({"arm": arm, "adapter": g.adapter.iloc[0], "cells": ", ".join(cells),
                     "scheme_params": params,
                     "security_level": parsed.get("security_level", "TODO[HUMAN]: from the papers")})
    order = {a: i for i, a in enumerate(grid.ARMS)}
    return pd.DataFrame(rows).sort_values("arm", key=lambda s: s.map(order), kind="stable")


def t4_overheads(t: Tables) -> pd.DataFrame:
    ov = t.overheads.copy()
    ov["db"] = ov.db_bytes.map(fmt_size)
    ov["record"] = ov.record_bytes.map(lambda r: fmt_size(int(r)) if r else "1bit")
    ov["ci95"] = [f"[{lo:.3g}, {hi:.3g}]" if pd.notna(lo) else "" for lo, hi in zip(ov.ci_lo, ov.ci_hi)]
    return ov[["pair", "role", "db", "record", "phase", "metric", "ratio", "ci95", "n_pairs", "padded"]]


def _markdown(df: pd.DataFrame, title: str, synthetic_run: bool) -> str:
    def cell(v) -> str:
        if isinstance(v, float):
            return "" if math.isnan(v) else f"{v:.4g}"
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = [f"# {title}", ""]
    if synthetic_run:
        lines += [f"> **{SYNTHETIC_MARK}.** Rows from analysis/synthetic.py, an invented cost model.", ""]
    lines += ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines) + "\n"


TABLE_TITLES = {"T1_machine": "T1 Machine, toolchain and pinned SHAs", "T2_scheme_params": "T2 Scheme parameters",
                "T3_reproduction": "T3 Reproduction: ours against published",
                "T4_overheads": "T4 Overhead summary with 95 % CIs"}


def render(t: Tables, out: Path, *, synthetic_run: bool, sysinfo: Path = SYSINFO) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    _style()
    written = []
    for name, build_fig in FIGURE_BUILDERS.items():
        fig = build_fig(t)
        if synthetic_run:
            _stamp(fig)
        for ext in ("pdf", "png"):
            path = out / f"{name}.{ext}"
            fig.savefig(path, dpi=200, bbox_inches="tight")
            written.append(path)
        plt.close(fig)
    tables = {"T1_machine": t1_machine(sysinfo), "T2_scheme_params": t2_params(t),
              "T3_reproduction": t.t3.drop(columns=["check", "target_lo", "target_hi"]),
              "T4_overheads": t4_overheads(t)}
    for name, table in tables.items():
        csv = table.assign(note=SYNTHETIC_MARK) if synthetic_run else table
        csv.to_csv(out / f"{name}.csv", index=False)
        (out / f"{name}.md").write_text(_markdown(table, TABLE_TITLES[name], synthetic_run), encoding="utf-8")
        written += [out / f"{name}.csv", out / f"{name}.md"]
    return written


def main(argv: list[str] | None = None) -> list[Path]:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--runs", type=Path, nargs="+", help="results/raw/<run_id> directories")
    src.add_argument("--synthetic", metavar="TIER", help="build from synthetic rows for this grid tier")
    p.add_argument("--out", type=Path, default=FIGURES)
    p.add_argument("--processed", type=Path, help="parquet output (default results/processed, or "
                                                  "<out>/processed with --synthetic)")
    p.add_argument("--reference-platform", type=Path, default=aggregate.REFERENCE_PLATFORM)
    p.add_argument("--sysinfo", type=Path, default=SYSINFO)
    args = p.parse_args(argv)
    if args.synthetic:
        df = frame.rows_to_frame(synthetic.rows_for_cells(grid.expand(grid.load(), args.synthetic)))
        status = pd.DataFrame()
        processed_dir = args.processed or args.out / "processed"
    else:
        if not args.reference_platform.exists():
            p.error(f"{args.reference_platform} is missing; run make sysinfo on the reference Mac")
        df, status = frame.load_runs(args.runs, args.reference_platform.read_text().strip())
        processed_dir = args.processed or aggregate.PROCESSED
    if df.empty:
        p.error("no usable rows")
    t = build(df, status)
    aggregate.write_tables(processed(t), processed_dir)
    written = render(t, args.out, synthetic_run=bool(args.synthetic), sysinfo=args.sysinfo)
    for path in written:
        print(path)
    return written


if __name__ == "__main__":
    main()
