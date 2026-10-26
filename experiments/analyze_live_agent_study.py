#!/usr/bin/env python3
"""Independent raw-window audit and descriptive paired experiment tables."""
import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

LABELS = {"default": "默认 EEVDF", "scx-1ms": "SCX 1ms 抢占", "scx-500us": "SCX 500μs 抢占",
          "scx-10ms": "SCX 10ms 无抢占", "agent": "自主 Agent"}


def mean(values):
    valid = [x for x in values if x is not None]
    return statistics.mean(valid) if valid else None


def sd(values):
    valid = [x for x in values if x is not None]
    return statistics.stdev(valid) if len(valid) > 1 else None


def close(actual, expected):
    assert math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-7), (actual, expected)


def external_score(measured, reference, latency=.6, throughput=.4):
    """Independent score against paired default, not the Agent's earlier live reference."""
    terms=[]
    if reference.get("p99_ms") and measured.get("p99_ms") and latency:
        terms.append((latency,math.log(reference["p99_ms"]/measured["p99_ms"])))
    speeds=[]
    for key in ("rps","eps"):
        if reference.get(key) is not None and reference[key]>0:
            if measured.get(key) is None:return None
            speeds.append(math.log(max(measured[key],1e-9)/reference[key]))
    if speeds and throughput:terms.append((throughput,statistics.mean(speeds)))
    if not terms:return None
    return 100*math.exp(sum(w*v for w,v in terms)/sum(w for w,_ in terms))


def quantile(record, fraction):
    count = sum(n for _, n in record["bins_us"])
    rank = max(1, int(count * fraction + .5))
    cumulative = 0
    for value, n in sorted(record["bins_us"]):
        cumulative += n
        if cumulative >= rank:
            return value / 1000
    raise AssertionError("Empty HDR histogram")


