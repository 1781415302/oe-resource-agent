#!/usr/bin/env python3
"""Summarise and plot the three course-design experiments.

Reads the raw directories produced by
``run_cpu_three_way_study.py``, ``run_cgroup_share_study.py`` and
``run_network_isolation_study.py`` and writes:

    figures/fig1_cpu_p99.png|svg          two scenarios, three arms, two rounds
    figures/fig2_cgroup_share.png|svg     measured CPU share per arm and round
    figures/fig3_network_timeline.png|svg packet rate, isolation, foreground p99
    summary_cpu.csv, summary_cgroup.csv, summary_network.csv
    实验报告-三组对照.md

Two rounds are reported round by round and as a mean. Nothing here claims
statistical significance, and the negative or flat results are kept.
"""
import argparse
import csv
import json
import math
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

C_ARM = {"A": "#4C78A8", "B": "#F58518", "D": "#9467BD", "E": "#E377C2", "C": "#54A24B"}
C_ROUND = {1: "#4C78A8", 2: "#F58518"}
GRAY = "#666666"


def setup_font(candidates=("/root/fonts/simhei.ttf",)):
    for name in candidates:
        path = Path(name)
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            family = font_manager.FontProperties(fname=str(path)).get_name()
            plt.rcParams["font.family"] = family
            plt.rcParams["axes.unicode_minus"] = False
            return family
    plt.rcParams["axes.unicode_minus"] = False
    return None


def mean_sd(values):
    clean = [v for v in values if v is not None]
    if not clean:
        return None, None
    if len(clean) == 1:
        return clean[0], None
    return statistics.mean(clean), statistics.stdev(clean)


def fmt(value, digits=3):
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


# --------------------------------------------------------------------------- #
# Experiment 1: CPU three-way
# --------------------------------------------------------------------------- #
ARM_ORDER = ["A", "B", "D", "E", "C"]
ARM_NAMES = {"A": "A 默认 EEVDF", "B": "B 固定 SCX 1ms+抢占",
             "D": "D SCX 20ms+抢占（片长过长）", "E": "E SCX 300μs+抢占（片长过短）",
             "C": "C 网页加权 Agent"}
ARM_TICKS = {"A": "A 默认\nEEVDF", "B": "B 固定 1ms\n+唤醒抢占",
             "D": "D 固定 20ms\n+唤醒抢占", "E": "E 固定 300μs\n+唤醒抢占",
             "C": "C Agent\n（自己算片长）"}
ARM_SHORT = {"A": "A 默认", "B": "B 1ms", "D": "D 20ms", "E": "E 300μs", "C": "C Agent"}
SCENARIO_NAMES = {"throughput": "吞吐优先混部(4000RPS+4线程)", "latency": "延迟优先混部(25600RPS+1线程)"}


def load_cpu(directory):
    directory = Path(directory)
    study = json.loads((directory / "study.json").read_text(encoding="utf-8"))
    table = {}
    for trial in study["trials"]:
        key = (trial["scenario_id"], trial["arm"])
        entry = table.setdefault(key, {"rounds": {}, "summary": {}, "blocks": {}})
        entry["rounds"][trial["block"]] = trial
        entry.setdefault("measured_policy", trial.get("observed_policy_counts"))
        block_path = directory / trial.get("file", "")
        if block_path.is_file():
            entry["blocks"][trial["block"]] = json.loads(block_path.read_text(encoding="utf-8"))
    for key, entry in table.items():
        entry["mean"] = {}
        entry["sd"] = {}
        for field in ("p99_ms", "p95_ms", "rps", "eps", "completion_fraction"):
            values = [entry["rounds"][b]["summary"].get(field) for b in sorted(entry["rounds"])]
            entry["mean"][field], entry["sd"][field] = mean_sd(values)
        entry["mean"]["errors"] = sum(entry["rounds"][b]["summary"].get("errors") or 0
                                      for b in entry["rounds"])
    return study, table


