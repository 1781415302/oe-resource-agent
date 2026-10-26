#!/usr/bin/env python3
"""Publish audited experiment tables, high-resolution figures and an honest report."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys

POLICIES = ["default", "scx-1ms", "scx-500us", "scx-10ms", "agent"]
SHORT = ["默认", "1ms", "500μs", "10ms", "Agent"]
COLORS = ["#718096", "#437BC2", "#8262AC", "#CB8A37", "#087F8C"]


def fmt(value, digits=2):
    return "—" if value is None else f"{value:.{digits}f}"


def cell(row, key, digits=3):
    if not row or row[key] is None:
        return "—"
    return fmt(row[key], digits) + (" ± " + fmt(row.get(key+"_sd"), digits) if row.get(key+"_sd") is not None else "")


def write_report(data, dynamics, output):
    study = data["study"]
    if study.get("variant") == "weighted_v2":
        lines=["# 加权 Agent 实验数据（正式两轮）", "", "当前版本不设置 p99 和 EPS 性能达标线。默认延迟权重60%、吞吐权重40%，以完整HDR p99及实际RPS/EPS相对现场基线进行几何加权。", "",
               f"本次为{study['rounds']}轮、8场景、默认与加权Agent两组，共{len(study['trials'])}次试运行，每次{study['required_windows']}个完整5秒窗口。每轮窗口先取中位数，再计算轮间均值及样本SD。N=2仅作描述性比较。", "",
               "正式数据按用户要求只选最先完成的两轮，额外第三轮另存原始目录，未用于此表。场景、权重与代码来源信息保存在study.json。", "",
               "| 场景 | 默认p99(ms) | 加权Agent p99(ms) | EPS保留率(%) | 对照加权分 |", "|---|---:|---:|---:|---:|"]
        by={(r['scenario_id'],r['condition']):r for r in data['rows']}
        for sid in study['scenarios']:
            r=by[sid,'agent'];b=by[sid,'default']
            lines.append(f"| {r['scenario']} | {cell(b,'p99_ms')} | {cell(r,'p99_ms')} | {cell(r,'eps_retention_pct',1)} | {cell(r,'weighted_score',2)} |")
        lines += ["", "EPS保留率、对照加权分均按同轮默认重算；该对照参照和Agent内部的早期现场基线不同。几何评分使用延迟反比及实际吞吐比值，无性能达标否决线。两项吞吐同时可测时先对RPS比值与EPS比值取几何均值，再参与评分。", "",
                  "缺测维度不补零，不同场景EPS单位不同。主矩阵保留预热后的探索窗口，控制器可能尚在继续试用其他候选，不将本次结束状态称作全局最优。", "",
                  "新版的独立动态压力及反馈丢失复测尚未执行，不能把旧版26秒功能验证当成新版验证。PPT作者脚本尚未按两轮完成定稿和检查。", "",
                  "独立审计："+json.dumps(data['audit'],ensure_ascii=False)]
        (output/"实验报告.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
        return
    by = {(r["scenario_id"], r["condition"]): r for r in data["rows"]}
    labels = {r["scenario_id"]: r["scenario"] for r in data["rows"]}
    n = len(study["trials"])
    text = ["# 自主调度 Agent 实验报告", "", f"实验时间：{study['started_utc']} 至 {study.get('ended_utc', '采集中')}（UTC）。",
            f"环境：{study['machine']['os']}，内核 {study['machine']['kernel']}，{study['machine']['vcpus']} vCPU VMware 来宾。Boot ID：`{study['machine']['boot_id']}`。", "",
            "## 实验问题与判定", "",
            "验证三个问题：Agent 是否根据真实数据自主动作，是否改善业务指标，以及是否在约束未通过或反馈缺失时回退。单机短时结果不能证明任何负载上的全局最优。", "",
            "## 设计与口径", "",
            f"- {len(study['scenarios'])} 类负载、{len(study['policies'])} 组配置、{study['rounds']} 轮，共 {n} 次试运行。每次预热至少 5 秒，再取 {study['required_windows']} 个完整约 5 秒窗口。",
            "- 每轮重新创建各场景的负载进程。同一个场景块内，五配置使用相同业务 PID；条件顺序循环轮换，每组从默认调度起步。三轮不能完全抵消所有顺序效应。",
            "- 每次试运行先取窗口指标的中位数，再对三轮取均值及样本标准差。窗口 p99 的均值不是合并全部请求后的总体 p99。N=3 为描述性重复，不宣称统计显著。",
            "- 所有配置统一每轮最多 5 次短 HTTP 探测。正式延迟取原始 wrk2 HDR；实际 RPS 取完成数/窗口时长。后台 EPS 按原始报告与窗口的重叠时间加权，覆盖不足 80% 或交付时间差达到 2 秒的窗口不补值。",
            "- 主矩阵包含预热后发生的 Agent 策略过渡窗口，不仅统计保留后的最佳时段。动态实验另记录从压力开始到恢复的完整过程。",
            "- 全场景使用相同 Agent 目标：p99 ≤5ms、后台 EPS 保留至少 80%、完成率 97%–105%、零请求错误。目标未按场景修改，历史获胜配置不参与判断。",
            "- EPS 保留率按同轮、同场景默认配置计算后取均值。它用于外部对照，和 Agent 调整前的短期现场基线并不完全相同。不同负载的 EPS 单位不同，不能跨场景比较绝对大小。",
            "- 下表中的业务通过率要求每个窗口同时满足 p99、完成率、错误和相对同轮默认的 EPS 约束；属于更完整的外部验收，不等同于 Agent 内部单次接受动作。", "",
            "## 完整窗口 p99（ms，均值 ± SD）", "",
            "| 场景 | 默认 | 1ms 抢占 | 500μs 抢占 | 10ms 无抢占 | Agent |", "|---|---:|---:|---:|---:|---:|"]
    for sid in study["scenarios"]:
        text.append("| " + " | ".join([labels[sid]]+[cell(by.get((sid,p)), "p99_ms") for p in POLICIES]) + " |")
    text += ["", "## 后台 EPS 保留率（%，均值 ± SD）", "", "| 场景 | 默认 | 1ms 抢占 | 500μs 抢占 | 10ms 无抢占 | Agent |", "|---|---:|---:|---:|---:|---:|"]
    for sid in study["scenarios"]:
        text.append("| " + " | ".join([labels[sid]]+[cell(by.get((sid,p)), "eps_retention_pct", 1) for p in POLICIES]) + " |")
    text += ["", "## Agent 的对照结果", "", "| 场景 | p99 改善率 | EPS 保留率 | 业务达标窗口 | 接受动作 | 回退动作 |", "|---|---:|---:|---:|---:|---:|"]
    for sid in study["scenarios"]:
        r=by.get((sid,"agent"))
        if r:
            text.append(f"| {r['scenario']} | {fmt(r['p99_improvement_pct'],1)} | {fmt(r['eps_retention_pct'],1)} | {r['business_pass_windows']}/{r['business_windows']} | {r['agent_accepts']} | {r['agent_rollbacks']} |")
    text += ["", "p99 改善率为正表示低于同轮默认，为负表示更高。窗口达标率不足、探索期间延迟上升、吞吐损失和重复间波动均保留，不据此改写原始数据或调整阈值。", "",
             "## 原始数据审计", "", f"{json.dumps(data['audit'], ensure_ascii=False)}", "",
             "审计独立重算了 HDR 桶分位数、请求实际完成速率和后台重叠加权 EPS，核对了固定配置的内核回读、同块 PID 和一致全局目标。每个原始试运行 JSON 保留完整窗口及策略时间线。"]
    if dynamics:
        text += ["", "## 独立动态与反馈保护实验", "", "| 实验 | 成功/重复次数 | 说明 |", "|---|---:|---|"]
        for key, name, description in (("independence", "场景不变，外部压力变化", "场景、负载版本和业务 PID 不变，观察策略变化与压力结束后的恢复"),
                                       ("feedback", "业务反馈丢失", "暂停本人测试的 wrk2，保留外部 CPU 压力，检查过期保护恢复默认并随后恢复进程")):
            cases=dynamics.get(key,[])
            text.append(f"| {name} | {sum(c['status']=='passed' for c in cases)}/{len(cases)} | {description} |")
    text += ["", "## 可用于答辩的表述", "",
             "“实验显示，Agent 能依据实时运行数据提出候选、检查后续反馈并执行保留或回退。各场景的性能改善、业务达标率和吞吐代价见完整对照表。当前机制为人工阈值驱动的启发式反馈控制，实验支持其条件性有效，不能据此声称始终优于所有固定配置。”", "",
             "## 文件与复现", "", "- `summary.csv`：汇总均值、SD、同轮改善率及约束通过数。",
             "- `repeats.csv`：每轮每配置数据、顺序、原始文件名。",
             "- `analysis.json` 与 `audit.json`：统计模型及独立审计结果。",
             "- `figures/`：300 DPI PNG 与可缩放 SVG。",
             "- 原始 `study.json`、`block-*.json`、动态实验 JSON 与脚本 SHA256 随数据包提供。", "",
             "复现：在同一演示虚拟机运行 `taskset -c 2 python3 -u experiments/run_live_agent_study.py --rounds 3 --windows 2 --output state/new-study-name`，输出目录需不存在。该命令会操作当前演示负载。"]
    (output/"实验报告.md").write_text("\n".join(text).replace("三轮",f"{study['rounds']}轮")+"\n",encoding="utf-8")


def figures(data, source, dynamics_dir, output, plt, np):
    active_policies=data["study"]["policies"]
    active_short=[SHORT[POLICIES.index(p)] for p in active_policies]
    active_colors=[COLORS[POLICIES.index(p)] for p in active_policies]
    weighted=data["study"].get("variant")=="weighted_v2"
    by = {(r["scenario_id"],r["condition"]):r for r in data["rows"]}
    scenes=data["study"]["scenarios"]
    labels={r["scenario_id"]:r["scenario"] for r in data["rows"]}
    dest=output/"figures";dest.mkdir(exist_ok=True)
    footer="每轮完整窗口先取中位数，图示重复轮次的均值 ± 样本 SD。散点为各轮结果，误差线不是置信区间。"
    def finish(fig,name):
        fig.savefig(dest/(name+".png"),dpi=300,facecolor="white")
        fig.savefig(dest/(name+".svg"),facecolor="white")
        plt.close(fig)
    for metric,name,title in (("p99_ms","01_完整延迟对照","完整窗口 p99 对照，纵轴为对数刻度"),
                              ("eps_retention_pct","02_吞吐保留对照","后台吞吐相对同轮默认的保留率")):
        fig,axes=plt.subplots(2,4,figsize=(16,8.6))
        for ax,sid in zip(axes.flat,scenes):
            values=[by.get((sid,p),{}).get(metric) for p in active_policies]
            errors=[by.get((sid,p),{}).get(metric+"_sd") or 0 for p in active_policies]
            ax.set_title(labels[sid],fontsize=14,weight="bold",pad=10)
            if all(v is None for v in values):
                ax.text(.5,.5,"不适用",ha="center",va="center",transform=ax.transAxes,color="#718096",fontsize=16)
                ax.set_axis_off();continue
            vals=[v or 0 for v in values]
            ax.bar(range(len(active_policies)),vals,color=active_colors,width=.65,zorder=2,alpha=.9)
            ax.errorbar(range(len(active_policies)),vals,yerr=errors,fmt="none",color="#24354B",capsize=4,lw=1.2,zorder=3)
            for i,p in enumerate(active_policies):
                points=[r for r in data["trial_rows"] if r["scenario_id"]==sid and r["condition"]==p]
                for j,r in enumerate(points):
                    v=r[metric] if metric in r else r.get("eps_retention_pct_vs_paired_default")
                    if v is not None:ax.scatter(i+(j-(len(points)-1)/2)*.1,v,s=16,facecolor="white",edgecolor="#24354B",zorder=4)
            ax.set_xticks(range(len(active_policies)),active_short,fontsize=10)
            if metric=="p99_ms":
                ax.set_yscale("log");ax.set_ylabel("p99 (ms)")
                if not weighted:ax.axhline(5,color="#B7483D",ls="--",lw=1,label="旧版 5ms 目标")
                ax.set_ylim(bottom=max(.1,min(v for v in vals if v>0)*.55))
            else:
                ax.set_ylabel("EPS 保留率 (%)")
                if not weighted:ax.axhline(80,color="#B7483D",ls="--",lw=1)
                ax.axhline(100,color="#ADB5C1",lw=.7);ax.set_ylim(bottom=0)
            ax.grid(axis="y",alpha=.18,zorder=0);ax.spines[["top","right"]].set_visible(False)
        fig.suptitle(title,fontsize=22,weight="bold",y=.99)
        fig.text(.5,.016,footer+("  加权版：延迟60%、吞吐40%，无性能达标线。" if weighted else "  阈值旧版：p99 ≤5ms、EPS ≥80%。"),ha="center",fontsize=10,color="#526177")
        fig.tight_layout(rect=(0,.05,1,.95));finish(fig,name)
    fig,axes=plt.subplots(1,2,figsize=(15,7))
    for ax,key,title in ((axes[0],"p99_improvement_pct","Agent 相对默认的 p99 改善率"),
                         (axes[1],"eps_retention_pct","Agent 的后台吞吐保留率")):
        rows=[by[sid,"agent"] for sid in scenes if (sid,"agent") in by and by[sid,"agent"].get(key) is not None]
        ax.barh(range(len(rows)),[r[key] for r in rows],xerr=[r.get(key+"_sd") or 0 for r in rows],color="#087F8C",capsize=4)
        ax.set_yticks(range(len(rows)),[r["scenario"] for r in rows],fontsize=12)
        ax.invert_yaxis();ax.set_xlabel("百分比 (%)");ax.set_title(title,fontsize=17,weight="bold")
        ax.axvline(0 if key=="p99_improvement_pct" else (100 if weighted else 80),color="#B7483D",ls="--",lw=1)
        ax.grid(axis="x",alpha=.18);ax.spines[["top","right"]].set_visible(False)
    fig.text(.5,.025,"改善率为负表示延迟更高。吞吐按同轮默认对照计算，和 Agent 内部调整前基线分别解释。",ha="center",fontsize=11)
    fig.tight_layout(rect=(0,.07,1,1));finish(fig,"03_Agent收益与代价")
    if not dynamics_dir or not (dynamics_dir/"dynamics.json").exists():return
    independence=sorted(dynamics_dir.glob("independence-*.json"))
    if independence:
        r=json.loads(independence[0].read_text(encoding="utf-8"));start=r["started_monotonic"]
        seen={}
        for s in r["samples"]:
            ob=s["agent"].get("observation")
            if ob:seen[ob["sequence"]]=ob
        points=sorted(seen.values(),key=lambda s:s["now"])
        x=[s["now"]-start for s in points]
        fig,axes=plt.subplots(3,1,figsize=(14,8),sharex=True,height_ratios=[2,2,1])
        stop=r["load_ended_monotonic"]-start
        for ax in axes:ax.axvspan(0,stop,color="#CB8A37",alpha=.08);ax.axvline(stop,color="#CB8A37",ls="--");ax.spines[["top","right"]].set_visible(False);ax.grid(alpha=.15)
        for key,label,color in (("cpu_busy_pct","业务 CPU 繁忙度",COLORS[0]),("wait_pct","服务调度等待",COLORS[-1])):
            axes[0].plot(x,[s.get(key) for s in points],label=label,color=color,lw=2)
        axes[0].set_ylabel("占比 (%)");axes[0].legend(loc="upper right")
        axes[1].plot(x,[s.get("probe_p95_ms") for s in points],label="短探测 p95",color="#087F8C",lw=2)
        axes[1].plot(x,[s.get("hdr_p99_ms") for s in points],label="最近完整 HDR p99",color="#437BC2",lw=1.5)
        axes[1].axhline(5,color="#B7483D",ls="--",label="全局 p99 上限 5ms");axes[1].set_ylabel("延迟 (ms)");axes[1].legend(loc="upper right")
        events=[e for e in r["after"]["agent"]["events"] if e["monotonic"]>=start and e.get("action")=="apply"]
        tx=[0]+[e["monotonic"]-start for e in events]+[x[-1]]
        code={"default":0,"scx-500us":1,"scx-1ms":2,"scx-10ms":3}
        ty=[0]+[code[e["strategy_id"]] for e in events];ty+=[ty[-1]]
        axes[2].step(tx,ty,where="post",color="#087F8C",lw=2.5);axes[2].set_yticks(range(4),["默认","500μs","1ms","10ms"]);axes[2].set_ylim(-.2,3.2);axes[2].set_ylabel("内核配置");axes[2].set_xlabel("距外部压力启动的时间 (s)")
        fig.suptitle("场景保持不变，Agent 根据外部压力自主响应",fontsize=20,weight="bold")
        fig.tight_layout(rect=(0,0,1,.96));finish(fig,"04_独立压力响应时间线")
    feedback=sorted(dynamics_dir.glob("feedback-*.json"))
    if feedback:
        r=json.loads(feedback[0].read_text(encoding="utf-8"))
        if r["status"]!="passed":return
        start=r["paused_at"];points=r["samples"];last=None;xx=[];age=[];policy=[]
        for s in points:
            ob=s.get("observation") or {}
            if ob.get("hdr_end") is not None:last=ob["hdr_end"]
            if s["monotonic"]>=start and last is not None:
                xx.append(s["monotonic"]-start);age.append(s["monotonic"]-last)
                policy.append(0 if s["kernel"]["policy_id"]=="kernel.default" else s["kernel"]["slice_us"]/1000)
        fig,axes=plt.subplots(2,1,figsize=(14,7),sharex=True)
        axes[0].plot(xx,age,color="#437BC2",lw=2,label="最近业务反馈的年龄");axes[0].axhline(30,color="#B7483D",ls="--",label="30s 保护条件");axes[0].set_ylabel("业务反馈年龄 (s)");axes[0].legend()
        axes[1].step(xx,policy,where="post",color="#087F8C",lw=2);axes[1].set_ylabel("配置时间片 (ms)");axes[1].set_xlabel("距暂停 wrk2 的时间 (s)");axes[1].set_yticks([0,.5,1,10],["默认","0.5","1","10"])
        for ax in axes:ax.axvline(r["fallback_at"]-start,color="#CB8A37",ls="--");ax.grid(alpha=.2);ax.spines[["top","right"]].set_visible(False)
        fig.suptitle("保留 CPU 压力，业务反馈过期后恢复默认",fontsize=20,weight="bold")
        fig.tight_layout(rect=(0,0,1,.95));finish(fig,"05_反馈缺失保护时间线")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--analysis",type=Path,required=True);parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True);parser.add_argument("--deps",type=Path,required=True)
    parser.add_argument("--dynamics",type=Path);parser.add_argument("--preview",action="store_true")
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    data=json.loads(args.analysis.read_text(encoding="utf-8"))
    assert args.preview or data["study"]["status"]=="complete"
    sys.path.insert(0,str(args.deps));os.environ["MPLCONFIGDIR"]=str(args.deps.parent/"mplconfig")
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt, font_manager
    import numpy as np
    font=Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():font_manager.fontManager.addfont(str(font))
    plt.rcParams.update({"font.family":"Microsoft YaHei","axes.unicode_minus":False,"svg.fonttype":"none"})
    dynamics=json.loads((args.dynamics/"dynamics.json").read_text(encoding="utf-8")) if args.dynamics and (args.dynamics/"dynamics.json").exists() else None
    write_report(data,dynamics,args.output)
    figures(data,args.source,args.dynamics,args.output,plt,np)
    manifest={str(p.relative_to(args.output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in args.output.rglob("*") if p.is_file() and p.name!="SHA256.json"}
    (args.output/"SHA256.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":"published", "figures":len(list((args.output/"figures").glob("*.png")))},ensure_ascii=False))


if __name__=="__main__":main()
