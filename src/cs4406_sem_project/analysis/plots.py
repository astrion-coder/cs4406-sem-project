"""Figures F1-F6, F8-F13 and tables T1-T4 (spec 12) from raw result rows, in one command.

    uv run python -m cs4406_sem_project.analysis.plots --runs results/raw/<run_id> ... [--out results/figures]
        [--session results/raw/<run_id>/session] [--netem results/raw/<run_id>/netem]
        [--loadgen results/raw/<run_id>/loadgen] [--recovery results/raw/<run_id> ...]
    uv run python -m cs4406_sem_project.analysis.plots --synthetic tier1 --out <dir>

F8 needs --session (session_check.py), F9 needs --netem (netem.py), F10 and F11 need --loadgen
(harness/loadgen.py) and F12 and F13 need --recovery (harness/recovery.py, drawn by analysis/rq4.py); without its
input a figure is skipped with a note. --synthetic builds every output
from analysis/synthetic.py rows, and stamps every figure and table "SYNTHETIC, not a result". Processed tables go to --processed as parquet.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.ticker import EngFormatter, FuncFormatter  # noqa: E402

from cs4406_sem_project.analysis import aggregate, amortise, extrapolate, frame, reproduce, rq4, synthetic  # noqa: E402
from cs4406_sem_project.harness import grid, loadgen, netem, session_check  # noqa: E402
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
    extrapolated: pd.DataFrame  # S4 at sizes it does not run (decision 10), from extrapolate.py
    fits: pd.DataFrame
    session: pd.DataFrame | None = None  # F8, from session_totals()
    emulation: pd.DataFrame | None = None  # F9, from emulation()
    load_curves: pd.DataFrame | None = None  # F10 and F11, from load_curves()


def build(df: pd.DataFrame, status: pd.DataFrame, session: pd.DataFrame | None = None,
          netem_rows: pd.DataFrame | None = None, loadgen_rows: pd.DataFrame | None = None) -> Tables:
    """`session` is session_check's comparison table, `netem_rows` the client rows with a profile column
    and `loadgen_rows` frame.load_loadgen's table; each is None when that experiment has not run."""
    summary = aggregate.summarise(df)
    med = aggregate.medians(summary)
    latency = amortise.latency_table(df[df.phase == "online"], amortise.load_profiles())
    ext = extrapolate.extrapolate(med)
    return Tables(
        frame=df, status=status, summary=summary, medians=med, overheads=aggregate.overheads(df),
        amortised=amortise.amortised_table(med), latency=latency,
        t3=reproduce.compare(df, reproduce.load_targets(), extrapolated=ext), breakdown=breakdown(med),
        extrapolated=ext, fits=extrapolate.fits(med),
        session=None if session is None else session_totals(med, session),
        emulation=None if netem_rows is None or netem_rows.empty else emulation(netem_rows, latency),
        load_curves=None if loadgen_rows is None or loadgen_rows.empty else load_curves(loadgen_rows))


def processed(t: Tables) -> dict[str, pd.DataFrame]:
    out = {"frame": t.frame, "status": t.status, "summary": t.summary, "overheads": t.overheads,
           "amortised": t.amortised, "latency": t.latency, "reproduction": t.t3, "breakdown": t.breakdown,
           "s4_extrapolated": t.extrapolated, "s4_fits": t.fits}
    if t.session is not None:
        out["session_totals"] = t.session
    if t.emulation is not None:
        out |= {"emulation": t.emulation, "emulation_overheads": emulation_overheads(t.emulation)}
    if t.load_curves is not None:
        out |= {"load_curves": t.load_curves, "saturation": saturation(t.load_curves),
                "core_scaling": core_scaling(t.load_curves)}
    return out


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


F8_QS = (1, 10, 100, 1000)  # spec 12, F8
F8_KINDS = {"total_s": "total client and server time (s)", "bytes": "total bytes"}