def classify_windows(block):
    """Split one trial's windows at the Agent's last policy change.

    Exploratory: every window before the last time the Agent changed (or
    accepted) a configuration -- the default period plus every probe.
    Convergent: windows that start after that boundary, i.e. the configuration
    stopped changing and the Agent holds it. The boundary comes from the recorded
    event timestamps, never from the window index, so a trial that never settles
    simply has no convergent window.
    """
    samples = block.get("samples") or []
    events = block.get("agent_events") or []
    marks = [e["monotonic"] for e in events if e.get("action") in ("apply", "accept")]
    boundary = max(marks) if marks else None
    rows = []
    for window in block.get("windows") or []:
        start, end = window["start_monotonic"], window["end_monotonic"]
        configs = {(s["kernel"].get("policy_id"), s["kernel"].get("slice_us"))
                   for s in samples if start <= s["monotonic"] <= end}
        converged = boundary is not None and start >= boundary
        rows.append({"block": block.get("block"), "start": start, "end": end,
                     "p99_ms": window.get("p99_ms"), "p95_ms": window.get("p95_ms"),
                     "rps": window.get("rps"), "eps": window.get("eps"),
                     "errors": sum(window["errors"].values()) if isinstance(window.get("errors"), dict)
                     else (window.get("errors") or 0),
                     "configs": sorted((c[0] or "", c[1] or 0) for c in configs),
                     "boundary": boundary,
                     "segment": "converged" if converged else "explore"})
    return rows


def segment_table(table, scenarios):
    """Per trial: exploratory vs convergent window medians, agent arm only."""
    out = []
    for sid in scenarios:
        entry = table.get((sid, "C"))
        if not entry:
            continue
        for block in sorted(entry["blocks"]):
            rows = classify_windows(entry["blocks"][block])
            for segment in ("explore", "converged"):
                subset = [r for r in rows if r["segment"] == segment]
                p99s = [r["p99_ms"] for r in subset if r["p99_ms"] is not None]
                epss = [r["eps"] for r in subset if r["eps"] is not None]
                if not subset:
                    continue
                out.append({"scenario": sid, "block": block, "segment": segment,
                            "windows": len(subset),
                            "p99_ms": statistics.median(p99s) if p99s else None,
                            "eps": statistics.median(epss) if epss else None,
                            "per_window": p99s})
    return out


def plot_cpu(table, scenarios, path_png, path_svg):
    """Grouped bars: every measured round plus their mean, for every arm."""
    fig, axes = plt.subplots(1, len(scenarios), figsize=(14.0, 4.9), dpi=300)
    if len(scenarios) == 1:
        axes = [axes]
    for ax, sid in zip(axes, scenarios):
        top = 0.0
        rounds_all = sorted({b for arm in ARM_ORDER
                             for b in (table.get((sid, arm)) or {}).get("rounds", {})})
        single_round = len(rounds_all) == 1
        width = 0.5 if single_round else min(0.26, 1.4 / (len(rounds_all) + 1))
        if single_round:
            names = [f"第{rounds_all[0]}轮（窗口 p99 中位数）"]
            colors = [C_ROUND.get(rounds_all[0], "#777777")]
        else:
            names = [f"第{b}轮" for b in rounds_all] + ["均值"]
            colors = [C_ROUND.get(b, "#777777") for b in rounds_all] + ["#9E9E9E"]
        for i, arm in enumerate(ARM_ORDER):
            entry = table.get((sid, arm))
            if not entry:
                continue
            rounds = sorted(entry["rounds"])
            values = [entry["rounds"][b]["summary"].get("p99_ms") for b in rounds]
            values.append(entry["mean"].get("p99_ms"))
            if single_round:
                values = values[:1]
            offsets = [i + (j - (len(values) - 1) / 2) * width for j in range(len(values))]
            for j, (value, color, offset) in enumerate(zip(values, colors, offsets)):
                if value is None:
                    continue
                top = max(top, value)
                ax.bar(offset, value, width * 0.96, color=color, zorder=3,
                       label=names[j] if i == 0 else None)
                ax.annotate(f"{value:.2f}", (offset, value), textcoords="offset points",
                            xytext=(0, 2), ha="center", fontsize=6.2, color="#333333")
            if arm == "C":
                slices = [entry["rounds"][b].get("chosen_slice_us") for b in rounds]
                slices = [int(s) for s in slices if s]
                if slices:
                    ax.annotate("片长 " + "/".join(str(s) for s in slices) + "μs",
                                (i, 0.02 * (top or 1)), ha="center", fontsize=7.6, color="#1B5E20")
        ax.set_xticks(list(range(len(ARM_ORDER))))
        ax.set_xticklabels([ARM_TICKS[a] for a in ARM_ORDER], fontsize=8.2)
        ax.set_ylim(0, (top or 1) * 1.22)
        ax.set_xlim(-0.5, len(ARM_ORDER) - 0.5)
        ax.set_ylabel("网页 p99 延迟 (ms，窗口中位数)")
        ax.set_title(SCENARIO_NAMES.get(sid, sid), fontsize=10, pad=20)
        ax.grid(axis="y", alpha=0.3, zorder=0)
        ax.legend(fontsize=7.8, loc="lower center", bbox_to_anchor=(0.5, 1.0),
                  ncol=len(names), frameon=False)
    fig.suptitle("实验一 CPU 调度：五组对照（B/D/E 是同一 SCX 机制、只有片长不同）", fontsize=10.8)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path_png, bbox_inches="tight")
    fig.savefig(path_svg, bbox_inches="tight")
    plt.close(fig)


