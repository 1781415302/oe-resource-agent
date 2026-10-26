#!/usr/bin/env python3
"""Paired, repeated measurement of the actual dashboard Agent and fixed policies.

No scenario/winner is passed to the live controller. Raw complete windows,
background intervals, process identities and actual policy timelines are saved.
All conditions receive the same bounded HTTP probes. Transition windows remain
in the Agent results, so adaptation costs are not hidden.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oeagent.business import align_background_intervals
from oeagent.demo_tuning import SCENARIOS, STRATEGIES
from oeagent.observe import process_snapshot

POLICIES = [*STRATEGIES, "agent"]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def utc():
    return datetime.now(timezone.utc).isoformat()


def request(body=None):
    path = "/api/experiment/snapshot" if body is None else "/api/control"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:8899" + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=15) as response:
        result = json.load(response)
    if result.get("ok") is False:
        raise RuntimeError(result.get("error"))
    return result


def control(action, **fields):
    return request({"action": action, **fields})


def set_policy(policy):
    spec = STRATEGIES[policy]
    return control("set_policy", policy="default" if policy == "default" else "scx",
                   slice_us=spec["slice_us"], wakeup_preempt=spec["wakeup_preempt"])


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def compact(snapshot):
    state = snapshot["state"]
    return {"monotonic": snapshot["captured_monotonic"], "kernel": snapshot["kernel_snapshot"],
            "policy_revision": snapshot["policy_revision"], "latest": state["latest"],
            "agent_phase": state["agent"]["phase"], "agent_reason": state["agent"]["reason"],
            "observation": state["agent"].get("observation") if state["agent"]["enabled"] else None}


def windows_from(snapshot, start, count, traffic, batch):
    now = snapshot["captured_monotonic"]
    if traffic:
        frontend = sorted((w for w in snapshot["frontend_windows"]
                           if w["start_monotonic"] >= start and w["end_monotonic"] <= now - .8),
                          key=lambda w: w["start_monotonic"])[:count]
    else:
        frontend = [{"start_monotonic": start + i*5, "end_monotonic": start + (i+1)*5,
                     "duration_seconds": 5, "seq": i+1} for i in range(count)
                    if start + (i+1)*5 <= now - .8]
    rows = []
    for front in frontend:
        lo, hi = front["start_monotonic"], front["end_monotonic"]
        aligned = align_background_intervals(snapshot["background_points"], lo, hi) if batch else None
        if batch and aligned["events_per_sec"] is None:
            break
        raw = front.get("raw_record")
        if raw:
            assert hashlib.sha256(raw.encode()).hexdigest() == front["source_sha256"]
            record = json.loads(raw)
            assert not record["final"] and not record["histogram_overflows"]
        rows.append({"start_monotonic": lo, "end_monotonic": hi,
                     "duration_seconds": hi-lo, "frontend": front if traffic else None,
                     "background": aligned,
                     "p99_ms": front.get("p99_ms"), "p95_ms": front.get("p95_ms"),
                     "rps": front.get("rps"), "completion_fraction": front.get("completion_fraction"),
                     "errors": sum(front.get("errors", {}).values()),
                     "eps": aligned["events_per_sec"] if batch else None})
    return rows


def trial(scenario_id, policy, block, position, count, timeout):
    set_policy("default")
    if policy == "agent":
        control("toggle_auto_agent", enable=True)
    elif policy != "default":
        set_policy(policy)
    first = request()
    state = first["state"]
    start = first["captured_monotonic"]
    identities = {k: v for k, v in state["process_ids"].items() if k not in ("scx", "agent")}
    first_cpu = process_snapshot(state["process_ids"]["agent"])
    result = {"scenario_id": scenario_id, "scenario": state["scenario"], "condition": policy,
              "block": block, "position": position, "started_utc": utc(), "started_monotonic": start,
              "accept_after": start+5, "workload_revision": state["workload_revision"],
              "process_ids": identities, "goals": state["agent"]["goals"],
              "samples": [], "windows": [], "quality": "collecting"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = request()
        live = snap["state"]
        if live["workload_revision"] != result["workload_revision"] or live["scenario_id"] != scenario_id:
            raise RuntimeError("Load changed during a trial; attribution invalid")
        if {k: live["process_ids"][k] for k in identities} != identities:
            raise RuntimeError("Measured workload process restarted during trial")
        if not snap["uniform_probes"]:
            raise RuntimeError("Experiment sampling settings changed")
        if policy != "agent":
            spec = STRATEGIES[policy]
            kernel = snap["kernel_snapshot"]
            assert kernel.get("slice_us") == spec["slice_us"]
            assert kernel.get("wakeup_preempt", False) == spec["wakeup_preempt"]
            assert not live["agent"]["enabled"]
        else:
            assert live["agent"]["enabled"] and live["agent"]["decision_source"] == "live_observations_only"
        result["samples"].append(compact(snap))
        result["windows"] = windows_from(snap, start+5, count, live["scenario"]["traffic"], live["scenario"]["batch"])
        if len(result["windows"]) >= count:
            result["quality"] = "complete"
            break
        time.sleep(.45)
    last = request()
    result.update(ended_utc=utc(), ended_monotonic=last["captured_monotonic"],
                  kernel_final=last["kernel_snapshot"],
                  background_raw_points=last["background_points"],
                  agent_events=[event for event in last["state"]["agent"]["events"] if event["monotonic"] >= start],
                  final_agent_phase=last["state"]["agent"]["phase"] if policy == "agent" else "disabled")
    if result["quality"] != "complete":
        result["quality"] = "incomplete"
        result["quality_reason"] = "Missing full attributable windows or valid background alignment; no imputation"
    second_cpu = process_snapshot(state["process_ids"]["agent"])
    if first_cpu and second_cpu and first_cpu["start_ticks"] == second_cpu["start_ticks"]:
        result["controller_cpu_pct"] = (second_cpu["cpu_ticks"]-first_cpu["cpu_ticks"]) / os.sysconf("SC_CLK_TCK") / (last["captured_monotonic"]-start)*100
    result["summary"] = {key: statistics.median(w[key] for w in result["windows"] if w[key] is not None)
                         if any(w[key] is not None for w in result["windows"]) else None
                         for key in ("p99_ms", "p95_ms", "rps", "completion_fraction", "eps")}
    result["summary"]["errors"] = sum(w["errors"] for w in result["windows"])
    counts = Counter((s["kernel"]["policy_id"], s["kernel"].get("slice_us")) for s in result["samples"])
    result["observed_policy_counts"] = [{"policy_id": key[0], "slice_us": key[1], "samples": value} for key, value in counts.items()]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--windows", type=int, default=2)
    parser.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS), default=list(SCENARIOS))
    parser.add_argument("--policies", nargs="+", choices=POLICIES, default=POLICIES)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=75)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    initial = request()
    original = initial["state"]
    assert not original["tuning"]["running"] and not original["cgroup"]["running"]
    report = {"schema_version": 1, "status": "running", "started_utc": utc(), "machine": original["machine"],
              "rounds": args.rounds, "required_windows": args.windows, "scenarios": args.scenarios,
              "policies": args.policies, "trials": [], "source_hashes": {},
              "method": {"warmup_seconds": 5, "window_seconds": 5,
                         "aggregation": "per-trial median of complete windows; paired repeats across independently restarted scenario blocks",
                         "rotation": "cyclic condition order by block and scenario; all conditions start from default",
                         "agent": "actual live_agent.py unchanged; same global goals across scenarios; includes policy transition windows",
                         "measurement": "same maximum 5 HTTP probes per sampling cycle under every condition",
                         "background": "clipped overlap of timestamped stdout reports; >=80% coverage and <2s delivery spread",
                         "processes": "same workload PIDs across conditions in a block; freshly restarted between blocks",
                         "scope": "single VMware guest; descriptive repeats, not general optimality or significance"}}
    for name in ("experiments/run_live_agent_study.py", "experiments/web_dashboard.py", "experiments/demo_load_worker.py",
                 "oeagent/live_agent.py", "oeagent/live_observe.py", "oeagent/demo_control.py", "oeagent/demo_tuning.py"):
        report["source_hashes"][name] = hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    report["builds"] = {name: json.loads((ROOT/"state"/name).read_text())
                        for name in ("build.json", "wrk2-window-build.json")}
    report["environment"] = {name: Path(name).read_text() for name in ("/proc/cpuinfo", "/proc/meminfo", "/proc/cmdline", "/proc/sys/kernel/random/boot_id")}
    try:
        control("set_experiment_sampling", enable=True)
        control("set_agent_goals", latency_ms=5., background_floor=.8)
        for block in range(1, args.rounds+1):
            scenes = args.scenarios[block-1:] + args.scenarios[:block-1]
            for index, sid in enumerate(scenes):
                set_policy("default")
                control("set_scenario", scenario=sid)
                offset = (block-1+index) % len(args.policies)
                order = args.policies[offset:] + args.policies[:offset]
                for position, policy in enumerate(order, 1):
                    print(json.dumps({"event": "trial_start", "utc": utc(), "block": block, "scenario": sid,
                                      "condition": policy, "completed": len(report["trials"])}, ensure_ascii=False), flush=True)
                    result = trial(sid, policy, block, position, args.windows, args.timeout)
                    name = f"block-{block:02d}-{sid}-{policy}.json"
                    save(args.output/name, result)
                    report["trials"].append({key: result[key] for key in ("scenario_id", "condition", "block", "position", "quality", "summary", "final_agent_phase", "observed_policy_counts") } | {"file": name})
                    save(args.output/"study.json", report)
                    print(json.dumps({"event": "trial_end", **report["trials"][-1]}, ensure_ascii=False), flush=True)
        report["status"] = "complete"
    except BaseException as exc:
        report.update(status="interrupted", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        set_policy("default")
        control("set_scenario", scenario=original["scenario_id"])
        control("set_agent_goals", latency_ms=original["agent"]["goals"]["latency_ms"],
                background_floor=original["agent"]["goals"]["background_floor"])
        control("set_experiment_sampling", enable=False)
        if original["agent"]["enabled"]:
            control("toggle_auto_agent", enable=True)
        report["ended_utc"] = utc()
        report["restored"] = request()["state"]["agent"]
        save(args.output/"study.json", report)
        print(json.dumps({"event": "study_end", "status": report["status"], "trials": len(report["trials"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