def session_totals(med: pd.DataFrame, session: pd.DataFrame) -> pd.DataFrame:
    """F8's table: per session cell, Q and quantity, the model's total Q x amortised(Q) from the grid
    medians, beside the session's measured total wherever it ran Q queries."""
    cells = session[frame.KEY].drop_duplicates().merge(med, on=frame.KEY, how="left")
    computed = pd.concat([cells[frame.KEY].assign(q=q, quantity=kind, computed=q * amortise.AMORTISED[kind](cells, q))
                          for kind in F8_KINDS for q in F8_QS], ignore_index=True)
    measured = session[[*frame.KEY, "q", "quantity", "measured", "rel_diff", "flagged"]]
    out = computed.merge(measured, on=[*frame.KEY, "q", "quantity"], how="left")
    out["flagged"] = out.flagged.fillna(False).astype(bool)
    return out


def emulation(client: pd.DataFrame, latency: pd.DataFrame) -> pd.DataFrame:
    """F9's table: per profile and cell, the emulated end-to-end latency (query generation to verified
    record; the round trip covers sending, the answer and receiving) beside the modelled one."""
    keys = ["profile", *frame.KEY]
    lat = client.assign(emulated_s=client.query_gen_s + client.rtt_s_max + client.client_recover_s
                        + client.client_verify_s.fillna(0.0))
    g = lat.groupby(keys)
    out = pd.DataFrame({"emulated_p50": g.emulated_s.median(), "emulated_p95": g.emulated_s.quantile(0.95),
                        "n": g.size(), "online_bytes": g.online_bytes.median()}).reset_index()
    modelled = latency.rename(columns={"p50": "modelled_p50", "p95": "modelled_p95"})
    out = out.merge(modelled[[*keys, "modelled_p50", "modelled_p95"]], on=keys, how="left")
    profiles = amortise.load_profiles()
    out["modelled_rtt_ms"] = out.profile.map({p: v.rtt_ms for p, v in profiles.items()})
    if "rtt_checked" in client:
        # Decision 22: a link whose RTT dnctl could not set (LAN) is modelled at its calibrated RTT. RTT is one
        # additive term of latency_e2e, so the model moves by the difference times the round count.
        links = client.groupby("profile")[["rtt_checked", "calib_rtt_ms"]].first()
        measured = links[~links.rtt_checked & links.calib_rtt_ms.notna()].calib_rtt_ms
        new_rtt = out.profile.map(measured)
        shift = ((new_rtt - out.modelled_rtt_ms) / 1e3 * amortise.ROUNDS).fillna(0.0)
        out[["modelled_p50", "modelled_p95"]] = out[["modelled_p50", "modelled_p95"]].add(shift, axis=0)
        out["modelled_rtt_ms"] = new_rtt.fillna(out.modelled_rtt_ms)
    out["emulated_over_modelled"] = out.emulated_p50 / out.modelled_p50
    return out


def emulation_overheads(em: pd.DataFrame) -> pd.DataFrame:
    """Per price pair, profile and cell: the verifiable arm's emulated latency and online bytes over its
    baseline's."""
    keys = ["profile", "db_bytes", "record_bytes"]
    parts = []
    for p in PRICE_PAIRS:
        m = em[em.arm == p.num].merge(em[em.arm == p.den], on=keys, suffixes=("_v", "_b"))
        parts.append(m[keys].assign(pair=p.label, arm=p.num, byte_overhead=m.online_bytes_v / m.online_bytes_b,
                                    latency_overhead=m.emulated_p50_v / m.emulated_p50_b))
    return pd.concat(parts, ignore_index=True)


LOAD_KEYS = ["arm", "db_bytes", "record_bytes", "cell_id", "experiment", "point", "workers"]


def load_curves(lg: pd.DataFrame) -> pd.DataFrame:
    """F10's and F11's table: per cell and sweep point, the median over runs of throughput and latency."""
    g = lg.groupby(LOAD_KEYS)
    out = pd.DataFrame({
        "clients": g.clients.first(), "offered_load_qps": g.offered_load_qps.median(),
        "throughput_qps": g.throughput_qps.median(), "lat_p50_s": g.lat_p50_s.median(),
        "lat_p95_s": g.lat_p95_s.median(), "server_cpu_util": g.server_cpu_util.median(),
        "saturated_runs": g.status.agg(lambda s: int((s == "saturated").sum())), "n": g.size()}).reset_index()
    return out.sort_values(["cell_id", "experiment", "throughput_qps"]).reset_index(drop=True)


