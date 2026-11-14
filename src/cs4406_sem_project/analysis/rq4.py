"""RQ4 figures F12 (recovery cost) and F13 (expected cost against the cheating rate) from recovery runs.

notebooks/11_rq4_analysis.ipynb prints the tables behind both figures, and `make figures RECOVERY=...` (plots.py
--recovery) redraws them. A Run in a later run directory replaces the Run of the same name in an earlier one, as a
resumed Run's latest attempt does.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

B = 10_000  # spec 6: 10 000 bootstrap resamples
SEED = 1234
VERIFIABLE = ["S1", "S2", "S3", "S4", "M1"]
POLICIES = ["abort", "switch_replica", "re_preprocess"]
FAULTS = ["flip_answer_bit", "wrong_record", "stale_record", "drop_server"]
RP_FAULT = "wrong_record, f = 0.1"

# Reference palette (dataviz skill, references/palette.md), light mode; categorical slots 1 to 3 in order. The
# validator passes it, with re_preprocess's green at 2.74:1 against the surface, so its points carry direct labels.
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
COLOR = {"abort": "#2a78d6", "switch_replica": "#eb6834", "re_preprocess": "#1baf7a"}
RC = {"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
      "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "font.size": 9, "axes.grid": True,
      "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False, "axes.spines.right": False}
TICK = {"flip_answer_bit": "flip\nbit", "wrong_record": "wrong\nrecord", "stale_record": "stale\nrecord",
        "drop_server": "drop\nserver", RP_FAULT: "wrong record\nf = 0.1"}
PLAIN = FuncFormatter(lambda v, _: f"{v:g}")


def _num(x) -> float:
    return 0.0 if x is None else float(x)


def load(runs: list[Path]) -> tuple[pd.DataFrame, dict, dict, list[dict]]:
    """Records of every ok Run, warm-ups dropped: (frame, manifest by Run name, source run per Run, run metas)."""
    manifest, source, metas = {}, {}, []
    for run_dir in runs:
        meta = json.loads((run_dir / "meta.json").read_text())
        if not meta["citable"] or meta["dirty"]:
            raise ValueError(f"{run_dir}: only a citable run from a clean tree is analysed")
        metas.append(meta)
        for line in (run_dir / "manifest.jsonl").read_text().splitlines():
            row = json.loads(line)
            manifest[row["name"]], source[row["name"]] = row, run_dir
    recs = []
    for name, m in manifest.items():
        if m["outcome"] != "ok":
            continue
        run = m["run"]
        for line in (source[name] / f"{name}.jsonl").read_text().splitlines():
            r = json.loads(line)
            if r["is_warmup"]:
                continue
            x = r["metrics"]
            row = {k: run[k] for k in ("arm", "kind", "fault", "policy", "fault_prob", "replicas_cheat", "db_bytes")}
            row.update(phase=r["phase"], faulted=r["faulted"], detected=r["detected"], correct=r["correct"],
                       outcome=r["recovery_outcome"], attempts=r["recovery_attempts"])
            if r["phase"] == "online":
                # Fault-free latency on one machine (spec 6, latency_e2e without the network terms): the two
                # servers of M0 and M1 answer in parallel, so their slower answer counts.
                row["first_s"] = (_num(x["query_gen_s"]) + max(_num(s) for s in x["server_answer_s"])
                                  + _num(x["client_recover_s"]) + _num(x["client_verify_s"]))
                row["recovery_s"] = _num(x["recovery_s"])
                row["time_to_end_s"] = row["first_s"] + row["recovery_s"]  # to a verified record, or to abort
                row["first_bytes"] = _num(x["up_bytes"]) + _num(x["down_bytes"])
                row["extra_bytes"] = _num(x["recovery_up_bytes"]) + _num(x["recovery_down_bytes"])
            else:
                row["offline_s"] = sum(_num(x[k]) for k in ("public_params_s", "server_preproc_s",
                                                             "server_preproc_proof_s", "client_offline_s"))
            recs.append(row)
    return pd.DataFrame(recs), manifest, source, metas


def boot_ci(values, rng: np.random.Generator, stat=np.median) -> tuple[float, float, float]:
    v = np.asarray(values, float)
    if len(v) == 0:
        return (np.nan, np.nan, np.nan)
    s = stat(v[rng.integers(0, len(v), size=(B, len(v)))], axis=1)
    return (float(stat(v)), float(np.quantile(s, 0.025)), float(np.quantile(s, 0.975)))


def _boot_mean(v, rng: np.random.Generator) -> np.ndarray:
    v = np.asarray(v, float)
    if len(v) == 0:
        return np.zeros(B)
    return v[rng.integers(0, len(v), size=(B, len(v)))].mean(axis=1)


def first_answers(df: pd.DataFrame) -> pd.DataFrame:
    """The first answer of every faulted trial, whatever the policy."""
    return df[(df.kind == "trials") & df.faulted.astype(bool)]


def f12_table(df: pd.DataFrame, rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    """What a rejection costs, per fault and policy, as a multiple of the arm's fault-free latency (spec 6,
    recovery_cost_ratio). Trials give abort and switch_replica for every fault. re_preprocess comes from the f = 0.1
    sweep (wrong_record), where a rejected query is followed by a fresh offline phase. Returns (base, f12)."""
    first = first_answers(df)
    base = (df[(df.kind == "sweep") & (df.fault_prob == 0)].groupby("arm")
            .agg(latency_s=("first_s", "median"), bytes=("first_bytes", "median")))
    rows = []

    def add(sel, arm, fault, policy):
        if len(sel) == 0:
            return
        t = boot_ci(sel.time_to_end_s / base.latency_s[arm], rng)
        b = boot_ci(sel.extra_bytes / base.bytes[arm], rng)
        oc = sel.outcome.value_counts()
        rows.append(dict(arm=arm, fault=fault, policy=policy, n=len(sel), ratio=t[0], ratio_lo=t[1], ratio_hi=t[2],
                         time_s=float(sel.time_to_end_s.median()), extra_bytes_ratio=b[0], extra_lo=b[1],
                         extra_hi=b[2], recovered=int(oc.get("recovered", 0)), aborted=int(oc.get("aborted", 0)),
                         silent_wrong=int(oc.get("silent_wrong", 0))))

    for arm in VERIFIABLE:
        for fault in FAULTS:
            for policy in ("abort", "switch_replica"):
                add(first[(first.arm == arm) & (first.fault == fault) & (first.policy == policy)], arm, fault, policy)
        rp = df[(df.kind == "sweep") & (df.arm == arm) & (df.policy == "re_preprocess") & (df.attempts > 0)]
        add(rp, arm, RP_FAULT, "re_preprocess")
    return base, pd.DataFrame(rows)


def expected(policy: str, f, h, r, s, o, d):
    """Spec 6.1's expected time per query at cheating rate f, from component costs (see f13_tables)."""
    fd = f * d
    e = (1 - fd) * h + fd * r
    reach = fd * (1 + fd + fd ** 2)
    if policy == "switch_replica":
        e = e + reach * ((1 - fd) * s + fd * r)
    elif policy == "re_preprocess":
        e = e + reach * (o + (1 - fd) * h + fd * r)
    return e