def segment_rows(table, scenarios):
    """Per-window rows of the Agent arm with the segment each window belongs to."""
    rows = []
    for sid in scenarios:
        entry = table.get((sid, "C"))
        if not entry:
            continue
        for block in sorted(entry["blocks"]):
            for row in classify_windows(entry["blocks"][block]):
                row.update(scenario=sid, block=block)
                rows.append(row)
    return rows


def plot_cpu_segments(table, rows, scenarios, path_png, path_svg):
    """Per-window series with the exploration phase shaded."""
    fig, axes = plt.subplots(1, len(scenarios), figsize=(12.6, 4.6), dpi=300)
    if len(scenarios) == 1:
        axes = [axes]
    for ax, sid in zip(axes, scenarios):
        series = sorted((r for r in rows if r["scenario"] == sid), key=lambda r: (r["block"], r["start"]))
        blocks = sorted({r["block"] for r in series})
        boundaries = []
        for block in blocks:
            points = [r for r in series if r["block"] == block]
            first_converged = next((i for i, r in enumerate(points) if r["segment"] == "converged"), None)
            if first_converged is not None:
                boundaries.append(first_converged + 0.5)
        if boundaries:
            ax.axvspan(0.5, min(boundaries), color="#B0B0B0", alpha=0.18, zorder=0)
            for value in sorted(set(boundaries)):
                ax.axvline(value, color="#666666", ls=":", lw=1.0, zorder=1)
        top = 0.0
        for block in blocks:
            points = [r for r in series if r["block"] == block]
            xs = list(range(1, len(points) + 1))
            ys = [r["p99_ms"] for r in points]
            ax.plot(xs, ys, marker="o", ms=4, lw=1.6, color=C_ROUND[block], zorder=4,
                    label=f"第{block}轮")
            top = max([top] + [v for v in ys if v is not None])
        for arm, style, name in (("A", "-.", "A 默认"), ("B", "--", "B 固定 1ms"),
                                 ("D", ":", "D 固定 20ms")):
            entry = table.get((sid, arm))
            if entry and entry["mean"].get("p99_ms") is not None:
                value = entry["mean"]["p99_ms"]
                ax.axhline(value, color=C_ARM.get(arm, "#9467BD"), ls=style, lw=1.1,
                           label=f"{name} {value:.2f}")
                top = max(top, value)
        ax.set_xticks(list(range(1, 9)))
        ax.set_xlabel("测量窗口序号（每窗 5 s）")
        ax.set_xlim(0.5, 8.5)
        ax.set_ylim(0, (top or 1) * 1.16)
        ax.set_ylabel("网页 p99 延迟 (ms)")
        ax.legend(fontsize=7.0, loc="upper left", ncol=3, framealpha=0.9)
        ax.set_title(SCENARIO_NAMES.get(sid, sid), fontsize=10, pad=20)
        ax.grid(axis="y", alpha=0.3, zorder=0)
        ax.legend(fontsize=7.4, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=4, frameon=False)
        ax.annotate("探索段（含首次切换）", (1.0, (top or 1) * 1.06), fontsize=8, color="#555555")
        ax.annotate("收敛段（不再改配置）", (5.2, (top or 1) * 1.06), fontsize=8, color="#555555")
    fig.suptitle("实验一 C 组（加权 Agent）逐窗口：以最后一次策略变动为界，灰底为探索段", fontsize=10.5)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path_png, bbox_inches="tight")
    fig.savefig(path_svg, bbox_inches="tight")
    plt.close(fig)


def write_cpu_segments_csv(segments, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["场景", "轮次", "分段", "窗口数", "p99 中位数", "后台EPS 中位数", "逐窗口 p99"])
        for row in sorted(segments, key=lambda r: (r["scenario"] != "throughput", r["block"], r["segment"])):
            writer.writerow([SCENARIO_NAMES.get(row["scenario"], row["scenario"]), row["block"],
                             "探索段" if row["segment"] == "explore" else "收敛段", row["windows"],
                             fmt(row["p99_ms"]), fmt(row["eps"], 1),
                             " / ".join(fmt(v) for v in row["per_window"])])


