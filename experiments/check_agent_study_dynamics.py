#!/usr/bin/env python3
"""Repeated independent-pressure and missing-business-feedback experiments."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

from run_live_agent_study import ROOT, compact, control, request, save, set_policy, utc
from oeagent.observe import process_snapshot


def feedback_case(output, index):
    set_policy("default")
    control("set_scenario", scenario="service")
    control("toggle_auto_agent", enable=True)
    stress = None
    paused_identity = None
    result = {"case": "missing_business_feedback", "repeat": index, "started_utc": utc(), "samples": [], "status": "running"}
    try:
        with (output/f"feedback-load-{index}.log").open("w") as stream:
            stress = subprocess.Popen(["taskset", "-c", "0", "sysbench", "cpu", "--threads=4", "--time=90", "run"],
                                      stdout=stream, stderr=subprocess.STDOUT)
            until = time.monotonic()+32
            while time.monotonic() < until:
                snap = request()
                result["samples"].append(compact(snap))
                if snap["state"]["agent"]["phase"] == "accepted" and snap["state"]["agent"]["current_strategy"] != "default":
                    break
                time.sleep(.45)
            else:
                raise RuntimeError("No validated SCX configuration before fault injection")
            pid = snap["state"]["process_ids"]["wrk2"]
            paused_identity = process_snapshot(pid)
            assert paused_identity is not None and paused_identity["comm"] in ("wrk", "wrk2")
            result["paused_process"] = paused_identity
            result["paused_at"] = time.monotonic()
            result["last_hdr_end_before_pause"] = snap["state"]["agent"]["observation"]["hdr_end"]
            os_signal(pid, paused_identity["start_ticks"], signal.SIGSTOP)
            until = time.monotonic()+43
            while time.monotonic() < until:
                snap = request()
                result["samples"].append(compact(snap))
                agent = snap["state"]["agent"]
                action = agent.get("last_action") or {}
                if agent["current_strategy"] == "default" and "完整业务反馈过期" in action.get("reason", ""):
                    result.update(status="passed", fallback_at=snap["captured_monotonic"],
                                  delay_after_pause=snap["captured_monotonic"]-result["paused_at"],
                                  delay_after_last_hdr=snap["captured_monotonic"]-max(
                                      sample["observation"]["hdr_end"] for sample in result["samples"]
                                      if sample["observation"] and sample["observation"].get("hdr_end") is not None),
                                  action=action, final_kernel=snap["kernel_snapshot"])
                    break
                time.sleep(.45)
            else:
                raise RuntimeError("Missing-feedback protection did not restore default")
    except Exception as exc:
        result.update(status="failed", error=str(exc))
    finally:
        if paused_identity:
            os_signal(paused_identity["pid"], paused_identity["start_ticks"], signal.SIGCONT)
        if stress is not None and stress.poll() is None:
            stress.terminate()
            try:
                stress.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stress.kill()
                stress.wait(timeout=3)
        set_policy("default")
        control("set_scenario", scenario="service")
        control("toggle_auto_agent", enable=True)
        result["ended_utc"] = utc()
        save(output/f"feedback-{index}.json", result)
    return {k: v for k, v in result.items() if k not in ("samples", "paused_process")}


def os_signal(pid, start_ticks, sig):
    import os
    current = process_snapshot(pid)
    if current is not None and current["start_ticks"] == start_ticks:
        os.kill(pid, sig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"started_utc": utc(), "status": "running", "independence": [], "feedback": [],
              "source_hashes": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                                for name in ("experiments/check_agent_study_dynamics.py", "experiments/check_live_agent.py",
                                             "oeagent/live_agent.py", "oeagent/live_observe.py")}}
    for index in range(1, args.repeats+1):
        set_policy("default")
        control("set_scenario", scenario="service")
        control("toggle_auto_agent", enable=True)
        proc = subprocess.run([sys.executable, "-u", str(ROOT/"experiments/check_live_agent.py"),
                               "--duration", "32", "--recovery-seconds", "28"], cwd=ROOT,
                              capture_output=True, text=True, timeout=110)
        result = json.loads((ROOT/"state/live-agent-validation.json").read_text())
        result["checker_stdout"], result["checker_stderr"] = proc.stdout, proc.stderr
        save(args.output/f"independence-{index}.json", result)
        report["independence"].append({k: result[k] for k in ("status", "elapsed_seconds", "unchanged_scene", "unchanged_workload_revision", "unchanged_managed_pids") if k in result})
        save(args.output/"dynamics.json", report)
        print(json.dumps({"event": "independence", "repeat": index, **report["independence"][-1]}), flush=True)
    for index in range(1, args.repeats+1):
        report["feedback"].append(feedback_case(args.output, index))
        save(args.output/"dynamics.json", report)
        print(json.dumps({"event": "feedback", "repeat": index, "status": report["feedback"][-1]["status"]}), flush=True)
    report.update(ended_utc=utc(), status="complete")
    save(args.output/"dynamics.json", report)


if __name__ == "__main__":
    main()
