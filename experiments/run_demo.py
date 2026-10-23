"""One-command end-to-end demonstration of the openEuler Adaptive Resource Control Agent."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]
from oeagent.backend import sched_state
from oeagent.observe import WorkloadObserver, process_snapshot
from oeagent.runtime import AgentRuntime, ResourceLock, load_profile
from oeagent.tools import create_registry


class _SilentHTTP(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"oeagent-demo"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        del args


class _DemoServer(HTTPServer):
    allow_reuse_address = True


def _banner(title):
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78, flush=True)


def main():
    registry = create_registry()
    report = {"schema_version": 1, "status": "running", "sections": {}}

    _banner("1. 系统环境、内核能力与安全 LSM 栈发现")
    caps = registry.call("system.capabilities", {})["data"]
    lsm = registry.call("security.lsm.describe", {})["data"]
    audit = registry.call("security.lsm.audit", {"pid": os.getpid()})["data"]
    print(f"  Agent 版本      : {caps['agent_version']} (字节码校验: {caps['python_cache_mode']})")
    print(f"  运行内核        : {caps['kernel_release']} ({caps['architecture']}), BTF={caps['btf']}, sched_ext={caps['sched_ext']}")
    print(f"  调度等待统计    : schedstats_enabled={caps['schedstats_enabled']}")
    print(f"  已注册插件      : {', '.join(caps['available_plugins'])}")
    print(f"  扩展 Hook 状态  : network={caps['extension_hooks']['network']}, security={caps['extension_hooks']['security']}")
    print(f"  活跃 LSM 模块栈 : {', '.join(lsm['active_lsm_stack'])} (BPF LSM={lsm['bpf_lsm_enabled']})")
    print(f"  当前进程安全审计: PID={audit['pid']} ({audit['comm']}), CapEff={audit['cap_eff_hex']}, CAP_SYS_ADMIN={audit['has_cap_sys_admin']}")
    report["sections"]["capabilities"] = {"system": caps, "lsm": lsm, "process_audit": audit}

    _banner("2. 受管 Cgroup 进程迁入与 1:4 CPU 权重争用隔离演示")
    tag = "demo-" + uuid.uuid4().hex[:6]
    g_low, g_high = tag + "-low", tag + "-high"
    workers = []
    with ResourceLock():
        try:
            c1 = registry.call("resource.cgroup.create", {"group": g_low})["data"]
            c2 = registry.call("resource.cgroup.create", {"group": g_high})["data"]
            registry.call("resource.cgroup.apply", {"group": g_high, "expected_value": c2["value"], "value": 4096})
            code = "import os\nos.sched_setaffinity(0, {0})\nwhile True: pass"
            p_low = subprocess.Popen([sys.executable, "-c", code])
            p_high = subprocess.Popen([sys.executable, "-c", code])
            workers.extend([p_low, p_high])
            time.sleep(0.1)
            s_low, s_high = process_snapshot(p_low.pid), process_snapshot(p_high.pid)
            registry.call("resource.cgroup.attach", {"group": g_low, "pid": p_low.pid, "expected_start_ticks": s_low["start_ticks"]})
            registry.call("resource.cgroup.attach", {"group": g_high, "pid": p_high.pid, "expected_start_ticks": s_high["start_ticks"]})
            t0_l, t0_h = process_snapshot(p_low.pid)["cpu_ticks"], process_snapshot(p_high.pid)["cpu_ticks"]
            time.sleep(2.0)
            t1_l, t1_h = process_snapshot(p_low.pid)["cpu_ticks"], process_snapshot(p_high.pid)["cpu_ticks"]
            dl, dh = max(1, t1_l - t0_l), max(1, t1_h - t0_h)
            ratio = round(dh / dl, 2)
            print(f"  受管组创建      : {g_low} (shares=1024, PID={p_low.pid}) vs {g_high} (shares=4096, PID={p_high.pid})")
            print(f"  同核争用 2.0s   : 低权重组获得 {dl} ticks | 高权重组获得 {dh} ticks")
            print(f"  实测分配比例    : {ratio}:1 (理论目标 4.0:1)")
            registry.call("resource.cgroup.detach", {"group": g_low, "pid": p_low.pid})
            registry.call("resource.cgroup.detach", {"group": g_high, "pid": p_high.pid})
            print("  回退与清理      : 进程已安全迁回根 cgroup，空受管组已移除")
            report["sections"]["cgroup_demo"] = {"low_ticks": dl, "high_ticks": dh, "ratio": ratio}
        finally:
            for w in workers:
                w.terminate()
                w.wait(timeout=5)
            for g in (g_low, g_high):
                path = Path("/sys/fs/cgroup/cpu,cpuacct/oeagent") / g
                if path.is_dir():
                    path.rmdir()

    _banner("3. eBPF TC 网络 Hook 流量观测与端口级隔离演示")
    port = 18095
    srv = _DemoServer(("127.0.0.1", port), _SilentHTTP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with ResourceLock():
        try:
            registry.call("network.ebpf.attach", {"interface": "lo", "target_port": port, "drop_target": False})
            for _ in range(4):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read()
            st_obs = registry.call("network.ebpf.status", {})["data"]["stats"]
            print(f"  [观测模式] 端口 {port} 放行 : 捕获目标包={st_obs['target_packets']}, 字节={st_obs['target_bytes']}, 丢包={st_obs['dropped_packets']}")
            registry.call("network.ebpf.attach", {"interface": "lo", "target_port": port, "drop_target": True})
            blocked = False
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                s.close()
            except OSError:
                blocked = True
            st_drop = registry.call("network.ebpf.status", {})["data"]["stats"]
            print(f"  [隔离模式] 端口 {port} 阻断 : 连接被拦截={blocked}, BPF 丢包计数={st_drop['dropped_packets']}")
            registry.call("network.ebpf.detach", {})
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read()
            print("  [卸载恢复] eBPF TC 过滤器已卸载，端口通信立即恢复正常")
            report["sections"]["ebpf_net_demo"] = {"observe_stats": st_obs, "drop_stats": st_drop, "blocked": blocked}
        finally:
            registry.call("network.ebpf.detach", {})
            srv.shutdown()
            srv.server_close()

    _banner("4. 自适应 sched_ext 调度闭环：负载感知 -> 在线切入 SCX -> 唤醒抢占 -> 自动回退")
    demo_dir = ROOT / "results" / f"demo-{time.time_ns()}"
    profile = load_profile(ROOT / "profiles/nginx-cpu.json")
    profile["sample_seconds"] = 0.5
    profile["policy"]["confirmations"] = 2
    profile["policy"]["residence_seconds"] = 1.5
    procs = []
    with ResourceLock():
        try:
            nginx_sim = subprocess.Popen([sys.executable, "-c", "import os,time; os.sched_setaffinity(0,{0});\nwhile True: time.sleep(0.002)"])
            procs.append(nginx_sim)
            time.sleep(0.15)
            profile["selectors"] = [
                {"role": "latency", "pids": [nginx_sim.pid]},
                {"role": "batch", "comm": ["sysbench"]}
            ]
            runtime = AgentRuntime(profile, demo_dir, emit_changes=False)

            obs1 = runtime.observer.window(0.5)
            dec1 = runtime.router.decide(obs1, runtime.current["slice_us"], runtime.current["policy_id"])
            print(f"  阶段 A [仅前台服务] : 观测到 latency={dec1['latency_tasks']}, batch={dec1['batch_tasks']} -> 决策={dec1['mode']} ({dec1['policy_id']}), 保持默认调度")

            batch_sim = subprocess.Popen([sys.executable, "-c", "import os; os.sched_setaffinity(0,{0});\nwhile True: pass"])
            procs.append(batch_sim)
            runtime.observer.selectors = [
                {"role": "latency", "pids": [nginx_sim.pid]},
                {"role": "batch", "pids": [batch_sim.pid]}
            ]
            time.sleep(0.15)

            applied = None
            for step in range(1, 3):
                obs = runtime.observer.window(0.5)
                lat_task = next((t for t in obs["tasks"] if "latency" in t["roles"]), {})
                dec = runtime.router.decide(obs, runtime.current["slice_us"], runtime.current["policy_id"])
                print(f"  阶段 B [后台加入 #{step}]: 观测到 latency 切换={lat_task.get('voluntary_per_sec', 0):.0f}/s, "
                      f"调度等待占比={lat_task.get('run_wait_fraction') or 0:.3f} -> 决策={dec['action']} ({dec['reason']})")
                if dec["action"] == "apply":
                    applied = runtime.apply(dec["policy_id"], dec["slice_us"], runtime.revision, wakeup_preempt=True)
                    runtime.router.applied(obs["ended_monotonic"])
                    print(f"  >>> 已激活 SCX 后端 : policy={applied['policy_id']}, slice_us={applied['slice_us']}, "
                          f"wakeup_preempt={applied['wakeup_preempt']}, map_id={applied['map_id']}, "
                          f"generation={applied['generation']}, sched_ext={sched_state()}")

            updated = runtime.apply("scx.slice", 2000, runtime.revision, wakeup_preempt=True)
            print(f"  阶段 C [BPF 在线调参]: 不重启后端更新 slice_us=2000 -> map_id={updated['map_id']} (不变), generation={updated['generation']}")

            batch_sim.terminate()
            batch_sim.wait(timeout=5)
            procs.remove(batch_sim)
            time.sleep(2.1)

            rolled_back = None
            for step in range(1, 3):
                obs = runtime.observer.window(0.5)
                dec = runtime.router.decide(obs, runtime.current["slice_us"], runtime.current["policy_id"])
                print(f"  阶段 D [后台退出 #{step}]: 观测到 batch={dec['batch_tasks']} -> 决策={dec['action']} ({dec['reason']})")
                if dec["action"] == "apply":
                    rolled_back = runtime.apply(dec["policy_id"], dec["slice_us"], runtime.revision)
                    runtime.router.applied(obs["ended_monotonic"])
                    print(f"  >>> 已自动回退默认  : policy={rolled_back['policy_id']}, sched_ext={sched_state()}, revision={rolled_back['revision']}")
            report["sections"]["scx_loop_demo"] = {"applied": applied, "updated": updated, "rolled_back": rolled_back}
        finally:
            for p in procs:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
            if runtime.backend:
                runtime.backend.stop()
            runtime.log.close()

    _banner("5. 实测性能收益与控制面开销汇总")
    slo_file = ROOT / "state/slo-continuous-validation.json"
    if slo_file.is_file():
        slo = json.loads(slo_file.read_text(encoding="utf-8"))
        s = slo.get("summary", {})
        print(f"  [阶段二 连续窗口 SLO 闭环] 速率={slo.get('offered_rps')} RPS, 窗口=5000ms, 确认窗口数={slo.get('baseline_confirmations')}")
        print(f"    默认调度基线 p99 : {s.get('baseline_p99_ms')} ms (违反目标 -> 自动触发 1ms SCX + 唤醒抢占探测)")
        print(f"    切入 SCX 后 p99  : {s.get('scx_p99_ms')} ms (反馈状态: {s.get('final_feedback_stage')})")
        print(f"    控制与导出开销   : Agent 控制线程 CPU={slo.get('agent_thread_cpu_seconds')}s, wrk2 窗口导出总耗时={slo.get('footer_export_duration_ns')} ns")
        report["sections"]["slo_continuous_summary"] = slo.get("summary")
    print(f"  最终内核 sched_ext 状态 : {sched_state()}")
    report["status"] = "passed"
    report["final_sched_ext"] = sched_state()
    (ROOT / "state/demo-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n演示全部完成，详细报告已写入 state/demo-report.json。")


if __name__ == "__main__":
    main()