def cpu_segments_markdown(segments):
    lines = ["| 场景 | 轮次 | 分段 | 窗口数 | p99 中位数 (ms) | 后台EPS 中位数 | 逐窗口 p99 (ms) |",
             "|---|---:|---|---:|---:|---:|---|"]
    for row in sorted(segments, key=lambda r: (r["scenario"] != "throughput", r["block"], r["segment"])):
        lines.append("| {sc} | {b} | {seg} | {n} | {p} | {e} | {w} |".format(
            sc=SCENARIO_NAMES.get(row["scenario"], row["scenario"]), b=row["block"],
            seg="探索段" if row["segment"] == "explore" else "收敛段", n=row["windows"],
            p=fmt(row["p99_ms"]), e=fmt(row["eps"], 1),
            w=" / ".join(fmt(v) for v in row["per_window"])))
    lines.append("")
    return lines


def write_cpu_csv(table, scenarios, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["场景", "组别", "轮次", "p99_ms", "p95_ms", "实际RPS", "完成率",
                         "后台EPS", "请求错误", "最终内核片长_us"])
        for sid in scenarios:
            for arm in ARM_ORDER:
                entry = table.get((sid, arm))
                if not entry:
                    continue
                for block in sorted(entry["rounds"]):
                    summary = entry["rounds"][block]["summary"]
                    writer.writerow([SCENARIO_NAMES.get(sid, sid), ARM_NAMES[arm], block,
                                     fmt(summary.get("p99_ms")), fmt(summary.get("p95_ms")),
                                     fmt(summary.get("rps"), 2), fmt(summary.get("completion_fraction"), 5),
                                     fmt(summary.get("eps"), 2), summary.get("errors"),
                                     entry["rounds"][block].get("chosen_slice_us") or ""])
                writer.writerow([SCENARIO_NAMES.get(sid, sid), ARM_NAMES[arm], "均值",
                                 fmt(entry["mean"].get("p99_ms")), fmt(entry["mean"].get("p95_ms")),
                                 fmt(entry["mean"].get("rps"), 2),
                                 fmt(entry["mean"].get("completion_fraction"), 5),
                                 fmt(entry["mean"].get("eps"), 2), entry["mean"].get("errors"), ""])
                writer.writerow([SCENARIO_NAMES.get(sid, sid), ARM_NAMES[arm], "样本SD",
                                 fmt(entry["sd"].get("p99_ms")), fmt(entry["sd"].get("p95_ms")),
                                 fmt(entry["sd"].get("rps"), 2), "", fmt(entry["sd"].get("eps"), 2), "", ""])


def cpu_markdown(table, scenarios):
    lines = []
    for sid in scenarios:
        lines.append(f"### {SCENARIO_NAMES.get(sid, sid)}")
        lines.append("")
        lines.append("| 组别 | p99 轮1/轮2/均值±SD (ms) | 实际RPS 均值 | 后台EPS 轮1/轮2/均值 | 错误 |")
        lines.append("|---|---:|---:|---:|---:|")
        for arm in ARM_ORDER:
            entry = table.get((sid, arm))
            if not entry:
                lines.append(f"| {ARM_NAMES[arm]} | 缺失 | — | — | — |")
                continue
            rounds = sorted(entry["rounds"])
            p99 = [entry["rounds"][b]["summary"].get("p99_ms") for b in rounds]
            eps = [entry["rounds"][b]["summary"].get("eps") for b in rounds]
            mean_p99 = entry["mean"].get("p99_ms")
            sd_p99 = entry["sd"].get("p99_ms")
            lines.append(
                "| {arm} | {p1} / {p2} / {m} ± {s} | {rps} | {e1} / {e2} / {em} | {err} |".format(
                    arm=ARM_NAMES[arm],
                    p1=fmt(p99[0] if len(p99) > 0 else None),
                    p2=fmt(p99[1] if len(p99) > 1 else None),
                    m=fmt(mean_p99),
                    s=fmt(sd_p99 if sd_p99 is not None else 0.0),
                    rps=fmt(entry["mean"].get("rps"), 1),
                    e1=fmt(eps[0] if len(eps) > 0 else None, 1),
                    e2=fmt(eps[1] if len(eps) > 1 else None, 1),
                    em=fmt(entry["mean"].get("eps"), 1),
                    err=entry["mean"].get("errors")))
        agent_entry = table.get((sid, "C"))
        if agent_entry:
            chosen = [f"第{block}轮 {int(agent_entry['rounds'][block]['chosen_slice_us'])}µs"
                      for block in sorted(agent_entry["rounds"])
                      if agent_entry["rounds"][block].get("chosen_slice_us")]
            if chosen:
                lines.append("")
                lines.append("Agent 最终参数（内核回读）：" + "；".join(chosen))
        lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# Experiment 2: cgroup shares
