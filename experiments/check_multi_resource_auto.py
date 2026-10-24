#!/usr/bin/env python3
"""End-to-end verification that AgentRuntime automatically controls CPU (SCX), Cgroup, eBPF TC, and LSM audit across dynamic runtime situations."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT), "/root/euler-bench"]

from eulerbench.runner import _prepare_nginx, _wait_ready
from oeagent.backend import sched_state
from oeagent.runtime import AgentRuntime, load_profile

PROFILE_PATH = REPO_ROOT / "profiles" / "nginx-multi-auto.json"
OUT_PATH = REPO_ROOT / "state" / "multi-resource-auto-validation.json"
NGINX_PORT = 18086
MONITOR_PORT = 18095


def _probe_port(port: int, timeout: float = 0.6, path: str = "/ping") -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.perf_counter_ns()
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "ok": resp.status == 200,
                "status_code": resp.status,
                "body_len": len(body),
                "latency_ms": round((time.perf_counter_ns() - t0) / 1e6, 3),
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "latency_ms": round((time.perf_counter_ns() - t0) / 1e6, 3),
        }


def _flood_port(port: int, count: int = 25) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    sent = 0
    for _ in range(count):
        try:
            with opener.open(f"http://127.0.0.1:{port}/ping", timeout=0.15) as resp:
                resp.read()
                sent += 1
        except Exception:
            sent += 1
    return sent


def _read_proc_ticks(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[11]) + int(fields[12])
    except Exception:
        return 0


def run_experiment() -> dict:
    # Stop live dashboard temporarily if it is holding port 18095 or SCX
    dashboard_was_active = (
        subprocess.run(["systemctl", "is-active", "--quiet", "oe-live-dashboard"], check=False).returncode == 0
    )
    if dashboard_was_active:
        subprocess.run(["systemctl", "stop", "oe-live-dashboard"], check=False)
        time.sleep(1.0)

    subprocess.run(["pkill", "-9", "-f", "scx_oe_adaptive"], check=False)
    subprocess.run(["pkill", "-9", "-f", f"127.0.0.1:{MONITOR_PORT}"], check=False)
    time.sleep(0.5)

    work_dir = Path(tempfile.mkdtemp(prefix="oe-multi-auto-"))
    log_dir = work_dir / "agent-logs"
    nginx_dir = work_dir / "nginx"
    nginx_dir.mkdir(parents=True, exist_ok=True)

    nginx_proc = None
    wrk_proc = None
    sysbench_proc = None
    aux_http_proc = None
    agent_thread = None
    runtime = None

    try:
        # 0. Start auxiliary HTTP server on MONITOR_PORT (18095) and Nginx + wrk2 on NGINX_PORT (18086)
        aux_code = (
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def log_message(self, *a): pass\n"
            "    def do_GET(self):\n"
            "        b = b'ok'\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Length', str(len(b)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b)\n"
            f"HTTPServer(('127.0.0.1', {MONITOR_PORT}), H).serve_forever()\n"
        )
        aux_http_proc = subprocess.Popen([sys.executable, "-c", aux_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cfg, page = _prepare_nginx(nginx_dir, {"port": NGINX_PORT, "page_bytes": 4096, "service_cpus": [0]})
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace("keepalive_timeout 65;", "keepalive_timeout 65; keepalive_requests 10000000;"),
            encoding="utf-8",
        )
        nginx_proc = subprocess.Popen(
            ["taskset", "-c", "0", shutil.which("nginx"), "-p", str(nginx_dir) + "/", "-c", str(cfg)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_ready(nginx_proc, f"http://127.0.0.1:{NGINX_PORT}/index.html", page)

        wrk_meta = json.loads((REPO_ROOT / "state" / "wrk2-window-build.json").read_text(encoding="utf-8"))
        wrk_proc = subprocess.Popen(
            ["taskset", "-c", "1", wrk_meta["binary"], "-t1", "-c32", "-d60s", "-T3s", "-R25600", f"http://127.0.0.1:{NGINX_PORT}/index.html"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Start AgentRuntime in background thread with nginx-multi-auto.json
        profile = load_profile(PROFILE_PATH)
        runtime = AgentRuntime(profile, log_dir, dry_run=False, mode="adaptive", emit_changes=False)
        agent_thread = threading.Thread(target=runtime.run, args=(35.0,), daemon=True)
        agent_thread.start()

        stages = {}

        # =====================================================================
        # STAGE 1: Service Only (Only Nginx + wrk2, no batch contention, normal port 18095 traffic)
        # =====================================================================
        print("[Stage 1/4] Service Only: Verifying automatic LSM audit + automatic eBPF observe attach + Cgroup & SCX inactive...")
        time.sleep(2.6)
        st1 = runtime.status()
        probe1 = _probe_port(MONITOR_PORT)
        ebpf_live1 = runtime.auto_plugins.ebpf.status()
        stages["stage1_service_only"] = {
            "description": "仅前台 Nginx 服务运行，无后台批处理争抢，受控端口无异常洪泛",
            "policy_id": st1["policy_id"],
            "sched_ext": st1["sched_ext"],
            "plugins_status": st1["plugins"],
            "ebpf_kernel_status": ebpf_live1,
            "monitor_port_probe": probe1,
            "checks": {
                "cpu_stays_default": st1["policy_id"] == "kernel.default" and st1["sched_ext"] == "disabled",
                "lsm_audited_nginx": st1["plugins"]["lsm_audited_count"] >= 1,
                "cgroup_inactive_when_no_contention": st1["plugins"]["cgroup"]["active"] is False,
                "ebpf_auto_attached_observe": st1["plugins"]["ebpf"]["attached"] is True and st1["plugins"]["ebpf"]["drop_target"] is False,
                "monitor_port_reachable": probe1["ok"] is True,
            },
        }
        print("  -> Stage 1 checks:", stages["stage1_service_only"]["checks"])

        # =====================================================================
        # STAGE 2: CPU Contention Arrives (Inject sysbench on CPU0)
        # =====================================================================
        print("[Stage 2/4] Mixed CPU Contention: Starting sysbench on CPU0 -> Verifying automatic Cgroup isolation (4096:1024) + LSM audit + SCX activation...")
        sysbench_proc = subprocess.Popen(
            ["taskset", "-c", "0", shutil.which("sysbench"), "cpu", "--threads=1", "--cpu-max-prime=10000", "--time=60", "run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for Agent to observe mixed workload (window 1 isolates Cgroup + audits sysbench, window 2 confirms and activates SCX)
        deadline = time.monotonic() + 7.0
        while time.monotonic() < deadline:
            st2 = runtime.status()
            if st2["policy_id"] == "scx.slice" and st2["plugins"]["cgroup"]["active"]:
                break
            time.sleep(0.4)
        st2 = runtime.status()
        lat_cgroup = runtime.auto_plugins.cgroup.inspect({"group": "auto_latency"})
        bat_cgroup = runtime.auto_plugins.cgroup.inspect({"group": "auto_batch"})
        stages["stage2_mixed_contention"] = {
            "description": "注入后台 sysbench 算力争用，Agent 自动审计新进程、自动创建并迁入 4096:1024 Cgroup、自动切入 SCX 1ms+抢占",
            "policy_id": st2["policy_id"],
            "slice_us": st2["slice_us"],
            "wakeup_preempt": st2.get("wakeup_preempt"),
            "sched_ext": st2["sched_ext"],
            "plugins_status": st2["plugins"],
            "kernel_cgroup_inspect": {
                "auto_latency": lat_cgroup,
                "auto_batch": bat_cgroup,
            },
            "checks": {
                "cpu_switched_to_scx": st2["policy_id"] == "scx.slice" and st2["sched_ext"] == "enabled" and st2["slice_us"] == 1000,
                "lsm_audited_both_roles": st2["plugins"]["lsm_audited_count"] >= 2,
                "cgroup_auto_isolated": st2["plugins"]["cgroup"]["active"] is True,
                "cgroup_weights_configured": lat_cgroup["value"] == 4096 and bat_cgroup["value"] == 1024,
                "cgroup_procs_verified_under_scx": len(lat_cgroup["procs"]) >= 1 and sysbench_proc.pid in bat_cgroup["procs"],
            },
        }
        print("  -> Stage 2 checks:", stages["stage2_mixed_contention"]["checks"])

        # =====================================================================
        # STAGE 3: Network Anomaly Flood on Monitored Port 18095 & Cooldown Recovery
        # =====================================================================
        print("[Stage 3/4] Network Port Flood Anomaly: Flooding port 18095 -> Verifying automatic eBPF TC_ACT_SHOT isolation & automatic recovery after cooldown...")
        _flood_port(MONITOR_PORT, count=28)
        # Wait for next 1s observation window to detect packet rate >= 20 pps and flip drop_target=True
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            st3 = runtime.status()
            if st3["plugins"]["ebpf"]["drop_target"]:
                break
            time.sleep(0.25)
        st3_isolated = runtime.status()
        ebpf_before_probe = runtime.auto_plugins.ebpf.status()
        blocked_probe = _probe_port(MONITOR_PORT, timeout=0.5)
        nginx_still_ok = _probe_port(NGINX_PORT, timeout=0.5, path="/index.html")
        ebpf_after_probe = runtime.auto_plugins.ebpf.status()

        # Now stop flooding and wait for drop_hold_windows (2 windows ~ 2.5s) to verify automatic recovery
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            st3_rec = runtime.status()
            if not st3_rec["plugins"]["ebpf"]["drop_target"]:
                break
            time.sleep(0.35)
        st3_recovered = runtime.status()
        recovered_probe = _probe_port(MONITOR_PORT, timeout=0.6)

        stages["stage3_ebpf_anomaly_and_recovery"] = {
            "description": "受控端口 18095 遭遇高频请求冲击，Agent 自动触发 eBPF TC_ACT_SHOT 丢包熔断，冲击停止冷却后自动恢复放行",
            "when_isolated": {
                "ebpf_plugin_status": st3_isolated["plugins"]["ebpf"],
                "ebpf_kernel_stats_before_probe": ebpf_before_probe.get("stats"),
                "ebpf_kernel_stats_after_probe": ebpf_after_probe.get("stats"),
                "blocked_port_18095_probe": blocked_probe,
                "foreground_nginx_18086_healthy": nginx_still_ok,
            },
            "after_cooldown_recovery": {
                "ebpf_plugin_status": st3_recovered["plugins"]["ebpf"],
                "recovered_port_18095_probe": recovered_probe,
            },
            "checks": {
                "ebpf_auto_switched_to_drop": st3_isolated["plugins"]["ebpf"]["drop_target"] is True,
                "probe_blocked_by_tc_act_shot": blocked_probe["ok"] is False,
                "foreground_nginx_unharmed": nginx_still_ok["ok"] is True,
                "dropped_packets_incremented": int((ebpf_after_probe.get("stats") or {}).get("dropped_packets", 0)) > 0,
                "ebpf_auto_recovered_after_cooldown": st3_recovered["plugins"]["ebpf"]["drop_target"] is False,
                "probe_restored_after_recovery": recovered_probe["ok"] is True,
            },
        }
        print("  -> Stage 3 checks:", stages["stage3_ebpf_anomaly_and_recovery"]["checks"])

        # =====================================================================
        # STAGE 4: Batch Contention Stops -> Automatic SCX Rollback + Automatic Cgroup Release
        # =====================================================================
        print("[Stage 4/4] Contention Cleared: Stopping sysbench -> Verifying automatic rollback to kernel.default + automatic Cgroup release...")
        sysbench_proc.terminate()
        try:
            sysbench_proc.wait(timeout=2)
        except Exception:
            sysbench_proc.kill()
        sysbench_proc = None

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            st4 = runtime.status()
            if st4["policy_id"] == "kernel.default" and not st4["plugins"]["cgroup"]["active"]:
                break
            time.sleep(0.4)
        st4 = runtime.status()
        lat_cgroup_after = runtime.auto_plugins.cgroup.inspect({"group": "auto_latency"})
        bat_cgroup_after = runtime.auto_plugins.cgroup.inspect({"group": "auto_batch"})

        # Stop AgentRuntime cleanly and verify full cleanup
        runtime.stop_requested = True
        agent_thread.join(timeout=5.0)
        ebpf_after_stop = runtime.auto_plugins.ebpf.status()

        stages["stage4_recovery_and_cleanup"] = {
            "description": "后台批处理退出后，Agent 自动卸载 SCX 恢复 kernel.default，并将 Nginx 自动迁回根 Cgroup，退出时清理 eBPF 钩子",
            "policy_id": st4["policy_id"],
            "sched_ext": st4["sched_ext"],
            "plugins_status": st4["plugins"],
            "kernel_cgroup_after_release": {
                "auto_latency_procs": lat_cgroup_after["procs"],
                "auto_batch_procs": bat_cgroup_after["procs"],
            },
            "ebpf_after_agent_stop": ebpf_after_stop,
            "checks": {
                "cpu_rolled_back_to_default": st4["policy_id"] == "kernel.default" and st4["sched_ext"] == "disabled",
                "cgroup_auto_released": st4["plugins"]["cgroup"]["active"] is False and len(lat_cgroup_after["procs"]) == 0,
                "ebpf_detached_on_shutdown": ebpf_after_stop.get("active") is False,
            },
        }
        print("  -> Stage 4 checks:", stages["stage4_recovery_and_cleanup"]["checks"])

        # =====================================================================
        # STAGE 5: Default Scheduler Cgroup Auto-Isolation Ratio Verification
        # =====================================================================
        print("[Stage 5] Verifying automatic 4096:1024 Cgroup CPU share enforcement under kernel.default on CPU3...")
        # Pin two CPU-bound processes (comm='nginx' and comm='sysbench' or via selector PIDs) on CPU3 under mode='default'
        busy_code = "import time\nend = time.monotonic() + 10.0\nx = 0\nwhile time.monotonic() < end:\n    x += 1\n"
        p_lat = subprocess.Popen(["taskset", "-c", "3", sys.executable, "-c", busy_code])
        p_bat = subprocess.Popen(["taskset", "-c", "3", sys.executable, "-c", busy_code])
        time.sleep(0.15)
        cfs_profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        cfs_profile["selectors"] = [
            {"role": "latency", "pids": [p_lat.pid]},
            {"role": "batch", "pids": [p_bat.pid]},
        ]
        cfs_profile["sample_seconds"] = 0.5
        cfs_log_dir = work_dir / "cfs-agent-logs"
        cfs_runtime = AgentRuntime(cfs_profile, cfs_log_dir, dry_run=False, mode="default", emit_changes=False)
        cfs_thread = threading.Thread(target=cfs_runtime.run, args=(4.0,), daemon=True)
        cfs_thread.start()
        time.sleep(1.2)
        t0_lat = _read_proc_ticks(p_lat.pid)
        t0_bat = _read_proc_ticks(p_bat.pid)
        time.sleep(1.8)
        d_lat = max(1, _read_proc_ticks(p_lat.pid) - t0_lat)
        d_bat = max(1, _read_proc_ticks(p_bat.pid) - t0_bat)
        cfs_runtime.stop_requested = True
        cfs_thread.join(timeout=3.0)
        for p in (p_lat, p_bat):
            p.terminate()
            try:
                p.wait(timeout=1)
            except Exception:
                p.kill()
        ratio = round(d_lat / d_bat, 2)
        lat_share_pct = round(d_lat / (d_lat + d_bat) * 100.0, 1)
        stages["stage5_cfs_cgroup_isolation_effect"] = {
            "description": "在 kernel.default (CFS) 模式下，Agent 自动识别单核争抢并下发 4096:1024 Cgroup 权重，实测前台算力占比提升至 ~80%",
            "latency_shares": 4096,
            "batch_shares": 1024,
            "latency_ticks": d_lat,
            "batch_ticks": d_bat,
            "latency_cpu_share_pct": lat_share_pct,
            "measured_ratio": ratio,
            "expected_ratio": 4.0,
            "checks": {
                "ratio_matches_4_to_1": 3.0 <= ratio <= 5.0,
            },
        }
        print(f"  -> Stage 5 Cgroup CFS ratio: {ratio}:1 ({lat_share_pct}% vs {round(100-lat_share_pct, 1)}%), check:", stages["stage5_cfs_cgroup_isolation_effect"]["checks"])

        # Collect audit trail from events.jsonl
        events_file = log_dir / "events.jsonl"
        events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        plugin_events = [
            {k: v for k, v in e.items() if k not in ("observation",)}
            for e in events
            if e.get("event", "").startswith("plugin_") or e.get("event") in ("policy_applied", "agent_started", "agent_stopped")
        ]

        all_passed = all(
            all(stage["checks"].values())
            for stage in stages.values()
        )
        summary = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "profile": "profiles/nginx-multi-auto.json",
            "all_passed": all_passed,
            "stages": stages,
            "event_timeline": plugin_events,
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\n=== ALL MULTI-RESOURCE AUTO-CONTROL CHECKS PASSED: {all_passed} ===")
        print(f"Saved full validation report to {OUT_PATH}")
        return summary

    finally:
        for p in (sysbench_proc, wrk_proc, nginx_proc, aux_http_proc):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except Exception:
                    p.kill()
        shutil.rmtree(work_dir, ignore_errors=True)
        if dashboard_was_active:
            subprocess.run(["systemctl", "start", "oe-live-dashboard"], check=False)


if __name__ == "__main__":
    res = run_experiment()
    if not res.get("all_passed"):
        sys.exit(1)