def audit_trial(trial):
    audited_http = audited_batch = 0
    lo = trial["accept_after"]
    previous_end = None
    for window in trial["windows"]:
        assert window["start_monotonic"] >= lo
        if previous_end is not None:
            assert window["start_monotonic"] >= previous_end - .01
        previous_end = window["end_monotonic"]
        seconds = window["end_monotonic"]-window["start_monotonic"]
        assert 4.5 <= seconds <= 5.5
        front = window["frontend"]
        if front:
            raw = front["raw_record"]
            assert hashlib.sha256(raw.encode()).hexdigest() == front["source_sha256"]
            record = json.loads(raw)
            assert not record["final"] and record["histogram_overflows"] == 0
            assert sum(n for _, n in record["bins_us"]) + record["terminal_exclusions"] == record["requests"]
            raw_seconds = (record["end_ns"]-record["start_ns"])/1e9
            close(window["rps"], record["requests"]/raw_seconds)
            close(window["completion_fraction"], record["requests"]/raw_seconds/trial["scenario"]["rps"])
            for metric, fraction in (("p99_ms", .99), ("p95_ms", .95)):
                close(window[metric], quantile(record, fraction))
            assert window["errors"] == sum(record["errors"].values())
            audited_http += 1
        if window["background"]:
            aligned = window["background"]
            points = trial["background_raw_points"]
            origin = aligned["estimated_origin_monotonic"]
            assert any(abs(p["received_monotonic"]-p["elapsed_seconds"]-origin) < 1e-8 for p in points)
            previous, weighted, covered = 0., 0., 0.
            prefix_origin, prefix_delay = math.inf, -math.inf
            matched = False
            for point in points:
                lower, upper = origin+previous, origin+point["elapsed_seconds"]
                previous = point["elapsed_seconds"]
                overlap = max(0., min(upper, window["end_monotonic"])-max(lower, window["start_monotonic"]))
                weighted += overlap * point["events_per_sec"]
                covered += overlap
                prefix_origin = min(prefix_origin, point["received_monotonic"]-point["elapsed_seconds"])
                prefix_delay = max(prefix_delay, point["received_monotonic"]-origin-point["elapsed_seconds"])
                # The final HTTP export can include a newly delivered report.
                # Verify the exact prefix used at alignment time, identified by
                # the saved origin, coverage and delivery spread; do not add a
                # later boundary interval to the original measurement.
                if covered > 0 and math.isclose(prefix_origin,origin,abs_tol=1e-8) and math.isclose(
                    min(seconds,covered),aligned["covered_seconds"],rel_tol=1e-8,abs_tol=1e-7) and math.isclose(
                    prefix_delay,aligned["max_report_delivery_delay_seconds"],rel_tol=1e-8,abs_tol=1e-7) and math.isclose(
                    weighted/covered,window["eps"],rel_tol=1e-8,abs_tol=1e-7):
                    matched = True
                    break
            assert matched, "No raw report prefix reproduces the saved alignment"
            assert covered >= seconds * .8
            assert aligned["max_report_delivery_delay_seconds"] < 2
            close(window["eps"], weighted/covered)
            close(aligned["covered_seconds"], min(seconds, covered))
            audited_batch += 1
    for key, value in trial["summary"].items():
        if key == "errors":
            assert value == sum(w["errors"] for w in trial["windows"])
        else:
            values = [w[key] for w in trial["windows"] if w[key] is not None]
            if values:
                close(value, statistics.median(values))
            else:
                assert value is None
    policy = trial["condition"]
    expected = {"default": (None, False), "scx-1ms": (1000, True), "scx-500us": (500, True), "scx-10ms": (10000, False)}
    for sample in trial["samples"]:
        kernel = sample["kernel"]
        if policy != "agent":
            assert (kernel.get("slice_us"), kernel.get("wakeup_preempt", False)) == expected[policy]
            assert kernel["sched_ext"] == ("disabled" if policy == "default" else "enabled")
        if sample["observation"]:
            assert not {"scenario_id", "scenario", "label", "winner", "history"}.intersection(sample["observation"])
    return audited_http, audited_batch


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def analyze(directory):
    study = json.loads((directory/"study.json").read_text(encoding="utf-8"))
    trials = [json.loads((directory/row["file"]).read_text(encoding="utf-8")) | {"file": row["file"]} for row in study["trials"]]
    http = batch = 0
    groups, block_ids = defaultdict(list), {}
    for trial in trials:
        try:
            a, b = audit_trial(trial)
        except AssertionError as exc:
            raise AssertionError(f"{trial['file']}: {exc}") from exc
        http += a
        batch += b
        key = trial["block"], trial["scenario_id"]
        if key in block_ids:
            assert block_ids[key] == trial["process_ids"], "Workload PIDs changed within a paired block"
        block_ids[key] = trial["process_ids"]
        if "weights" in trial:
            assert trial["weights"] == {"latency": .6, "throughput": .4}
        else:
            assert trial["goals"]["latency_ms"] == 5 and trial["goals"]["background_floor"] == .8
        groups[trial["scenario_id"], trial["condition"]].append(trial)
    baselines = {(t["scenario_id"], t["block"]): t for t in trials if t["condition"] == "default"}
    rows, raw_rows = [], []
    for sid in study["scenarios"]:
        for policy in study["policies"]:
            periods = groups[sid, policy]
            if not periods:
                continue
            row = {"scenario_id": sid, "scenario": periods[0]["scenario"]["label"], "condition": policy,
                   "label": LABELS[policy], "repeats": len(periods),
                   "complete_repeats": sum(t["quality"] == "complete" for t in periods),
                   "windows": sum(len(t["windows"]) for t in periods)}
            for metric in ("p99_ms", "p95_ms", "rps", "eps", "completion_fraction"):
                values = [t["summary"][metric] for t in periods]
                row[metric], row[metric+"_sd"] = mean(values), sd(values)
            gains, retentions, scores, p99_pass, business_pass, n_http, n_business = [], [], [], 0, 0, 0, 0
            switches = accepted = rolled_back = 0
            for t in periods:
                baseline = baselines.get((sid, t["block"]))
                base = baseline["summary"] if baseline else {}
                gain = (1-t["summary"]["p99_ms"]/base["p99_ms"])*100 if base.get("p99_ms") and t["summary"]["p99_ms"] is not None else None
                retention = t["summary"]["eps"]/base["eps"]*100 if base.get("eps") and t["summary"]["eps"] is not None else None
                gains.append(gain)
                retentions.append(retention)
                score=external_score(t["summary"],base)
                scores.append(score)
                events = t["agent_events"] if policy == "agent" else []
                switches += sum(e["action"] == "apply" for e in events)
                accepted += sum(e["action"] == "accept" for e in events)
                rolled_back += sum(e.get("kind") == "rollback" for e in events)
                for w in t["windows"]:
                    http_ok = True
                    if w["frontend"]:
                        n_http += 1
                        p99_pass += w["p99_ms"] <= 5
                        http_ok = w["p99_ms"] <= 5 and .97 <= w["completion_fraction"] <= 1.05 and w["errors"] == 0
                    bg_ok = not t["scenario"]["batch"] or (base.get("eps") is not None and w["eps"] is not None and w["eps"] >= base["eps"]*.8)
                    n_business += 1
                    business_pass += bool(http_ok and bg_ok)
                raw_rows.append({"scenario_id": sid, "condition": policy, "block": t["block"], "position": t["position"],
                                 "quality": t["quality"], **t["summary"], "p99_improvement_pct_vs_paired_default": gain,
                                 "eps_retention_pct_vs_paired_default": retention,
                                 "weighted_score_vs_paired_default": score,
                                 "controller_cpu_pct": t.get("controller_cpu_pct"), "final_agent_phase": t["final_agent_phase"],
                                 "file": t["file"]})
            row.update(p99_improvement_pct=mean(gains), p99_improvement_pct_sd=sd(gains),
                       weighted_score=mean(scores),weighted_score_sd=sd(scores),
                       eps_retention_pct=mean(retentions), eps_retention_pct_sd=sd(retentions),
                       errors=sum(t["summary"]["errors"] for t in periods), p99_pass_windows=p99_pass,
                       http_windows=n_http, business_pass_windows=business_pass, business_windows=n_business,
                       controller_cpu_pct=mean([t.get("controller_cpu_pct") for t in periods]),
                       agent_switches=switches, agent_accepts=accepted, agent_rollbacks=rolled_back)
            rows.append(row)
    audit = {"status": "passed", "study_status": study["status"], "trials": len(trials), "http_windows": http,
             "background_windows": batch, "all_raw_p99_rps_eps_recomputed": True,
             "same_workload_pids_within_blocks": True, "identical_global_preferences": True,
             "fixed_configuration_kernel_readbacks": True, "decision_inputs_exclude_scene_and_history": True}
    return {"study": study, "rows": rows, "trial_rows": raw_rows, "audit": audit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    data = analyze(args.source)
    (args.output/"analysis.json").write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (args.output/"audit.json").write_text(json.dumps(data["audit"], ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    write_csv(args.output/"summary.csv", data["rows"])
    write_csv(args.output/"repeats.csv", data["trial_rows"])
    print(json.dumps(data["audit"], ensure_ascii=False))


if __name__ == "__main__":
    main()