# --------------------------------------------------------------------------- #
ARM2_NAMES = {"A": "A 等份额 1024:1024", "B": "B 4:1 权重 4096:1024"}


def load_cgroup(directory):
    study = json.loads((Path(directory) / "study.json").read_text(encoding="utf-8"))
    runs = []
    for entry in study["runs"]:
        run = dict(entry)
        path = Path(directory) / entry["file"]
        if path.is_file():
            run["detail"] = json.loads(path.read_text(encoding="utf-8"))
        runs.append(run)
    return study, runs


def plot_cgroup(study, runs, path_png, path_svg):
    fig, ax = plt.subplots(figsize=(9.8, 4.9), dpi=300)
    ordered = sorted(runs, key=lambda r: (r["block"], r["arm"]))
    labels, high, normal, ratios = [], [], [], []
    for run in ordered:
        result = run.get("result") or {}
        h = (result.get("high") or {}).get("cpu_share_pct")
        n = (result.get("normal") or {}).get("cpu_share_pct")
        labels.append(f"第{run['block']}轮 {run['arm']} 组\nshares {run['low_shares']}:{run['high_shares']}")
        high.append(h or 0.0)
        normal.append(n or 0.0)
        ratios.append(result.get("measured_high_to_normal_ratio"))
    x = list(range(len(labels)))
    ax.bar(x, high, 0.55, color="#E45756", label="高优先组 (latency)", zorder=3)
    ax.bar(x, normal, 0.55, bottom=high, color="#72B7B2", label="普通组 (batch)", zorder=3)
    for i, (h, n) in enumerate(zip(high, normal)):
        ax.annotate(f"{h:.1f}%", (i, h / 2), ha="center", va="center", fontsize=9.5, color="white")
        ax.annotate(f"{n:.1f}%", (i, h + n / 2), ha="center", va="center", fontsize=9.5, color="white")
        if ratios[i]:
            ax.annotate(f"实测 {ratios[i]:.2f} : 1", (i, 102.5), ha="center", fontsize=9, color="#222222")
    line50 = ax.axhline(50, color=GRAY, ls=":", lw=1.1)
    line80 = ax.axhline(80, color=GRAY, ls="--", lw=1.1)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8.5)
    ax.set_ylim(0, 112)
    ax.set_ylabel("实测 CPU 时间占比 (%)")
    ax.set_title("实验二 Cgroup 权重：默认调度下两个同核持续计算任务的 CPU 份额", fontsize=10.5)
    ax.grid(axis="y", alpha=0.3, zorder=0)
    handles, names = ax.get_legend_handles_labels()
    handles += [line80, line50]
    names += ["80% 理论值 (4096:1024)", "50% 等分"]
    ax.legend(handles, names, fontsize=7.8, loc="upper center", bbox_to_anchor=(0.5, -0.19),
              ncol=4, frameon=False)
    fig.tight_layout()
    fig.savefig(path_png, bbox_inches="tight")
    fig.savefig(path_svg, bbox_inches="tight")
    plt.close(fig)


def write_cgroup_csv(runs, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["轮次", "组别", "高优先shares", "普通shares", "高优先CPU秒", "普通CPU秒",
                         "高优先占比%", "普通占比%", "实测比值", "理论比值",
                         "高优先工作量", "普通工作量", "清理状态"])
        for run in runs:
            result = run.get("result") or {}
            h = result.get("high") or {}
            n = result.get("normal") or {}
            cleanup = run.get("cleanup") or {}
            groups = cleanup.get("groups") or {}
            cleanup_ok = all((state.get("removed") is True) or (state.get("present") is False)
                             for state in groups.values()) if groups else None
            writer.writerow([run["block"], run["arm"], run["low_shares"], run["high_shares"],
                             fmt(h.get("cpu_seconds")), fmt(n.get("cpu_seconds")),
                             fmt(h.get("cpu_share_pct")), fmt(n.get("cpu_share_pct")),
                             fmt(result.get("measured_high_to_normal_ratio")),
                             fmt(result.get("configured_ratio")),
                             h.get("work_units"), n.get("work_units"),
                             "已迁出并删除组" if cleanup_ok else "见原始JSON"])