def saturation(curves: pd.DataFrame) -> pd.DataFrame:
    """Per cell, the closed-loop point with the highest median throughput: harness/loadgen.py's saturation
    throughput, which also sets the open loop's offered loads."""
    closed = curves[curves.experiment == "closed"]
    return closed.loc[closed.groupby("cell_id").throughput_qps.idxmax()].reset_index(drop=True)


def core_scaling(curves: pd.DataFrame) -> pd.DataFrame:
    """overhead_vs_cores(W) (spec 6): per price pair and cell, baseline over verifiable throughput at each W."""
    mc = curves[curves.experiment == "multicore"]
    keys = ["db_bytes", "record_bytes", "workers"]
    parts = [mc[mc.arm == p.num].merge(mc[mc.arm == p.den], on=keys, suffixes=("_v", "_b"))
             .assign(pair=p.label, arm=p.num) for p in PRICE_PAIRS]
    out = pd.concat(parts, ignore_index=True)
    out["overhead"] = out.throughput_qps_b / out.throughput_qps_v
    return out[["pair", "arm", *keys, "throughput_qps_v", "throughput_qps_b", "overhead"]]


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
    ax.set_xticks(sorted(sizes), [fmt_size(int(s)) for s in sorted(sizes)], rotation=40, ha="right")
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
        sizes = set(shown.db_bytes)
        ext = t.extrapolated
        if not ext.empty and metric in ext:
            last = shown[(shown.arm == extrapolate.ARM) & shown.db_bytes.isin(extrapolate.FIT_SIZES)]
            last = last.dropna(subset=[metric]).sort_values("db_bytes").tail(1)
            x = list(last.db_bytes) + list(ext.db_bytes)
            y = list(last[metric]) + list(ext[metric])
            ax.plot(x, y, color=COLOR[extrapolate.ARM], linestyle=":", marker="o", markersize=5,
                    markerfacecolor=SURFACE, label=f"{extrapolate.ARM} extrapolated (decision 10)")
            sizes |= set(ext.db_bytes)
        _db_axis(ax, sorted(sizes))
        ax.set_yscale("log")
        ax.set_title(label, loc="left")
        ax.set_xlabel("database size")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=5, bbox_to_anchor=(0.5, 0.95))
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
        _mark_break_even(ax, cells, kind, fn, max(amortise.QS))
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


def _mark_break_even(ax, cells: dict, kind: str, y_of: Callable, q_max: int) -> None:
    """Mark where a single-server verifiable arm's offline phase starts to pay off against M1, or back."""
    marked = 0
    for a, b in (("S1", "M1"), ("S2", "M1"), ("M1", "S1"), ("M1", "S2")):
        if cells.get(a) is None or cells.get(b) is None:
            continue
        q = amortise.break_even_Q(cells[a], cells[b], kind)
        if q is not None and 1 < q <= q_max:
            y = y_of(cells[a], q)
            ax.plot([q], [y], marker="D", markersize=6, color=INK, linestyle="none")
            ax.annotate(f"{a} ≤ {b} from Q = {q:,}", (q, y), textcoords="offset points",
                        xytext=(6, 6 - 14 * marked), fontsize=7, color=INK_2)
            marked += 1
    if not marked:
        ax.text(0.02, 0.02, f"no break-even between S1, S2 and M1 for Q ≤ {q_max:,}",
                transform=ax.transAxes, fontsize=7, color=INK_2)


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


class Skip(Exception):
    """The figure's input was not given or holds nothing to draw; the message says what is missing."""


