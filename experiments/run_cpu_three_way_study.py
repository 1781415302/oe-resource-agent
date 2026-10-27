#!/usr/bin/env python3
"""Four-arm CPU scheduling comparison with an explicit swapped order.

Arms (the course-design comparison):
    A  default        kernel EEVDF, SCX detached
    B  scx-1ms        fixed SCX 1ms slice + wakeup preemption (a good setting)
    D  scx-10ms       fixed SCX 10ms without preemption (a bad setting, the
                      project's own documented failure case: it starves a
                      saturated frontend)
    C  agent          the live weighted Agent, which computes its own slice

B and D are the same mechanism with different parameters, so the pair shows that
"SCX" is not automatically good: the parameters decide.

Only two rounds are run, so a cyclic rotation would not balance the arms. Round 1
runs A->B->D->C and round 2 runs C->D->B->A for every scenario. Two rounds give
descriptive numbers, not a balanced design and not statistical significance.

Everything measured and saved by ``run_weighted_agent_study.trial`` is reused
unchanged: 5s warmup, complete 5s windows, identical bounded HTTP probes, the
same workload PIDs inside a block, and the Agent's own exploration and policy
transitions kept in the measured windows.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from oeagent.demo_tuning import SCENARIOS, STRATEGIES  # noqa: E402
import run_weighted_agent_study as base  # noqa: E402


ARMS = {
    "A": {"policy": "default", "label": "默认 EEVDF"},
    "B": {"policy": "scx-1ms", "label": STRATEGIES["scx-1ms"]["label"]},
    "D": {"policy": "scx-u20000p1", "label": "SCX 20ms + 唤醒抢占（片长选得过长）"},
    "E": {"policy": "scx-u300p1", "label": "SCX 300μs + 唤醒抢占（片长选得过短）"},
    "C": {"policy": "agent", "label": "网页加权 Agent (延迟60/吞吐40，自己算片长)"},
}
ROUND_ORDERS = {1: ["A", "B", "D", "E", "C"], 2: ["C", "E", "D", "B", "A"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--windows", type=int, default=8)
    parser.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS),
                        default=["throughput", "latency"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=75)
    args = parser.parse_args()
    if args.rounds not in (1, 2):
        raise SystemExit("这个对照只为两轮设计；--rounds 只能是 1 或 2")
    args.output.mkdir(parents=True, exist_ok=False)

    initial = base.request()
    original = initial["state"]
    assert not original["tuning"]["running"] and not original["cgroup"]["running"]
    if original["agent"]["enabled"]:
        base.control("toggle_auto_agent", enable=False)

    report = {
        "schema_version": 1,
        "study": "cpu-three-way",
        "status": "running",
        "started_utc": base.utc(),
        "machine": original["machine"],
        "rounds": args.rounds,
        "required_windows": args.windows,
        "scenarios": args.scenarios,
        "arms": ARMS,
        "round_orders": {str(k): v for k, v in ROUND_ORDERS.items() if k <= args.rounds},
        "trials": [],
        "source_hashes": {},
        "method": {
            "design": "two rounds; round 1 A->B->D->C, round 2 C->D->B->A for every scenario",
            "why_not_cyclic": "four arms need four blocks for a cyclic rotation to balance; two blocks would partly cancel",
            "warmup_seconds": 5,
            "window_seconds": 5,
            "windows_per_trial": args.windows,
            "aggregation": "per-trial median of complete windows; the two rounds are reported separately and as a mean, no significance claim",
            "agent": "actual weighted_agent.py, relative weights only, no performance target; exploration and policy transitions stay inside the measured windows",
            "measurement": "same maximum 5 HTTP probes per sampling cycle under every arm",
            "processes": "same workload PIDs across the arms of a block; freshly restarted between blocks",
            "scope": "single VMware guest, descriptive course-design comparison",
        },
    }
    for name in ("experiments/run_cpu_three_way_study.py", "experiments/run_weighted_agent_study.py",
                 "experiments/web_dashboard.py", "experiments/demo_load_worker.py",
                 "oeagent/weighted_agent.py", "oeagent/live_agent.py", "oeagent/live_observe.py",
                 "oeagent/demo_control.py", "oeagent/demo_tuning.py"):
        report["source_hashes"][name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    report["environment"] = {name: Path(name).read_text(encoding="utf-8", errors="replace")
                             for name in ("/proc/cpuinfo", "/proc/meminfo", "/proc/cmdline")}
    try:
        base.control("set_experiment_sampling", enable=True)
        base.control("set_agent_weights", latency=.6, throughput=.4)
        for block in range(1, args.rounds + 1):
            order = ROUND_ORDERS[block]
            for sid in args.scenarios:
                base.set_policy("default")
                base.control("set_scenario", scenario=sid)
                for position, arm in enumerate(order, 1):
                    policy = ARMS[arm]["policy"]
                    print(json.dumps({"event": "trial_start", "utc": base.utc(), "block": block,
                                      "scenario": sid, "arm": arm, "policy": policy,
                                      "position": position,
                                      "completed": len(report["trials"])}, ensure_ascii=False), flush=True)
                    result = base.trial(sid, policy, block, position, args.windows, args.timeout)
                    result["arm"] = arm
                    result["arm_label"] = ARMS[arm]["label"]
                    result["round_order"] = list(order)
                    result["final_kernel"] = {key: result["kernel_final"].get(key)
                                              for key in ("policy_id", "slice_us", "wakeup_preempt")}
                    result["chosen_slice_us"] = result["kernel_final"].get("slice_us")
                    name = f"block-{block:02d}-{sid}-{arm}.json"
                    base.save(args.output / name, result)
                    report["trials"].append({"scenario_id": sid, "arm": arm, "arm_label": ARMS[arm]["label"],
                                             "policy": policy, "block": block, "position": position,
                                             "quality": result["quality"], "summary": result["summary"],
                                             "final_agent_phase": result["final_agent_phase"],
                                             "observed_policy_counts": result["observed_policy_counts"],
                                             "final_kernel": result["final_kernel"],
                                             "chosen_slice_us": result["chosen_slice_us"],
                                             "file": name})
                    base.save(args.output / "study.json", report)
                    print(json.dumps({"event": "trial_end", **report["trials"][-1]}, ensure_ascii=False), flush=True)
        report["status"] = "complete"
    except BaseException as exc:
        report.update(status="interrupted", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        base.set_policy("default")
        base.control("set_scenario", scenario=original["scenario_id"])
        base.control("set_agent_weights", latency=original["agent"]["weights"]["latency"],
                     throughput=original["agent"]["weights"]["throughput"])
        base.control("set_experiment_sampling", enable=False)
        if original["agent"]["enabled"]:
            base.control("toggle_auto_agent", enable=True)
        report["ended_utc"] = base.utc()
        report["restored"] = base.request()["state"]["agent"]
        base.save(args.output / "study.json", report)
        print(json.dumps({"event": "study_end", "status": report["status"],
                          "trials": len(report["trials"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