def cgroup_markdown(study, runs):
    lines = ["| 轮次 | 组别 | shares | 高优先 CPU 秒 | 普通 CPU 秒 | 高优先占比 | 普通占比 | 实测比值 |",
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for run in runs:
        result = run.get("result") or {}
        h = result.get("high") or {}
        n = result.get("normal") or {}
        lines.append("| {b} | {a} | {lo}:{hi} | {hs} | {ns} | {hp}% | {np}% | {r} : 1 |".format(
            b=run["block"], a=ARM2_NAMES.get(run["arm"], run["arm"]), lo=run["low_shares"],
            hi=run["high_shares"], hs=fmt(h.get("cpu_seconds")), ns=fmt(n.get("cpu_seconds")),
            hp=fmt(h.get("cpu_share_pct")), np=fmt(n.get("cpu_share_pct")),
            r=fmt(result.get("measured_high_to_normal_ratio"))))
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# Experiment 3: eBPF port isolation
# --------------------------------------------------------------------------- #
def load_network(directory):
    directory = Path(directory)
    study = json.loads((directory / "study.json").read_text(encoding="utf-8"))
    rounds = []
    for entry in study["runs"]:
        path = directory / f"block-{entry['block']:02d}-{entry['arm'].lower()}" / "round.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_dir"] = str(path.parent)
            rounds.append(data)
    return study, rounds


def rate_series(round_data):
    samples = round_data.get("samples") or []
    clock, pps = [], []
    previous = None
    for sample in samples:
        if previous is not None:
            dt = sample["monotonic"] - previous["monotonic"]
            dp = sample["target_packets"] - previous["target_packets"]
            if dt > 0:
                clock.append(sample["clock"])
                pps.append(dp / dt)
        previous = sample
    return clock, pps


def drop_windows(round_data):
    spans, start = [], None
    for sample in round_data.get("samples") or []:
        active = bool(sample.get("drop_target") or sample.get("coordinator_dropping"))
        if active and start is None:
            start = sample["clock"]
        elif not active and start is not None:
            spans.append((start, sample["clock"]))
            start = None
    if start is not None:
        spans.append((start, (round_data.get("samples") or [{}])[-1].get("clock")))
    return spans


def phase_bounds(round_data):
    schedule = round_data.get("schedule_seconds") or {}
    baseline = schedule.get("baseline", 12)
    flood = schedule.get("flood", 20)
    recovery = schedule.get("recovery", 15)
    return baseline, baseline + flood, baseline + flood + recovery


def window_series(round_data):
    clock0 = round_data.get("stream_ready_monotonic")
    xs, ys = [], []
    for window in round_data.get("windows") or []:
        if clock0 is None:
            break
        xs.append((window["start_monotonic"] - clock0 + window["end_monotonic"] - clock0) / 2)
        ys.append(window.get("p99_ms"))
    return xs, ys


def bucket_mean(series_list, step):
    """Average several (x, y) series onto a common grid of ``step`` seconds."""
    buckets = {}
    for xs, ys in series_list:
        for x, y in zip(xs, ys):
            if y is None:
                continue
            key = int(round(x / step))
            buckets.setdefault(key, []).append(y)
    ordered = sorted(buckets)
    return [k * step for k in ordered], [statistics.mean(buckets[k]) for k in ordered]