def f8_session(t: Tables) -> plt.Figure:
    if t.session is None:
        raise Skip("no session comparison; pass --session results/raw/<run_id>/session after session_check.py")
    s = t.session
    have = s[s.measured.notna() & s.computed.notna()]
    db = have[have.record_bytes == FOCUS_RECORD].db_bytes.max()
    if pd.isna(db):
        raise Skip("no 1 KiB session cell has both a measured and a computed total")
    db = int(db)
    sel = s[(s.db_bytes == db) & ((s.record_bytes == FOCUS_RECORD) | s.arm.isin(ONE_BIT))]
    arms = [a for a in grid.ARMS if a in set(sel.dropna(subset=["computed"]).arm)]
    cells = {a: _cell(t.medians, a, db, 0 if a in ONE_BIT else FOCUS_RECORD) for a in arms}
    qs = np.logspace(0, math.log10(max(F8_QS)), 200)
    fig, axes = plt.subplots(1, len(F8_KINDS), figsize=(10, 3.8))
    for ax, (kind, label) in zip(axes, F8_KINDS.items()):
        fn = amortise.AMORTISED[kind]

        def total(c, q, fn=fn):
            return q * fn(c, q)

        for arm in arms:
            ax.plot(qs, [total(cells[arm], q) for q in qs], color=COLOR[arm],
                    linestyle="--" if arm in ONE_BIT else "-", label=arm + (" (1 bit)" if arm in ONE_BIT else ""))
            m = sel[(sel.arm == arm) & (sel.quantity == kind)].dropna(subset=["measured"])
            ax.plot(m.q, m.measured, color=COLOR[arm], marker="o", markersize=7, markeredgecolor=SURFACE,
                    markeredgewidth=1.5, linestyle="none")
            off = m[m.flagged]
            ax.plot(off.q, off.measured, marker="o", markersize=12, markerfacecolor="none", markeredgecolor=INK,
                    markeredgewidth=1.2, linestyle="none")
        _mark_break_even(ax, cells, kind, total, max(F8_QS))
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(F8_QS, [f"{q:,}" for q in F8_QS])
        ax.minorticks_off()
        ax.set_xlabel("queries after one offline phase, Q")
        ax.set_title(label, loc="left")
    handles, _ = axes[0].get_legend_handles_labels()
    fig.legend(handles=handles, loc="upper center", ncol=len(handles), bbox_to_anchor=(0.5, 0.95))
    keys = [plt.Line2D([], [], color=INK_2, label="computed: offline + Q x online, grid medians"),
            plt.Line2D([], [], color=INK_2, marker="o", linestyle="none", label="measured: one session"),
            plt.Line2D([], [], marker="o", markersize=10, markerfacecolor="none", markeredgecolor=INK,
                       linestyle="none", label="over 5 % apart at Q = 100 (spec 5)")]
    fig.legend(handles=keys, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 0.89))
    fig.suptitle(f"F8  Is setup worth it? Total cost after Q queries at {fmt_size(db)} x 1 KiB",
                 x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.8))
    return fig


F9_MARKERS = {"lan": "o", "broadband": "s", "mobile": "^", netem.NONE: "D"}


def _diagonal(ax, values, pad: float = 1.5, scale: str = "log") -> None:
    lo, hi = float(np.nanmin(values)) / pad, float(np.nanmax(values)) * pad
    ax.plot([lo, hi], [lo, hi], color=AXIS, linewidth=1, linestyle="--")
    ax.set_xscale(scale)
    ax.set_yscale(scale)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_aspect("equal", adjustable="box")


