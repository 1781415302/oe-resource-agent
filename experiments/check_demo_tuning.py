#!/usr/bin/env python3
"""Run the dashboard's real scenario suite and audit its exported evidence.

This is functional/controlled-load validation, not a production workload claim.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.request


def request(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as response:
        result = json.load(response)
    if result.get("ok") is False:
        raise RuntimeError(result.get("error", "API failure"))
    return result


def audit(report):
    windows = 0
    hdr_windows = hot_updates = 0
    for run in report["suite"]["results"]:
        search = run["search"]
        scenario = search["scenario"]
        assert search["stage"] == "complete"
        assert len(search["trials"]) == search["repeats"] * 4
        identities = [t["context"]["process_ids"] for t in search["trials"]]
        assert all(identity == identities[0] for identity in identities)
        for before, after in zip(search["trials"], search["trials"][1:]):
            if "default" not in (before["strategy_id"], after["strategy_id"]):
                a, b = before["kernel_snapshot"], after["kernel_snapshot"]
                assert a["map_id"] == b["map_id"] and a["pid"] == b["pid"]
                assert b["generation"] > a["generation"]
                hot_updates += 1
        for trial in search["trials"]:
            snapshot = trial["kernel_snapshot"]
            expected = {"default": (None, False), "scx-1ms": (1000, True),
                        "scx-500us": (500, True), "scx-10ms": (10000, False)}[trial["strategy_id"]]
            assert (snapshot.get("slice_us"), snapshot.get("wakeup_preempt", False)) == expected
            assert snapshot["sched_ext"] == ("disabled" if trial["strategy_id"] == "default" else "enabled")
            for sample in trial["windows"]:
                assert sample["start_monotonic"] >= trial["accept_after"]
                digest = hashlib.sha256(json.dumps(sample["raw"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                assert digest == sample["source_sha256"]
                if scenario["traffic"]:
                    raw = sample["raw"]["frontend"]
                    record = json.loads(raw)
                    assert hashlib.sha256(raw.encode()).hexdigest() == sample["frontend_sha256"]
                    seconds = (record["end_ns"] - record["start_ns"]) / 1e9
                    assert math.isclose(record["requests"] / seconds, sample["rps"], rel_tol=1e-9)
                    assert sum(row[1] for row in record["bins_us"]) + record["terminal_exclusions"] == record["requests"]
                    # HDR uses rounded rank, not ceil(N*p). Recompute from
                    # exported bins without calling the production summarizer.
                    rank = max(1, int(sum(count for _, count in record["bins_us"]) * .99 + .5))
                    cumulative = 0
                    for value, count in sorted(record["bins_us"]):
                        cumulative += count
                        if cumulative >= rank:
                            assert math.isclose(value / 1000, sample["p99_ms"], rel_tol=1e-9)
                            break
                    hdr_windows += 1
                else:
                    assert sample["rps"] is None and sample["p99_ms"] is None
                if scenario["batch"]:
                    background = sample["raw"]["background"]
                    origin = background["estimated_origin_monotonic"]
                    weighted = covered = 0.0
                    for point in background["raw_points"]:
                        assert point["pid"] == trial["context"]["process_ids"]["sysbench"]
                        lo = max(sample["start_monotonic"], origin + point["previous_elapsed_seconds"])
                        hi = min(sample["end_monotonic"], origin + point["elapsed_seconds"])
                        if hi > lo:
                            covered += hi - lo
                            weighted += (hi - lo) * point["events_per_sec"]
                    assert covered / sample["duration_seconds"] >= .8
                    assert math.isclose(weighted / covered, sample["eps"], rel_tol=1e-9)
                windows += 1
    return {"audited_windows": windows, "hdr_p99_recomputed_windows": hdr_windows,
            "same_map_hot_updates_verified": hot_updates,
            "same_workload_processes_all_periods": True, "background_pid_attribution": True,
            "percentile_convention": "HDR rounded rank max(1, trunc(N*p + 0.5))"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8899")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--scenarios", nargs="+")
    parser.add_argument("--output", default="state/demo-tuning-validation.json")
    args = parser.parse_args()
    output = Path(args.output)
    started = time.monotonic()
    request(args.base, "/api/control", {"action": "stop_tuning"})
    payload = {"action": "start_suite", "repeats": args.repeats, "windows": args.windows}
    if args.scenarios:
        payload["scenarios"] = args.scenarios
    request(args.base, "/api/control", payload)
    previous = None
    while time.monotonic() - started < 45 * 60:
        state = request(args.base, "/api/live-state")
        tuning = state["tuning"]
        search = tuning["search"]
        progress = (state["scenario_id"], search["completed_trials"],
                    search["current"]["strategy_id"] if search["current"] else None)
        if progress != previous:
            print(json.dumps({"elapsed_seconds": round(time.monotonic()-started), "progress": progress,
                              "stage": search["stage"], "reason": search["reason"]}, ensure_ascii=False), flush=True)
            previous = progress
        if not tuning["suite"]["running"]:
            if tuning["suite"]["status"] != "complete":
                raise RuntimeError(tuning["suite"]["reason"])
            exported = request(args.base, "/api/tuning/export")
            checked = audit(exported)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(exported, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            summary = {"status": "passed", "elapsed_seconds": round(time.monotonic()-started),
                       "scenario_count": len(exported["suite"]["results"]), **checked,
                       "repeats": args.repeats, "windows_per_trial": args.windows,
                       "results": [{"scenario": r["search"]["scenario"]["label"],
                                    "decision": r["search"]["decision"]} for r in exported["suite"]["results"]]}
            output.with_name(output.stem + "-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({k: v for k, v in summary.items() if k != "results"}), flush=True)
            return
        time.sleep(2)
    request(args.base, "/api/control", {"action": "stop_tuning"})
    raise TimeoutError("Scenario suite exceeded 45 minutes")


if __name__ == "__main__":
    main()
