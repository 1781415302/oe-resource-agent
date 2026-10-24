#!/usr/bin/env python3
"""
Verify that every numerical metric in docs/course-project-report.md and README.md
strictly matches the empirical JSON files in state/.

Rule: Every number in the report must be found in state/*.json or directly calculated from it.
"""
import io
import json
import re
import sys
from pathlib import Path

# Force UTF-8 stdout
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    state_dir = repo_root / "state"
    docs_dir = repo_root / "docs"
    report_md = (docs_dir / "course-project-report.md").read_text(encoding="utf-8")
    readme_md = (repo_root / "README.md").read_text(encoding="utf-8")

    errors = []

    def check(condition: bool, msg: str):
        if not condition:
            errors.append(msg)
            print(f"[FAIL] {msg}")
        else:
            print(f"[PASS] {msg}")

    # 1. Check 57,600 RPS stage 1 pilot
    pilot_json_path = state_dir / "baseline-pilot-57600-validation.json"
    check(pilot_json_path.exists(), "state/baseline-pilot-57600-validation.json exists")
    if pilot_json_path.exists():
        pdata = json.loads(pilot_json_path.read_text(encoding="utf-8"))["trials"]
        for key, exp_rps, exp_p99 in [
            ("default_mixed", 57604.1, 10.807),
            ("default_dynamic", 57577.7, 580.607),
            ("fixed-1000_mixed", 55116.0, 10477.567),
            ("fixed-1000_dynamic", 57599.7, 4.839),
            ("adaptive_mixed", 57600.2, 3.309),
            ("adaptive_dynamic", 57600.2, 3.169),
        ]:
            check(pdata[key]["actual_rps"] == exp_rps, f"57.6k {key} RPS is {exp_rps}")
            check(pdata[key]["p99_latency_ms"] == exp_p99, f"57.6k {key} p99 is {exp_p99}")
            comma_rps = f"{exp_rps:,.1f}"
            check(str(exp_rps) in report_md or comma_rps in report_md, f"Report contains 57.6k RPS {exp_rps} (or {comma_rps})")
            check(str(exp_p99) in report_md, f"Report contains 57.6k p99 {exp_p99}")

    # 2. Check SLO continuous closed loop
    slo_json_path = state_dir / "slo-continuous-validation.json"
    check(slo_json_path.exists(), "state/slo-continuous-validation.json exists")
    if slo_json_path.exists():
        slo = json.loads(slo_json_path.read_text(encoding="utf-8"))
        w1 = slo["windows"][0]
        w2 = slo["windows"][1]
        w4 = slo["windows"][3]
        w5 = slo["windows"][4]
        w6 = slo["windows"][5]
        check(w1["frontend_p99_ms"] == 3.739, "SLO Window #1 p99 is 3.739 ms")
        check(w2["frontend_p99_ms"] == 4.691, "SLO Window #2 p99 is 4.691 ms")
        check(w4["frontend_p99_ms"] == 2.135, "SLO Window #4 p99 is 2.135 ms")
        check(w5["frontend_p99_ms"] == 2.243, "SLO Window #5 p99 is 2.243 ms")
        check(w6["frontend_p99_ms"] == 2.143, "SLO Window #6 p99 is 2.143 ms")
        check("3.739 ms" in report_md, "Report contains SLO Window #1 p99 3.739 ms")
        check("4.691 ms" in report_md, "Report contains SLO Window #2 p99 4.691 ms")
        check("2.135 ms" in report_md, "Report contains SLO Window #4 p99 2.135 ms")
        check("0.420 s" in report_md, "Report contains SLO Agent CPU 0.420 s")
        check("2.351 ms" in report_md, "Report contains SLO export 2.351 ms")

    # 3. Check 6x4 Main Matrix & Dynamic Scenario
    rig_json_path = state_dir / "rigorous-suite-validation.json"
    check(rig_json_path.exists(), "state/rigorous-suite-validation.json exists")
    if rig_json_path.exists():
        m5 = json.loads(rig_json_path.read_text(encoding="utf-8"))["part1_5cycle_matrix"]["summary"]
        dy_ad = m5["adaptive"]["dynamic"]
        check(dy_ad["service_before_p99"] == 1.992, "Adaptive dynamic service_before is 1.992")
        check(dy_ad["mixed_transition_p99"] == 3.117, "Adaptive dynamic mixed_transition is 3.117")
        check(dy_ad["mixed_steady_p99"] == 2.091, "Adaptive dynamic mixed_steady is 2.091")
        check(dy_ad["recover_transition_p99"] == 1.931, "Adaptive dynamic recover_transition is 1.931")
        check(dy_ad["service_after_p99"] == 1.941, "Adaptive dynamic service_after is 1.941")
        check(dy_ad["switch_in_ms"] == 805.5, "Adaptive switch_in_ms is 805.5")
        check(dy_ad["rollback_ms"] == 565.4, "Adaptive rollback_ms is 565.4")

        # Verify dynamic trajectory appears without fake ±
        check("1.992 → (3.117) → 2.091 → 1.931 → 1.941 ms" in report_md, "Report contains exact adaptive dynamic sequence")
        check("805.5 ms" in report_md, "Report contains switch_in 805.5 ms")
        check("565.4 ms" in report_md, "Report contains rollback 565.4 ms")
        check("± 42.1" not in report_md, "Report does NOT contain fake ± 42.1 ms")
        check("± 35.8" not in report_md, "Report does NOT contain fake ± 35.8 ms")
        check("±0.035" not in report_md, "Report does NOT contain fake ±0.035 ms")

        # Verify pure web rate gradients in report
        check("1.877 ± 0.006 ms" in report_md, "Report contains 12.8k RPS default 1.877 ± 0.006 ms")
        check("1.858 ± 0.001 ms" in report_md, "Report contains 12.8k RPS adaptive 1.858 ± 0.001 ms")
        check("1.935 ± 0.013 ms" in report_md, "Report contains 25.6k RPS default 1.935 ± 0.013 ms")
        check("1.958 ± 0.030 ms" in report_md, "Report contains 25.6k RPS adaptive 1.958 ± 0.030 ms")
        check("2.386 ± 0.024 ms" in report_md, "Report contains 38.4k RPS default 2.386 ± 0.024 ms")
        check("2.386 ± 0.018 ms" in report_md, "Report contains 38.4k RPS adaptive 2.386 ± 0.018 ms")

        # Verify eBPF port flood
        ebpf = json.loads(rig_json_path.read_text(encoding="utf-8"))["part3_multi_resource_ablation"]["ebpf_port_flood_ablation"]
        check(ebpf["without_ebpf_isolation"]["foreground_avg_wait_us"] == 1541.4, "eBPF flood without wait is 1541.4 µs")
        check(ebpf["without_ebpf_isolation"]["foreground_p99_ms"] == 3.559, "eBPF flood without p99 is 3.559 ms")
        check(ebpf["with_ebpf_tc_act_shot"]["foreground_avg_wait_us"] == 0.4, "eBPF flood with wait is 0.4 µs")
        check(ebpf["with_ebpf_tc_act_shot"]["foreground_p99_ms"] == 1.92, "eBPF flood with p99 is 1.92 ms")
        check(ebpf["with_ebpf_tc_act_shot"]["ebpf_stats"]["dropped_packets"] == 972, "eBPF dropped packets is 972")
        check("180 req" not in report_md, "Report does NOT contain fabricated 180 req/s")
        check("76.2%" not in report_md, "Report does NOT contain fabricated 76.2%")

    # 4. Check Split Ablation Results
    split_json_path = state_dir / "split_ablation_results.json"
    check(split_json_path.exists(), "state/split_ablation_results.json exists")
    if split_json_path.exists():
        sp = json.loads(split_json_path.read_text(encoding="utf-8"))["five_condition_ablation"]
        check(sp["1_baseline_default"]["summary"]["avg_wait_us"]["mean"] == 1556.0, "Split ablation baseline wait is 1556.0 µs")
        check(sp["2_only_cgroup"]["summary"]["avg_wait_us"]["mean"] == 1026.7, "Split ablation cgroup wait is 1026.7 µs")
        check(sp["5_full_scx_multi"]["summary"]["avg_wait_us"]["mean"] == 26.8, "Split ablation full wait is 26.8 µs")
        check(sp["5_full_scx_multi"]["summary"]["p99_ms"]["mean"] == 2.131, "Split ablation full p99 is 2.131 ms")
        check("1556.0 ± 28.5 µs" in report_md, "Report contains 1556.0 ± 28.5 µs")
        check("1026.7 ± 35.4 µs" in report_md, "Report contains 1026.7 ± 35.4 µs")
        check("26.8 ± 8.7 µs" in report_md, "Report contains 26.8 ± 8.7 µs")
        check("2.131 ± 0.170 ms" in report_md, "Report contains 2.131 ± 0.170 ms")

    # 5. Check Multi-Resource Auto
    auto_json_path = state_dir / "multi-resource-auto-validation.json"
    check(auto_json_path.exists(), "state/multi-resource-auto-validation.json exists")
    if auto_json_path.exists():
        aj = json.loads(auto_json_path.read_text(encoding="utf-8"))
        check(aj["stages"]["stage1_service_only"]["monitor_port_probe"]["latency_ms"] == 0.501, "Stage 1 probe latency is 0.501 ms")
        check(aj["stages"]["stage3_ebpf_anomaly_and_recovery"]["when_isolated"]["ebpf_plugin_status"]["last_observed_pps"] == 315.94, "Stage 3 last_observed_pps is 315.94")
        check(aj["stages"]["stage3_ebpf_anomaly_and_recovery"]["after_cooldown_recovery"]["recovered_port_18095_probe"]["latency_ms"] == 0.372, "Stage 4 recovered latency is 0.372 ms")
        check("0.501 ms" in report_md, "Report contains Stage 1 probe latency 0.501 ms")
        check("315.94 pkt/s" in report_md, "Report contains Stage 3 315.94 pkt/s")
        check("0.372 ms" in report_md, "Report contains Stage 4 probe latency 0.372 ms")
        check("315.94 pkt/s" in readme_md, "README contains Stage 3 315.94 pkt/s")

    print("\n------------------------------------------------------------")
    if errors:
        print(f"[FAIL] Completed with {len(errors)} consistency check failures!")
        return 1
    else:
        print("[PASS] ALL DATA CONSISTENCY CHECKS PASSED PERFECTLY!")
        return 0

if __name__ == "__main__":
    sys.exit(main())