def f9_network(t: Tables) -> plt.Figure:
    if t.emulation is None:
        raise Skip("no emulated-link rows; pass --netem results/raw/<run_id>/netem after netem.py")
    em = t.emulation
    profiles = list(amortise.load_profiles())
    fig, axes = plt.subplots(1, len(profiles) + 1, figsize=(13, 3.9))
    for ax, profile in zip(axes, profiles):
        sel = em[em.profile == profile].dropna(subset=["emulated_p50", "modelled_p50"])
        if sel.empty:
            _empty(ax, f"no {profile} rows")
            continue
        _diagonal(ax, pd.concat([sel.emulated_p50, sel.modelled_p50]))
        for arm in [a for a in grid.ARMS if a in set(sel.arm)]:
            a = sel[sel.arm == arm]
            ax.plot(a.modelled_p50, a.emulated_p50, color=COLOR[arm], marker="o", markersize=7,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, linestyle="none", label=arm)
        ax.set_title(f"{profile}: emulated against modelled", loc="left")
        ax.set_xlabel("modelled p50 latency (s)")
        ax.set_ylabel("emulated p50 latency (s)")
    handles, labels = [], []
    for ax in axes[:-1]:
        for h, lab in zip(*ax.get_legend_handles_labels()):
            if lab not in labels:
                handles.append(h)
                labels.append(lab)
    ax = axes[-1]
    ov = emulation_overheads(em).dropna(subset=["byte_overhead", "latency_overhead"])
    if ov.empty:
        _empty(ax, "no verifiable and baseline pair")
    else:
        _diagonal(ax, pd.concat([ov.byte_overhead, ov.latency_overhead]), pad=1.1, scale="linear")
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:g}×"))
        for r in ov.itertuples():
            ax.plot([r.byte_overhead], [r.latency_overhead], color=COLOR[r.arm],
                    marker=F9_MARKERS.get(r.profile, "P"), markersize=7, markeredgecolor=SURFACE,
                    markeredgewidth=1.5, linestyle="none")
        shapes = [plt.Line2D([], [], color=INK_2, marker=F9_MARKERS.get(p, "P"), linestyle="none", label=p)
                  for p in dict.fromkeys(ov.profile)]
        ax.legend(handles=shapes, loc="lower right", title="profile", title_fontsize=7)
        ax.set_title("latency overhead against byte overhead", loc="left")
        ax.set_xlabel("online bytes, verifiable / baseline")
        ax.set_ylabel("emulated p50 latency, verifiable / baseline")
    handles.append(plt.Line2D([], [], color=AXIS, linewidth=1, linestyle="--", label="y = x"))
    fig.legend(handles=handles, loc="upper center", ncol=len(handles), bbox_to_anchor=(0.5, 0.92))
    fig.suptitle("F9  Emulated (dummynet on lo0) against modelled end-to-end latency per profile, focus cells",
                 x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    return fig


def _focus_load(t: Tables) -> tuple[pd.DataFrame, int]:
    """The load curves of the 1 KiB cells (one-bit arms: their only cell) at the largest database."""
    if t.load_curves is None:
        raise Skip("no load-generator rows; pass --loadgen results/raw/<run_id>/loadgen after harness/loadgen.py")
    c = t.load_curves
    c = c[(c.record_bytes == FOCUS_RECORD) | c.arm.isin(ONE_BIT)]
    if c.empty:
        raise Skip("no 1 KiB or one-bit load-generator cell")
    db = int(c.db_bytes.max())
    return c[c.db_bytes == db], db


F10_LOOPS = {"closed": "closed loop, C clients", "open": "open loop, Poisson rate λ"}
F10_LATENCY = {"lat_p50_s": "p50", "lat_p95_s": "p95"}
F10_SATURATION_POINT = "open_w{w}_f1.00"


def f10_load(t: Tables) -> plt.Figure:
    curves, db = _focus_load(t)
    sat = saturation(curves).set_index("arm")
    fig, axes = plt.subplots(len(F10_LATENCY), len(F10_LOOPS), figsize=(11, 6.6), sharex=True, squeeze=False)
    for j, (loop, loop_label) in enumerate(F10_LOOPS.items()):
        for i, (col, pct) in enumerate(F10_LATENCY.items()):
            ax = axes[i][j]
            sel = curves[curves.experiment == loop]
            if sel.empty:
                _empty(ax, f"no {loop}-loop rows")
                continue
            for arm in [a for a in grid.ARMS if a in set(sel.arm)]:
                a = sel[sel.arm == arm].sort_values("clients" if loop == "closed" else "offered_load_qps")
                _arm_line(ax, arm, a.throughput_qps, a[col])
                if arm not in sat.index:
                    continue
                at = (a[a.clients == sat.loc[arm, "clients"]] if loop == "closed"
                      else a[a.point == F10_SATURATION_POINT.format(w=int(a.workers.iloc[0]))])
                ax.plot(at.throughput_qps, at[col], marker="D", markersize=8, color=COLOR[arm], markeredgecolor=INK,
                        markeredgewidth=1.2, linestyle="none", zorder=5)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_ylabel(f"{pct} latency (s)")
            if i == 0:
                ax.set_title(loop_label, loc="left")
            if i == len(F10_LATENCY) - 1:
                ax.set_xlabel("achieved throughput (queries/s)")
    handles, labels = [], []
    for ax in axes.flat:
        for h, lab in zip(*ax.get_legend_handles_labels()):
            if lab not in labels:
                handles.append(h)
                labels.append(lab)
    handles.append(plt.Line2D(
        [], [], marker="D", markersize=7, color=INK_2, markeredgecolor=INK, linestyle="none",
        label="saturation: closed loop's highest median throughput; open loop at λ = 100 % of it"))
    fig.legend(handles=handles, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 0.96))
    fig.suptitle(f"F10  Throughput against latency under load at {fmt_size(db)} x 1 KiB (S3 retrieves one bit), "
                 "medians over runs", x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    return fig


def f11_cores(t: Tables) -> plt.Figure:
    curves, db = _focus_load(t)
    mc = curves[curves.experiment == "multicore"]
    if mc.empty:
        raise Skip("no multicore rows; run harness/loadgen.py with --experiments multicore")
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 3.8))
    ws = sorted(mc.workers.unique())
    ax.plot(ws, ws, color=AXIS, linewidth=1, linestyle="--", label="ideal: W x the W = 1 throughput")
    for arm in [a for a in grid.ARMS if a in set(mc.arm)]:
        a = mc[mc.arm == arm].sort_values("workers")
        _arm_line(ax, arm, a.workers, a.throughput_qps / a.throughput_qps.iloc[0])
    ax.set_ylabel("throughput relative to W = 1")
    ax.legend(loc="upper left", ncol=2)
    ov = core_scaling(mc)
    for arm, g in ov.groupby("arm", sort=False):
        _arm_line(ax2, arm, g.workers, g.overhead)
        ax2.annotate(g.pair.iloc[0], (g.workers.iloc[-1], g.overhead.iloc[-1]), textcoords="offset points",
                     xytext=(6, 0), va="center", fontsize=7, color=INK_2)
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:g}×"))
    ax2.set_ylabel("throughput, baseline / verifiable")
    for a in (ax, ax2):
        a.set_xticks(ws)
        a.set_xlabel("server workers, W")
    fig.suptitle(f"F11  Throughput and overhead against server cores at {fmt_size(db)} x 1 KiB, "
                 "medians over runs", x=0.02, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


FIGURE_BUILDERS: dict[str, Callable[[Tables], plt.Figure]] = {
    "F1_reproduction": f1_reproduction, "F2_overhead_heatmaps": f2_heatmaps, "F3_absolute_cost": f3_absolute,
    "F4_amortised": f4_amortised, "F5_network": f5_network, "F6_breakdown": f6_breakdown,
    "F8_session": f8_session, "F9_emulated_network": f9_network, "F10_load_latency": f10_load,
    "F11_cores": f11_cores,
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


def _markdown(df: pd.DataFrame, title: str, synthetic_run: bool, note: str | None = None) -> str:
    def cell(v) -> str:
        if isinstance(v, float):
            return "" if math.isnan(v) else f"{v:.4g}"
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = [f"# {title}", ""]
    if note:
        lines += [f"> {note}", ""]
    if synthetic_run:
        lines += [f"> **{SYNTHETIC_MARK}.** Rows from analysis/synthetic.py, an invented cost model.", ""]
    lines += ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines) + "\n"


TABLE_TITLES = {"T1_machine": "T1 Machine, toolchain and pinned SHAs", "T2_scheme_params": "T2 Scheme parameters",
                "T3_reproduction": "T3 Reproduction: ours against published",
                "T4_overheads": "T4 Overhead summary with 95 % CIs"}
TABLE_NOTES = {
    "T3_reproduction": "**Identifiers.** `id` is a published reproduction target (R1 to R28), quoted with its paper and "
                       "page in docs/reproduction_targets.md and compared by the `rule` in "
                       "configs/reproduction_targets.yaml. This table answers RQ1, which vpir_bench_spec.md §2 defines "
                       "along with the other research questions. §12 defines the phases, gates (G3 is the human review "
                       "of this table), figures (F1 draws it) and tables.",
}


def render(t: Tables, out: Path, *, synthetic_run: bool, sysinfo: Path = SYSINFO) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    _style()
    written = []
    for name, build_fig in FIGURE_BUILDERS.items():
        try:
            fig = build_fig(t)
        except Skip as e:
            print(f"skipped {name}: {e}", file=sys.stderr)
            continue
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
        (out / f"{name}.md").write_text(_markdown(table, TABLE_TITLES[name], synthetic_run, TABLE_NOTES.get(name)),
                                          encoding="utf-8")
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
    p.add_argument("--session", type=Path, help="results/raw/<run_id>/session from session_check.py, for F8")
    p.add_argument("--netem", type=Path, help="results/raw/<run_id>/netem from netem.py, for F9")
    p.add_argument("--loadgen", type=Path, help="results/raw/<run_id>/loadgen from harness/loadgen.py, for F10, F11")
    p.add_argument("--recovery", type=Path, nargs="+",
                   help="results/raw/<run_id> recovery runs from harness/recovery.py, later ones replacing earlier "
                        "Runs of the same name, for F12 and F13")
    args = p.parse_args(argv)
    if args.synthetic:
        if args.session or args.netem or args.loadgen or args.recovery:
            p.error("--synthetic builds its own session, network and load rows; drop --session, --netem, --loadgen "
                    "and --recovery")
        g = grid.load()
        df = frame.rows_to_frame(synthetic.rows_for_cells(grid.expand(g, args.synthetic)))
        status = pd.DataFrame()
        session_rows = synthetic.rows_for_cells(session_check.session_cells(g, "focus", session_check.QUERIES),
                                                run_id="synthetic-session")
        session = session_check.compare(df, frame.rows_to_frame(session_rows))
        cells = netem.netem_cells(g, "focus", netem.QUERIES)
        netem_rows = pd.concat([
            frame.rows_to_frame([r for c in cells for r in synthetic.client_rows_for(c, prof, run_id="synthetic-netem")
                                 ]).assign(profile=name)
            for name, prof in amortise.load_profiles().items()], ignore_index=True)
        load = loadgen.yaml.safe_load(loadgen.LOAD_PATH.read_text())
        loadgen_rows = frame.loadgen_frame(synthetic.loadgen_points(loadgen.targets(g, "focus"), load))
        processed_dir = args.processed or args.out / "processed"
    else:
        if not args.reference_platform.exists():
            p.error(f"{args.reference_platform} is missing; run make sysinfo on the reference Mac")
        for d in (args.session, args.netem, args.loadgen):
            if d is not None and not d.is_dir():
                p.error(f"{d} is not a directory")
        reference = args.reference_platform.read_text().strip()
        df, status = frame.load_runs(args.runs, reference)
        session = frame.load_session(args.session, reference) if args.session else None
        if session is not None:
            grid_run = json.loads((args.session / frame.SESSION_COMPARISON).read_text())["grid_run"]
            if grid_run not in {d.name for d in args.runs}:
                p.error(f"{args.session} compares against grid run {grid_run}, which is not among --runs; "
                        "F8 would draw two different runs' medians")
        netem_rows = frame.load_netem(args.netem, reference) if args.netem else None
        loadgen_rows = frame.load_loadgen(args.loadgen, reference) if args.loadgen else None
        processed_dir = args.processed or aggregate.PROCESSED
    if df.empty:
        p.error("no usable rows")
    t = build(df, status, session, netem_rows, loadgen_rows)
    aggregate.write_tables(processed(t), processed_dir)
    written = render(t, args.out, synthetic_run=bool(args.synthetic), sysinfo=args.sysinfo)
    if args.recovery:
        written += rq4.figures(args.recovery, args.out)
    elif not args.synthetic:
        print("skipped F12_recovery_cost, F13_expected_cost: no recovery runs; pass --recovery "
              "results/raw/<run_id> ...", file=sys.stderr)
    for path in written:
        print(path)
    return written


if __name__ == "__main__":
    main()