def f13_tables(df: pd.DataFrame, rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Expected cost per query against the cheating rate f. Measured: the sweep's mean time to a verified record or
    to abort, bootstrap 95 % CI. Computed (spec 6.1) from component costs measured outside the sweep:
      h  an honest first attempt: the f = 0 control
      r  a rejected first attempt: the wrong_record abort trials
      s  an honest further attempt: the wrong_record switch_replica trials, whose replicas are honest
      o  an offline phase, for re_preprocess: the flip_hint_bit offline Runs
      d  the detection rate: the wrong_record trials
    A cheat is caught with probability d and costs r; a missed one is accepted and costs h. After a rejection,
    further attempt k (up to 3) is reached with probability (f d)^(k-1), and a rejected further attempt is taken to
    cost r. Returns (measured against computed per point, computed curves, components per arm)."""
    sweep = df[df.kind == "sweep"]
    first = first_answers(df)
    trial_wr = first[first.fault == "wrong_record"]
    comp_rows, meas_rows, components = [], [], {}
    for arm in VERIFIABLE:
        sw = sweep[sweep.arm == arm]
        t = trial_wr[trial_wr.arm == arm]
        parts = dict(h=sw[sw.fault_prob == 0].first_s, r=t[t.policy == "abort"].time_to_end_s,
                     s=t[(t.policy == "switch_replica") & (t.attempts > 0)].recovery_s,
                     o=df[(df.kind == "offline") & (df.arm == arm) & (df.fault == "flip_hint_bit")].offline_s,
                     d=t.detected.astype(float))
        point = {k: float(v.mean()) if len(v) else 0.0 for k, v in parts.items()}
        boots = {k: _boot_mean(v, rng) for k, v in parts.items()}
        components[arm] = {k: (point[k], len(parts[k])) for k in parts}
        for policy in POLICIES:
            if not (sw.policy == policy).any():
                continue
            meas = sw[(sw.policy == policy) | ((sw.fault_prob == 0) & (sw.policy == "abort"))]
            for f, g in meas.groupby("fault_prob"):
                m = boot_ci(g.time_to_end_s, rng, np.mean)
                cv = expected(policy, f, **boots)
                c = expected(policy, f, **point)
                meas_rows.append(dict(arm=arm, policy=policy, f=f, n=len(g), measured=m[0], m_lo=m[1], m_hi=m[2],
                                      computed=c, c_lo=float(np.quantile(cv, 0.025)),
                                      c_hi=float(np.quantile(cv, 0.975)), rel_err=m[0] / c - 1,
                                      agree=bool(m[1] <= np.quantile(cv, 0.975) and np.quantile(cv, 0.025) <= m[2])))
            for f in np.concatenate([[0.0], np.geomspace(1e-3, 1, 60)]):
                comp_rows.append(dict(arm=arm, policy=policy, f=f, computed=expected(policy, f, **point)))
    return pd.DataFrame(meas_rows), pd.DataFrame(comp_rows), components


def _plain_log_y(ax, subs=(1.0,)) -> None:
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=subs))
    ax.yaxis.set_major_formatter(PLAIN)
    ax.yaxis.set_minor_formatter(NullFormatter())


def _dodge_plot(ax, data, y, lo, hi, cats) -> None:
    for i, policy in enumerate(POLICIES):
        d = data[data.policy == policy]
        xs = [cats.index(f) + (i - 1) * 0.22 for f in d.fault]
        ax.errorbar(xs, d[y], yerr=[d[y] - d[lo], d[hi] - d[y]], fmt="o", ms=5, color=COLOR[policy],
                    ecolor=COLOR[policy], elinewidth=1.5, capsize=0, markeredgecolor=SURFACE, markeredgewidth=1.5,
                    label=policy)
        if policy == "re_preprocess":
            for x, v, n in zip(xs, d[y], d.n):
                ax.annotate(f"x{v:,.0f}\n(n = {n})", (x, v), xytext=(-6, 0), textcoords="offset points",
                            ha="right", va="center", fontsize=7, color=INK2)
    ax.set_xticks(range(len(cats)), [TICK[c] for c in cats])
    ax.set_xlim(-0.5, len(cats) - 0.5)


def plot_f12(f12: pd.DataFrame) -> plt.Figure:
    cats = FAULTS + [RP_FAULT]
    with plt.rc_context(RC):
        fig, axes = plt.subplots(2, len(VERIFIABLE), figsize=(15, 6.4), sharey="row")
        for j, arm in enumerate(VERIFIABLE):
            d = f12[f12.arm == arm]
            _dodge_plot(axes[0, j], d, "ratio", "ratio_lo", "ratio_hi", cats)
            axes[0, j].set_title(arm, color=INK, fontsize=10, loc="left")
            _dodge_plot(axes[1, j], d[(d.policy != "abort") & (d.extra_bytes_ratio > 0)],
                        "extra_bytes_ratio", "extra_lo", "extra_hi", cats)
            for ax in axes[:, j]:
                _plain_log_y(ax)
                ax.tick_params(axis="x", labelsize=7)
        axes[0, 0].set_ylabel("time to verified record or abort\n(x fault-free latency)")
        axes[1, 0].set_ylabel("extra bytes, recovering policies\n(x fault-free query bytes; abort adds none)")
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper left", ncol=3, frameon=False, bbox_to_anchor=(0.01, 1.0),
                   markerscale=1.4)
        fig.suptitle("F12  Recovery cost by fault and policy, 128 MiB x 1 KiB (S3 one bit; S4 at 64 KiB). "
                     "Median, bootstrap 95 % CI", x=0.99, ha="right", fontsize=10, color=INK)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
    return fig


def plot_f13(f13: pd.DataFrame, curves: pd.DataFrame) -> plt.Figure:
    with plt.rc_context(RC):
        fig, axes = plt.subplots(1, len(VERIFIABLE), figsize=(15, 3.6), sharey=False)
        for j, arm in enumerate(VERIFIABLE):
            ax = axes[j]
            for policy in POLICIES:
                c = curves[(curves.arm == arm) & (curves.policy == policy)]
                if len(c) == 0:
                    continue
                ax.plot(c.f, c.computed, color=COLOR[policy], lw=2, label=f"{policy}, computed")
                m = f13[(f13.arm == arm) & (f13.policy == policy)]
                ax.errorbar(m.f, m.measured, yerr=[m.measured - m.m_lo, m.m_hi - m.measured], fmt="o", ms=5,
                            color=COLOR[policy], elinewidth=1.5, capsize=0, markeredgecolor=SURFACE,
                            markeredgewidth=1.5)
                if policy == "re_preprocess":
                    ax.annotate("re_preprocess", (0.5, c[c.f <= 0.5].computed.iloc[-1]), xytext=(-4, 6),
                                textcoords="offset points", ha="right", fontsize=7, color=INK2)
            # Linear from 0 to 0.001, where no rate was measured, then logarithmic.
            ax.set_xscale("symlog", linthresh=1e-3, linscale=0.4)
            ax.set_xlim(-2e-4, 1.15)
            ax.set_xticks([0, 1e-3, 1e-2, 1e-1, 1], ["0", "0.001", "0.01", "0.1", "1"])
            ax.xaxis.set_minor_formatter(NullFormatter())
            ax.set_title(arm, color=INK, fontsize=10, loc="left")
            ax.set_xlabel("cheating rate f")
        for ax in axes:  # decades over a wide range, 1-1.5-2-3-5-7 within one or two decades
            ax.set_yscale("log")
            lo, hi = ax.get_ylim()
            _plain_log_y(ax, subs=(1.0,) if hi / lo > 100 else (1.0, 1.5, 2.0, 3.0, 5.0, 7.0))
        axes[0].set_ylabel("mean time per query (s)")
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, [lbl.replace(", computed", "") for lbl in labels], loc="upper left", ncol=3,
                   frameon=False, bbox_to_anchor=(0.01, 1.02))
        fig.suptitle("F13  Expected cost against cheating rate: measured points (mean, 95 % CI) over the computed "
                     "curve", x=0.99, ha="right", fontsize=10, color=INK)
        fig.tight_layout(rect=(0, 0, 1, 0.92))
    return fig


def save(fig: plt.Figure, out: Path, name: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{name}.{ext}" for ext in ("png", "pdf")]
    for path in paths:
        fig.savefig(path, dpi=200)
    plt.close(fig)
    return paths


def figures(runs: list[Path], out: Path) -> list[Path]:
    """F12 and F13 from the recovery runs, drawn in notebook 11's order so the bootstrap draws match it."""
    df = load(runs)[0]
    rng = np.random.default_rng(SEED)
    f12 = f12_table(df, rng)[1]
    f13, curves, _ = f13_tables(df, rng)
    return save(plot_f12(f12), out, "F12_recovery_cost") + save(plot_f13(f13, curves), out, "F13_expected_cost")