def plot_network(rounds, path_png, path_svg):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11.4, 7.4), dpi=300, sharex=True,
                                   gridspec_kw={"height_ratios": [1, 1]})
    baseline, flood_end, total = phase_bounds(rounds[0])
    for ax in (ax1, ax2):
        ax.axvspan(0, baseline, color="#B0B0B0", alpha=0.10)
        ax.axvspan(flood_end, total, color="#7EB26D", alpha=0.08)
        ax.axvline(baseline, color="#333333", ls="--", lw=1)
        ax.axvline(flood_end, color="#333333", ls="--", lw=1)
    isolated_at = None
    for round_data in rounds:
        if round_data["arm"] != "B":
            continue
        for start, end in drop_windows(round_data):
            for ax in (ax1, ax2):
                ax.axvspan(start, end, color="#E45756", alpha=0.10)
            if isolated_at is None:
                isolated_at = start
    for arm in ("A", "B"):
        subset = [r for r in rounds if r["arm"] == arm]
        if not subset:
            continue
        for round_data in subset:
            clock, pps = rate_series(round_data)
            ax1.plot(clock, pps, color=C_ARM[arm], lw=0.8, alpha=0.35)
            xs, ys = window_series(round_data)
            ax2.plot(xs, ys, color=C_ARM[arm], lw=0.8, alpha=0.35, marker="o", ms=2.0)
        xs, ys = bucket_mean([rate_series(r) for r in subset], 0.5)
        ax1.plot(xs, ys, color=C_ARM[arm], lw=2.2,
                 label=f"{arm} 组{'（只观测，不丢包）' if arm == 'A' else '（自动隔离）'}")
        xs2, ys2 = bucket_mean([window_series(r) for r in subset], 5.0)
        ax2.plot(xs2, ys2, color=C_ARM[arm], lw=2.2, marker="o", ms=4,
                 label=f"{arm} 组（两轮同窗序均值）")
    if isolated_at is not None:
        ax1.annotate("自动隔离生效\n目标端口包速率降到近零", (isolated_at + 0.6, ax1.get_ylim()[1] * 0.42),
                     fontsize=8, color="#B22222")
    ax1.annotate("正常", (baseline * 0.5, ax1.get_ylim()[1] * 0.93), ha="center", fontsize=9, color=GRAY)
    ax1.annotate("目标端口高频冲击", ((baseline + flood_end) / 2, ax1.get_ylim()[1] * 0.93),
                 ha="center", fontsize=9, color="#B22222")
    ax1.annotate("恢复", ((flood_end + total) / 2, ax1.get_ylim()[1] * 0.93), ha="center",
                 fontsize=9, color="#2E7D32")
    ax1.set_ylabel("目标端口包速率 (packets/s)")
    ax1.set_title("实验三 eBPF 端口隔离：目标端口包速率、自动丢包区间（浅红）与正常业务尾延迟",
                  fontsize=10.5, pad=22)
    ax1.grid(alpha=0.3)
    ax1.legend(fontsize=8, loc="lower center", bbox_to_anchor=(0.5, 1.005), ncol=2, frameon=False)
    ax1.set_ylim(bottom=-200)
    if isolated_at is not None:
        ax2.annotate("B 组第 2 轮首个窗口 p99 99ms：\n与 eBPF 钩子加载/挂载同时发生，保留不删",
                     (5.0, 99.0), textcoords="offset points", xytext=(12, -18), fontsize=7.5,
                     color="#B22222",
                     arrowprops=dict(arrowstyle="->", color="#B22222", lw=0.8))
    ax2.set_ylabel("正常业务 p99 (ms)")
    ax2.set_xlabel("相对时间 (秒)，从 wrk2 标定完成开始；细线为单轮，粗线为两轮均值")
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=8, loc="upper right", framealpha=0.9)
    fig.tight_layout()
    fig.savefig(path_png, bbox_inches="tight")
    fig.savefig(path_svg, bbox_inches="tight")
    plt.close(fig)


def phase_stats(round_data):
    baseline, flood_end, _total = phase_bounds(round_data)
    clock0 = round_data.get("stream_ready_monotonic")
    groups = {"baseline": [], "flood": [], "recovery": []}
    for window in round_data.get("windows") or []:
        if clock0 is None:
            continue
        middle = (window["start_monotonic"] + window["end_monotonic"]) / 2 - clock0
        phase = "baseline" if middle < baseline else "flood" if middle < flood_end else "recovery"
        groups[phase].append(window)
    stats = {}
    for phase, windows in groups.items():
        stats[phase] = {
            "windows": len(windows),
            "p99_ms": mean_sd([w.get("p99_ms") for w in windows])[0],
            "rps": mean_sd([w.get("rps") for w in windows])[0],
            "errors": sum(sum(v for v in (w.get("errors") or {}).values()) if isinstance(w.get("errors"), dict)
                          else (w.get("errors") or 0) for w in windows),
        }
    return stats


def write_network_csv(rounds, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["轮次", "组别", "阶段", "窗口数", "p99均值_ms", "RPS均值", "错误数"])
        for round_data in sorted(rounds, key=lambda r: (r["block"], r["arm"])):
            stats = phase_stats(round_data)
            for phase in ("baseline", "flood", "recovery"):
                row = stats[phase]
                writer.writerow([round_data["block"], round_data["arm"], phase, row["windows"],
                                 fmt(row["p99_ms"]), fmt(row["rps"], 1), row["errors"]])


