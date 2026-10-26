#!/usr/bin/env python3
"""Exercise independent decisions without changing scene or managed workloads.

Run inside the demo VM. The only extra process belongs to this checker and is
terminated in finally. No scenario/policy command is used to induce a decision.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.request


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path="/api/live-state"):
    with OPENER.open("http://127.0.0.1:8899" + path, timeout=10) as response:
        return json.load(response)


def capture(state):
    return {key: state[key] for key in ("timestamp", "scenario_id", "workload_revision", "process_ids",
            "scheduler", "agent", "sysbench_running", "traffic_running", "sample_age_seconds")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=22)
    parser.add_argument("--recovery-seconds", type=int, default=14)
    args = parser.parse_args()
    output = Path("state/live-agent-validation.json")
    output.parent.mkdir(exist_ok=True)
    report = {"status": "running", "method": "real HTTP API + external CPU load; no scene/policy controls",
              "samples": [], "kernel_evidence": []}
    stress = None
    try:
        before = request()
        assert before["scenario_id"] == "service", "Select service before running this checker"
        assert before["agent"]["enabled"], "Enable autonomous Agent before running this checker"
        assert not before["sysbench_running"], "This check uses external, unmanaged interference"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            before = request()
            sample = before["agent"].get("observation") or {}
            if sample.get("cpu_busy_pct") is not None and before["agent"]["current_strategy"] == "default":
                break
            time.sleep(.5)
        assert before["agent"]["current_strategy"] == "default"
        revision, pids = before["workload_revision"], before["process_ids"]
        report["before"] = capture(before)
        started = time.monotonic()
        report["started_monotonic"] = started
        with Path("state/live-agent-external-load.log").open("w") as log:
            stress = subprocess.Popen(["taskset", "-c", "0", "sysbench", "cpu", "--threads=4",
                                       f"--time={args.duration}", "run"], stdout=log, stderr=subprocess.STDOUT)
            report["external_load_pid"] = stress.pid
            observed_action = False
            recorded_policies = set()
            while time.monotonic() - started < args.duration+2 and stress.poll() is None:
                state = request()
                assert state["scenario_id"] == "service" and state["workload_revision"] == revision
                assert all(state["process_ids"][role] == pids[role] for role in ("nginx", "wrk2", "sysbench"))
                assert not state["sysbench_running"], "External load must not be registered as a scene"
                report["samples"].append(capture(state))
                strategy = state["agent"]["current_strategy"]
                observed_action |= strategy != "default"
                if strategy not in recorded_policies:
                    report["kernel_evidence"].append(request("/api/evidence?view=scheduler"))
                    recorded_policies.add(strategy)
                time.sleep(.65)
            assert observed_action, "Agent did not react to the independently injected CPU pressure"
        stress.wait(timeout=5)
        assert stress.returncode == 0, "External stress command failed"
        report["load_ended_monotonic"] = time.monotonic()
        deadline = time.monotonic() + args.recovery_seconds
        while time.monotonic() < deadline:
            state = request()
            assert state["scenario_id"] == "service" and state["workload_revision"] == revision
            report["samples"].append(capture(state))
            sample = state["agent"].get("observation") or {}
            if state["agent"]["current_strategy"] == "default" and (sample.get("cpu_busy_pct") or 100) < 70:
                break
            time.sleep(.65)
        assert state["agent"]["current_strategy"] == "default", "Agent failed to recover default after pressure ended"
        assert state["agent"]["enabled"]
        assert state["agent"]["decision_source"] == "live_observations_only"
        assert state["agent"]["events"], "Missing decision audit"
        report.update(status="passed", elapsed_seconds=round(time.monotonic()-started, 3),
                      unchanged_scene=True, unchanged_managed_pids=True, unchanged_workload_revision=True,
                      independent_pressure_triggered_policy=True, recovered_default=True,
                      after=capture(state))
        report["kernel_evidence"].append(request("/api/evidence?view=scheduler"))
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        if stress is not None and stress.poll() is None:
            stress.terminate()
            try:
                stress.wait(timeout=3)
            except subprocess.TimeoutExpired:
                stress.kill()
                stress.wait(timeout=3)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items()
                         if key not in ("samples", "kernel_evidence", "before", "after")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