def network_markdown(rounds):
    lines = ["| 轮次 | 组别 | 阶段 | 窗口数 | p99 均值 (ms) | RPS 均值 | 错误数 |",
             "|---|---|---|---:|---:|---:|---:|"]
    for round_data in sorted(rounds, key=lambda r: (r["block"], r["arm"])):
        stats = phase_stats(round_data)
        for phase in ("baseline", "flood", "recovery"):
            row = stats[phase]
            lines.append(f"| {round_data['block']} | {'A 只观测' if round_data['arm'] == 'A' else 'B 自动隔离'} | "
                         f"{ {'baseline': '正常', 'flood': '冲击', 'recovery': '恢复'}[phase] } | "
                         f"{row['windows']} | {fmt(row['p99_ms'])} | {fmt(row['rps'], 1)} | {row['errors']} |")
    lines.append("")
    lines.append("| 轮次 | 组别 | 隔离触发 (s) | 解除隔离 (s) | 目标端口连接 成功/失败 | 丢包数 |")
    lines.append("|---|---|---:|---:|---|---:|")
    for round_data in sorted(rounds, key=lambda r: (r["block"], r["arm"])):
        clock0 = round_data.get("stream_ready_monotonic")
        isolated = recovered = None
        for event in round_data.get("events") or []:
            if event.get("event") == "plugin_ebpf_isolated" and clock0 and event.get("monotonic"):
                isolated = event["monotonic"] - clock0
            if event.get("event") == "plugin_ebpf_recovered" and clock0 and event.get("monotonic"):
                recovered = event["monotonic"] - clock0
        totals = round_data.get("flood_totals") or {}
        dropped = (round_data.get("ebpf_final") or {}).get("stats") or {}
        lines.append("| {b} | {a} | {i} | {r} | {ok} / {bad} | {d} |".format(
            b=round_data["block"], a="A 只观测" if round_data["arm"] == "A" else "B 自动隔离",
            i=fmt(isolated, 1) if isolated is not None else "未触发",
            r=fmt(recovered, 1) if recovered is not None else "未解除",
            ok=totals.get("ok", "—"), bad=totals.get("failed", "—"),
            d=dropped.get("dropped_packets", "—")))
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    family = setup_font()
    figures = args.output / "figures"
    figures.mkdir(parents=True, exist_ok=True)

    cpu_study, cpu_table = load_cpu(args.cpu)
    scenarios = [s for s in ("throughput", "latency") if any(k[0] == s for k in cpu_table)]
    plot_cpu(cpu_table, scenarios, figures / "fig1_cpu_p99.png", figures / "fig1_cpu_p99.svg")
    write_cpu_csv(cpu_table, scenarios, args.output / "summary_cpu.csv")
    segments = segment_table(cpu_table, scenarios)
    if segments:
        plot_cpu_segments(cpu_table, segment_rows(cpu_table, scenarios), scenarios,
                          figures / "fig4_cpu_segments.png", figures / "fig4_cpu_segments.svg")
        write_cpu_segments_csv(segments, args.output / "summary_cpu_segments.csv")

    cgroup_study, cgroup_runs = load_cgroup(args.cgroup)
    plot_cgroup(cgroup_study, cgroup_runs, figures / "fig2_cgroup_share.png",
                figures / "fig2_cgroup_share.svg")
    write_cgroup_csv(cgroup_runs, args.output / "summary_cgroup.csv")

    net_study, net_rounds = load_network(args.network)
    plot_network(net_rounds, figures / "fig3_network_timeline.png",
                 figures / "fig3_network_timeline.svg")
    write_network_csv(net_rounds, args.output / "summary_network.csv")

    lines = ["# 三组对照实验：CPU 调度 / Cgroup 资源分配 / eBPF 网络隔离", ""]
    lines.append(f"图表统一使用中文字体：{family or '未找到中文字体，可能显示方块'}。"
                 "两轮数据逐轮列出并给出算术均值与样本标准差，只作描述性比较，不声称统计显著。")
    lines.append("")
    lines.append("## 实验一 CPU 调度")
    lines.append("")
    lines.extend(cpu_markdown(cpu_table, scenarios))
    lines.append("![CPU 三组对比](figures/fig1_cpu_p99.png)")
    lines.append("")
    if segments:
        lines.append("### C 组（加权 Agent）探索段与收敛段")
        lines.append("")
        lines.append("探索段＝窗口内发生内核切换，或窗口早于 Agent 最后一次 accept；"
                     "收敛段＝单一配置且位于最后一次决策之后。按记录的实际策略时间线切分，不按窗口序号切。")
        lines.append("")
        lines.extend(cpu_segments_markdown(segments))
        lines.append("![CPU 分段](figures/fig4_cpu_segments.png)")
        lines.append("")
    lines.append("## 实验二 Cgroup 资源分配")
    lines.append("")
    lines.append(f"调度前提：默认内核调度（sched_ext 初始 {cgroup_study.get('sched_ext_initial')}、"
                 f"结束 {cgroup_study.get('sched_ext_final')}）。")
    lines.append("")
    lines.extend(cgroup_markdown(cgroup_study, cgroup_runs))
    lines.append("![Cgroup 份额](figures/fig2_cgroup_share.png)")
    lines.append("")
    lines.append("## 实验三 eBPF 网络隔离")
    lines.append("")
    lines.extend(network_markdown(net_rounds))
    lines.append("![网络隔离时间线](figures/fig3_network_timeline.png)")
    (args.output / "自动汇总-表格.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(args.output),
                      "figures": sorted(p.name for p in figures.iterdir())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
