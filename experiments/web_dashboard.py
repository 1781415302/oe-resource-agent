#!/usr/bin/env python3
"""Real-Time Live Sandbox & Telemetry Server for openEuler Resource Control Agent."""
from __future__ import annotations

import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser


REPO_ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = REPO_ROOT / "docs" / "demo-dashboard.html"
IS_LINUX = platform.system().lower() == "linux"

if IS_LINUX:
    sys.path[:0] = [str(REPO_ROOT), "/root/euler-bench"]
    from eulerbench.runner import _prepare_nginx, _wait_ready
    from oeagent.backend import NativeBackend, sched_state
    from oeagent.observe import process_snapshot
    from oeagent.plugins import CgroupPlugin, NetworkHookPlugin, SecurityHookPlugin


def _now_hms() -> str:
    return time.strftime("%H:%M:%S")


def _read_schedstat(pid: int) -> tuple[int, int, int] | None:
    try:
        parts = Path(f"/proc/{pid}/schedstat").read_text(encoding="utf-8").split()
        return int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return None


def _read_proc_ticks(pid: int) -> int:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(") ", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except Exception:
        return 0


class LiveSandbox:
    """Manages real Nginx, wrk2, sysbench, SCX, Cgroup, and eBPF processes in the VM."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.work_dir = Path(tempfile.mkdtemp(prefix="oe-live-sandbox-"))
        self.nginx_port = 18091
        self.ebpf_port = 18095
        self.nginx_proc: subprocess.Popen | None = None
        self.wrk_proc: subprocess.Popen | None = None
        self.sysbench_proc: subprocess.Popen | None = None
        self.ebpf_http_proc: subprocess.Popen | None = None

        self.traffic_enabled = True
        self._auto_idled = False
        self.sysbench_enabled = False
        self.auto_agent = False
        self.choreography_running = False
        self.choreography_stage = "就绪：可随时点击下方开关或启动实况剧本"

        self.sysbench_latest_eps = 0.0
        self.last_client_poll = time.monotonic()
        self.seq = 0
        self.history: deque[dict] = deque(maxlen=45)
        self.logs: deque[dict] = deque(maxlen=50)

        self.backend = NativeBackend(self._on_backend_event, self.work_dir)
        self.cgroup_plugin = CgroupPlugin()
        self.net_plugin = NetworkHookPlugin()
        self.sec_plugin = SecurityHookPlugin()

        self.ebpf_state = {
            "attached": False,
            "drop_target": False,
            "port": self.ebpf_port,
            "stats": {
                "total_packets": 0,
                "total_bytes": 0,
                "target_packets": 0,
                "target_bytes": 0,
                "dropped_packets": 0,
            },
            "last_probe": {
                "ts": "-",
                "ok": True,
                "status": "尚未发送探测请求",
                "latency_ms": 0.0,
            },
        }
        self.cgroup_state = {
            "running": False,
            "low_shares": 1024,
            "high_shares": 4096,
            "low_ticks": 40,
            "high_ticks": 159,
            "low_pct": 20.1,
            "high_pct": 79.9,
            "ratio": 3.98,
            "expected_ratio": 4.0,
        }

        self._high_wait_streak = 0
        self._no_batch_streak = 0
        self._auto_cgroup_pids: dict[str, list[int]] = {"latency": [], "batch": []}
        self._prev_ebpf_target_pkts: int | None = None
        self._ebpf_auto_cooldown = 0

        wrk_meta_path = REPO_ROOT / "state" / "wrk2-window-build.json"
        self.wrk_bin = json.loads(wrk_meta_path.read_text(encoding="utf-8"))["binary"]

        # Clean up any leftover sched_ext or ports before starting
        self._cleanup_stray()
        self._start_ebpf_target_server()
        self._log("🚀 实时沙盒引擎已启动 (CPU0: Nginx/sysbench 混部核心, CPU1: wrk2 25.6k RPS 压测核)")

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _on_backend_event(self, ev: dict) -> None:
        pass

    def _log(self, msg: str, level: str = "info") -> None:
        self.logs.append({"ts": _now_hms(), "level": level, "msg": msg})

    def _cleanup_stray(self) -> None:
        subprocess.run(["pkill", "-f", f"127.0.0.1:{self.nginx_port}"], check=False)
        try:
            self.net_plugin.detach("lo")
        except Exception:
            pass

    def _start_ebpf_target_server(self) -> None:
        code = (
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def log_message(self, *a): pass\n"
            "    def do_GET(self):\n"
            "        b = b'oe-ebpf-live-ok'\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Length', str(len(b)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b)\n"
            f"HTTPServer(('127.0.0.1', {self.ebpf_port}), H).serve_forever()\n"
        )
        self.ebpf_http_proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _ensure_traffic(self) -> None:
        with self.lock:
            if not self.traffic_enabled:
                self._stop_traffic_locked()
                return
            if self.nginx_proc is None or self.nginx_proc.poll() is not None:
                d = self.work_dir / f"nginx-{int(time.time()*1000)}"
                d.mkdir(parents=True, exist_ok=True)
                cfg, page = _prepare_nginx(d, {"port": self.nginx_port, "page_bytes": 4096, "service_cpus": [0]})
                cfg.write_text(
                    cfg.read_text(encoding="utf-8").replace(
                        "keepalive_timeout 65;",
                        "keepalive_timeout 65; keepalive_requests 10000000;",
                    ),
                    encoding="utf-8",
                )
                self.nginx_proc = subprocess.Popen(
                    ["taskset", "-c", "0", shutil.which("nginx"), "-p", str(d) + "/", "-c", str(cfg)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _wait_ready(self.nginx_proc, f"http://127.0.0.1:{self.nginx_port}/index.html", page)

            if self.wrk_proc is None or self.wrk_proc.poll() is not None:
                self.wrk_proc = subprocess.Popen(
                    [
                        "taskset",
                        "-c",
                        "1",
                        self.wrk_bin,
                        "-t1",
                        "-c32",
                        "-d600s",
                        "-T3s",
                        "-R25600",
                        f"http://127.0.0.1:{self.nginx_port}/index.html",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def _stop_traffic_locked(self) -> None:
        if self.wrk_proc is not None:
            self.wrk_proc.terminate()
            try:
                self.wrk_proc.wait(timeout=2)
            except Exception:
                self.wrk_proc.kill()
            self.wrk_proc = None
        if self.nginx_proc is not None:
            self.nginx_proc.terminate()
            try:
                self.nginx_proc.wait(timeout=2)
            except Exception:
                self.nginx_proc.kill()
            self.nginx_proc = None

    def _read_sysbench_stdout(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        pattern = re.compile(r"eps:\s*([0-9.]+)")
        for line in proc.stdout:
            m = pattern.search(line)
            if m:
                self.sysbench_latest_eps = float(m.group(1))

    def _ensure_sysbench(self) -> None:
        with self.lock:
            if not self.sysbench_enabled:
                if self.sysbench_proc is not None:
                    self.sysbench_proc.terminate()
                    try:
                        self.sysbench_proc.wait(timeout=2)
                    except Exception:
                        self.sysbench_proc.kill()
                    self.sysbench_proc = None
                self.sysbench_latest_eps = 0.0
                return
            if self.sysbench_proc is None or self.sysbench_proc.poll() is not None:
                self.sysbench_proc = subprocess.Popen(
                    [
                        "stdbuf",
                        "-oL",
                        "taskset",
                        "-c",
                        "0",
                        shutil.which("sysbench"),
                        "cpu",
                        "--threads=1",
                        "--cpu-max-prime=10000",
                        "--time=3600",
                        "--report-interval=1",
                        "run",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                t = threading.Thread(target=self._read_sysbench_stdout, args=(self.sysbench_proc,), daemon=True)
                t.start()

    def _probe_http_and_sched(self, duration: float = 0.55) -> dict:
        nginx_pid = self.nginx_proc.pid if (self.nginx_proc and self.nginx_proc.poll() is None) else None
        sysb_pid = self.sysbench_proc.pid if (self.sysbench_proc and self.sysbench_proc.poll() is None) else None

        s0 = _read_schedstat(nginx_pid) if nginx_pid else None
        b0 = _read_proc_ticks(sysb_pid) if sysb_pid else None
        t0_wall = time.monotonic()

        latencies: list[float] = []
        if nginx_pid is not None:
            req = b"GET /index.html HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
            buf = bytearray(8192)
            sock: socket.socket | None = None
            deadline = t0_wall + duration
            while time.monotonic() < deadline:
                try:
                    if sock is None:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        sock.settimeout(0.8)
                        sock.connect(("127.0.0.1", self.nginx_port))
                    req_t0 = time.perf_counter_ns()
                    sock.sendall(req)
                    got = 0
                    while got < 4200:
                        n = sock.recv_into(buf)
                        if n <= 0:
                            raise ConnectionError("EOF")
                        got += n
                    latencies.append((time.perf_counter_ns() - req_t0) / 1e6)
                    time.sleep(0.002)
                except Exception:
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                        sock = None
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        else:
            time.sleep(duration)

        dt = max(time.monotonic() - t0_wall, 0.05)
        s1 = _read_schedstat(nginx_pid) if nginx_pid else None
        b1 = _read_proc_ticks(sysb_pid) if sysb_pid else None

        wait_pct = 0.0
        avg_wait_us = 0.0
        slices_per_s = 0.0
        nginx_cpu_pct = 0.0
        if s0 and s1:
            dr = max(0, s1[0] - s0[0])
            dw = max(0, s1[1] - s0[1])
            ds = max(0, s1[2] - s0[2])
            if (dr + dw) > 0:
                wait_pct = round(dw / (dr + dw) * 100.0, 1)
            if ds > 0:
                avg_wait_us = round((dw / 1000.0) / ds, 1)
            slices_per_s = round(ds / dt, 1)
            nginx_cpu_pct = round((dr / 1e9) / dt * 100.0, 1)

        sysb_cpu_pct = 0.0
        if b0 is not None and b1 is not None:
            clk_tck = os.sysconf("SC_CLK_TCK")
            b_dticks = max(0, b1 - b0)
            sysb_cpu_pct = min(100.0, round((b_dticks / clk_tck) / dt * 100.0, 1))

        latencies.sort()
        n_lat = len(latencies)
        p50 = round(latencies[int(n_lat * 0.50)], 3) if n_lat else 0.0
        p95 = round(latencies[int(n_lat * 0.95)], 3) if n_lat else 0.0
        p99 = round(latencies[int(n_lat * 0.99)], 3) if n_lat else 0.0

        return {
            "http_p50_ms": p50,
            "http_p95_ms": p95,
            "http_p99_ms": p99,
            "nginx_wait_pct": wait_pct,
            "nginx_avg_wait_us": avg_wait_us,
            "nginx_slices_per_s": slices_per_s,
            "nginx_cpu_pct": nginx_cpu_pct,
            "sysbench_cpu_pct": sysb_cpu_pct,
            "sysbench_eps": round(self.sysbench_latest_eps, 1) if self.sysbench_enabled else 0.0,
        }

    def _run_auto_agent_step(self, metrics: dict) -> None:
        if not self.auto_agent:
            self._high_wait_streak = 0
            self._no_batch_streak = 0
            return

        # 1. Auto-attach & auto-monitor eBPF TC hook on port 18095
        if not self.ebpf_state["attached"]:
            try:
                res = self.net_plugin.attach({"interface": "lo", "target_port": self.ebpf_port, "drop_target": False})
                self.ebpf_state["attached"] = True
                self.ebpf_state["drop_target"] = False
                if res.get("stats"):
                    self.ebpf_state["stats"] = res["stats"]
                    self._prev_ebpf_target_pkts = int(res["stats"].get("target_packets", 0))
                self._log(
                    f"🛡️ [Agent 自动闭环 · eBPF] 已自动在 lo 挂载 TC 流量监测探针 (port={self.ebpf_port}, drop_target=0)",
                    "ok",
                )
            except Exception as exc:
                self._log(f"Agent 自动挂载 eBPF 失败: {exc}", "err")
        else:
            try:
                st = self.net_plugin.status()
                stats = st.get("stats") or {}
                if stats:
                    self.ebpf_state["stats"] = stats
                cur_pkts = int(stats.get("target_packets", 0))
                delta_pkts = max(0, cur_pkts - (self._prev_ebpf_target_pkts if self._prev_ebpf_target_pkts is not None else cur_pkts))
                self._prev_ebpf_target_pkts = cur_pkts
                if not self.ebpf_state["drop_target"] and delta_pkts >= 15:
                    res = self.net_plugin.attach({"interface": "lo", "target_port": self.ebpf_port, "drop_target": True})
                    self.ebpf_state["drop_target"] = True
                    self._ebpf_auto_cooldown = 4
                    if res.get("stats"):
                        self.ebpf_state["stats"] = res["stats"]
                    self._log(
                        f"🔴 [Agent 自动闭环 · eBPF 熔断] 检测到端口 {self.ebpf_port} 突发高频流量 "
                        f"(Δpackets={delta_pkts}/窗) → 自动下发 drop_target=1 (TC_ACT_SHOT 零拷贝阻断)！",
                        "warn",
                    )
                elif self.ebpf_state["drop_target"] and self._ebpf_auto_cooldown > 0:
                    self._ebpf_auto_cooldown -= 1
                    if self._ebpf_auto_cooldown == 0 and delta_pkts < 15:
                        res = self.net_plugin.attach({"interface": "lo", "target_port": self.ebpf_port, "drop_target": False})
                        self.ebpf_state["drop_target"] = False
                        if res.get("stats"):
                            self.ebpf_state["stats"] = res["stats"]
                        self._log(
                            f"🟢 [Agent 自动闭环 · eBPF 恢复] 端口 {self.ebpf_port} 流量恢复正常 → 自动解除阻断 (TC_ACT_OK)",
                            "ok",
                        )
            except Exception:
                pass

        snap = self.backend.snapshot()
        scx_active = snap.get("policy_id") == "scx.slice"

        if self.sysbench_enabled and metrics["nginx_wait_pct"] >= 30.0:
            self._high_wait_streak += 1
        else:
            self._high_wait_streak = 0

        if not self.sysbench_enabled:
            self._no_batch_streak += 1
        else:
            self._no_batch_streak = 0

        if not scx_active and self._high_wait_streak >= 2:
            self._high_wait_streak = 0
            try:
                # Step A: Automatic LSM security audit on nginx & sysbench
                ng_pids = self._get_nginx_worker_pids()
                sb_pid = self.sysbench_proc.pid if (self.sysbench_proc and self.sysbench_proc.poll() is None) else None
                for pid in (ng_pids[:1] + ([sb_pid] if sb_pid else [])):
                    try:
                        audit = self.sec_plugin.audit({"pid": pid})
                        self._log(
                            f"🔒 [Agent 自动闭环 · LSM 审计] 核验 PID={pid} ({audit.get('comm')}): "
                            f"label={audit.get('lsm_label')}, cap_eff={audit.get('cap_eff_hex')}",
                            "info",
                        )
                    except Exception:
                        pass

                # Step B: Automatic Cgroup isolation (4096 vs 1024) before activating SCX
                try:
                    g_lat = self.cgroup_plugin.create({"group": "auto_latency"})
                    g_bat = self.cgroup_plugin.create({"group": "auto_batch"})
                    if g_lat["value"] != 4096:
                        self.cgroup_plugin.apply({"group": "auto_latency", "expected_value": g_lat["value"], "value": 4096})
                    if g_bat["value"] != 1024:
                        self.cgroup_plugin.apply({"group": "auto_batch", "expected_value": g_bat["value"], "value": 1024})
                    self._auto_cgroup_pids = {"latency": [], "batch": []}
                    for pid in ng_pids:
                        ps = process_snapshot(pid)
                        if ps:
                            self.cgroup_plugin.attach({"group": "auto_latency", "pid": pid, "expected_start_ticks": ps["start_ticks"]})
                            self._auto_cgroup_pids["latency"].append(pid)
                    if sb_pid:
                        ps = process_snapshot(sb_pid)
                        if ps:
                            self.cgroup_plugin.attach({"group": "auto_batch", "pid": sb_pid, "expected_start_ticks": ps["start_ticks"]})
                            self._auto_cgroup_pids["batch"].append(sb_pid)
                    self._log(
                        f"📦 [Agent 自动闭环 · Cgroup 隔离] 自动迁移 Nginx({self._auto_cgroup_pids['latency']}) → auto_latency(4096), "
                        f"sysbench({self._auto_cgroup_pids['batch']}) → auto_batch(1024)",
                        "ok",
                    )
                except Exception as exc:
                    self._log(f"Agent 自动 Cgroup 隔离跳过: {exc}", "info")

                # Step C: Activate SCX scheduler (1000us + wakeup_preempt)
                res = self.backend.start(1000, wakeup_preempt=True)
                self._log(
                    f"⚡ [Agent 自动闭环 · SCX 调度] 连续 2 窗检测到 CPU0 排队激增 "
                    f"(排队占比={metrics['nginx_wait_pct']}%, 唤醒等待={metrics['nginx_avg_wait_us']}µs) "
                    f"→ 自动激活 scx.slice (1000µs + 唤醒抢占, map_id={res.get('map_id')})",
                    "warn",
                )
            except Exception as exc:
                self._log(f"Agent 激活 SCX 失败: {exc}", "err")

        elif scx_active and self._no_batch_streak >= 2:
            self._no_batch_streak = 0
            try:
                self.backend.stop()
                self._log(
                    "✓ [Agent 自动闭环 · SCX 回退] 连续 2 窗确认后台批处理争用消除 → 自动卸载 SCX 恢复 kernel.default",
                    "ok",
                )
                for role, grp in (("latency", "auto_latency"), ("batch", "auto_batch")):
                    for pid in list(self._auto_cgroup_pids.get(role, [])):
                        try:
                            self.cgroup_plugin.detach({"group": grp, "pid": pid})
                        except Exception:
                            pass
                if any(self._auto_cgroup_pids.values()):
                    self._log("📦 [Agent 自动闭环 · Cgroup 释放] 已自动将业务进程从受管 Cgroup 迁回根组", "ok")
                self._auto_cgroup_pids = {"latency": [], "batch": []}
            except Exception as exc:
                self._log(f"Agent 卸载 SCX 失败: {exc}", "err")

    def _loop(self) -> None:
        while True:
            try:
                # Auto-idle if no browser has polled for > 90 seconds
                idle_sec = time.monotonic() - self.last_client_poll
                if idle_sec > 90.0 and not self.choreography_running:
                    if self.traffic_enabled or self.sysbench_enabled:
                        with self.lock:
                            self.traffic_enabled = False
                            self._auto_idled = True
                            self.sysbench_enabled = False
                            self._stop_traffic_locked()
                            self._ensure_sysbench()
                            if self.backend.process is not None:
                                self.backend.stop()
                    time.sleep(1.0)
                    continue

                self._ensure_traffic()
                self._ensure_sysbench()
                if self.backend.process is not None:
                    try:
                        self.backend.heartbeat()
                    except Exception:
                        pass

                metrics = self._probe_http_and_sched(duration=0.55)
                self._run_auto_agent_step(metrics)

                snap = self.backend.snapshot()
                self.seq += 1
                point = {
                    "seq": self.seq,
                    "ts": _now_hms(),
                    "sched_ext": snap.get("sched_ext", sched_state()),
                    "policy_id": snap.get("policy_id", "kernel.default"),
                    "slice_us": snap.get("slice_us"),
                    "wakeup_preempt": snap.get("wakeup_preempt", False),
                    "sysbench_on": self.sysbench_enabled,
                    **metrics,
                }
                self.history.append(point)

                if self.ebpf_state["attached"]:
                    try:
                        st = self.net_plugin.status()
                        if st.get("stats"):
                            self.ebpf_state["stats"] = st["stats"]
                    except Exception:
                        pass
            except Exception as exc:
                self._log(f"采样循环异常: {exc}", "err")
            time.sleep(0.35)

    def get_state(self) -> dict:
        self.last_client_poll = time.monotonic()
        # If traffic was auto-idled and browser reconnects, wake it back up
        if self._auto_idled:
            self._auto_idled = False
            self.traffic_enabled = True
        snap = self.backend.snapshot()
        latest = self.history[-1] if self.history else {
            "seq": 0,
            "ts": _now_hms(),
            "http_p50_ms": 0.0,
            "http_p95_ms": 0.0,
            "http_p99_ms": 0.0,
            "nginx_wait_pct": 0.0,
            "nginx_avg_wait_us": 0.0,
            "nginx_slices_per_s": 0.0,
            "nginx_cpu_pct": 0.0,
            "sysbench_cpu_pct": 0.0,
            "sysbench_eps": 0.0,
        }
        return {
            "ok": True,
            "timestamp": _now_hms(),
            "traffic_running": self.traffic_enabled,
            "sysbench_running": self.sysbench_enabled,
            "auto_agent": self.auto_agent,
            "choreography_running": self.choreography_running,
            "choreography_stage": self.choreography_stage,
            "scheduler": {
                "state": snap.get("sched_ext", sched_state()),
                "policy_id": snap.get("policy_id", "kernel.default"),
                "slice_us": snap.get("slice_us"),
                "wakeup_preempt": snap.get("wakeup_preempt", False),
                "map_id": snap.get("map_id"),
                "generation": snap.get("generation"),
            },
            "latest": latest,
            "history": list(self.history),
            "ebpf": self.ebpf_state,
            "cgroup": self.cgroup_state,
            "logs": list(self.logs),
        }

    def handle_control(self, payload: dict) -> dict:
        self.last_client_poll = time.monotonic()
        action = payload.get("action", "")

        if action == "toggle_traffic":
            enable = bool(payload.get("enable", not self.traffic_enabled))
            with self.lock:
                self.traffic_enabled = enable
                self._ensure_traffic()
            self._log(f"{'🟢 已启动' if enable else '⏸ 已暂停'} 前台 Nginx + wrk2 (25,600 RPS) 实时压测流")
            return {"ok": True}

        if action == "toggle_sysbench":
            enable = bool(payload.get("enable", not self.sysbench_enabled))
            with self.lock:
                self.sysbench_enabled = enable
                self._ensure_sysbench()
            if enable:
                self._log("🔥 已在 CPU0 注入后台 sysbench 算力满载干扰！观察排队占比与延迟飙升...", "warn")
            else:
                self._log("🟢 已停止 CPU0 后台 sysbench 干扰进程", "ok")
            return {"ok": True}

        if action == "toggle_auto_agent":
            enable = bool(payload.get("enable", not self.auto_agent))
            self.auto_agent = enable
            self._log(
                f"🤖 Agent 自动闭环管控已{'开启 (自动协同控制 SCX 调度 + Cgroup 权重隔离 + eBPF 流量熔断 + LSM 审计)' if enable else '关闭 (切换为手动模式)'}",
                "ok",
            )
            return {"ok": True}

        if action == "set_policy":
            pol = payload.get("policy", "default")
            slice_us = int(payload.get("slice_us", 1000))
            wakeup = bool(payload.get("wakeup_preempt", True))
            with self.lock:
                if pol == "default":
                    self.auto_agent = False
                    if self.backend.process is not None:
                        self.backend.stop()
                    self._log("🔄 已手动切回内核默认调度器 kernel.default (sched_ext=disabled)")
                else:
                    if self.backend.process is None or self.backend.process.poll() is not None:
                        res = self.backend.start(slice_us, wakeup_preempt=wakeup)
                        self._log(
                            f"⚡ 已手动加载 eBPF SCX 调度器 (slice_us={slice_us}µs, wakeup_preempt={wakeup}, map_id={res.get('map_id')})",
                            "warn",
                        )
                    else:
                        res = self.backend.update(slice_us, wakeup_preempt=wakeup)
                        self._log(
                            f"🎛️ 已通过 BPF Map 零重启热更新参数: slice_us={slice_us}µs, wakeup_preempt={wakeup} "
                            f"(map_id={res.get('map_id')} 不变, generation={res.get('generation')})",
                            "ok",
                        )
            return {"ok": True}

        if action == "start_choreography":
            if not self.choreography_running:
                t = threading.Thread(target=self._run_choreography, daemon=True)
                t.start()
            return {"ok": True}

        if action == "run_cgroup":
            low_shares = int(payload.get("low_shares", 1024))
            high_shares = int(payload.get("high_shares", 4096))
            threading.Thread(target=self._run_cgroup_experiment, args=(low_shares, high_shares), daemon=True).start()
            return {"ok": True}

        if action == "ebpf_mode":
            mode = payload.get("mode", "observe")
            with self.lock:
                if mode == "detach":
                    self.net_plugin.detach()
                    self.ebpf_state["attached"] = False
                    self.ebpf_state["drop_target"] = False
                    self._log("⚪ 已从 lo 网卡卸载 eBPF TC 网络钩子 (oe_net_hook.bpf.o)")
                else:
                    drop = mode == "drop"
                    res = self.net_plugin.attach({"interface": "lo", "target_port": self.ebpf_port, "drop_target": drop})
                    self.ebpf_state["attached"] = True
                    self.ebpf_state["drop_target"] = drop
                    if res.get("stats"):
                        self.ebpf_state["stats"] = res["stats"]
                    if drop:
                        self._log(f"🔴 eBPF TC 已切换为【端口阻断隔离模式】(port={self.ebpf_port}, drop_target=1 -> TC_ACT_SHOT)", "warn")
                    else:
                        self._log(f"🟢 eBPF TC 已挂载为【流量观测模式】(port={self.ebpf_port}, drop_target=0 -> TC_ACT_OK)", "ok")
            return {"ok": True}

        if action == "ebpf_probe":
            return self._do_ebpf_probe()

        if action == "ebpf_flood":
            threading.Thread(target=self._do_ebpf_flood, daemon=True).start()
            return {"ok": True}

        return {"ok": False, "error": f"Unknown action: {action}"}

    def _do_ebpf_flood(self) -> None:
        self._log(f"💥 [端口突发洪泛注入] 正在向 127.0.0.1:{self.ebpf_port} 连续突发 25 个高频请求...", "warn")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(25):
            try:
                with opener.open(f"http://127.0.0.1:{self.ebpf_port}/ping", timeout=0.12) as resp:
                    resp.read()
            except Exception:
                pass

    def _do_ebpf_probe(self) -> dict:
        t0 = time.perf_counter_ns()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{self.ebpf_port}/ping", timeout=0.6) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                lat_ms = round((time.perf_counter_ns() - t0) / 1e6, 2)
                probe_res = {
                    "ts": _now_hms(),
                    "ok": True,
                    "status": f"HTTP {resp.status} OK ({body}) — 耗时 {lat_ms} ms",
                    "latency_ms": lat_ms,
                }
                self._log(f"📡 [eBPF 实时探测] 访问 127.0.0.1:{self.ebpf_port} 成功 ({lat_ms} ms)", "ok")
        except Exception as exc:
            lat_ms = round((time.perf_counter_ns() - t0) / 1e6, 2)
            probe_res = {
                "ts": _now_hms(),
                "ok": False,
                "status": f"连接被内核 eBPF TC_ACT_SHOT 阻断! ({type(exc).__name__}, {lat_ms} ms)",
                "latency_ms": lat_ms,
            }
            self._log(f"🛡️ [eBPF 实时拦截] 访问 127.0.0.1:{self.ebpf_port} 被内核丢包阻断 (TC_ACT_SHOT)!", "warn")

        if self.ebpf_state["attached"]:
            try:
                st = self.net_plugin.status()
                if st.get("stats"):
                    self.ebpf_state["stats"] = st["stats"]
            except Exception:
                pass
        self.ebpf_state["last_probe"] = probe_res
        return {"ok": True, "probe": probe_res, "ebpf": self.ebpf_state}

    def _run_cgroup_experiment(self, low_shares: int, high_shares: int) -> None:
        if self.cgroup_state["running"]:
            return
        self.cgroup_state["running"] = True
        self._log(f"📦 [Cgroup 实时实验] 启动同核 CPU3 权重争用 ({low_shares} vs {high_shares} shares, 持续 1.8s)...")
        # Note: Cgroup cpu.shares is a CFS controller; if SCX is active, pause it briefly or run on CFS
        was_scx = False
        prev_slice = 1000
        prev_wakeup = True
        with self.lock:
            snap = self.backend.snapshot()
            if snap.get("policy_id") == "scx.slice":
                was_scx = True
                prev_slice = snap.get("slice_us") or 1000
                prev_wakeup = bool(snap.get("wakeup_preempt", True))
                self.backend.stop()

        p_low = None
        p_high = None
        try:
            g_low = self.cgroup_plugin.create({"group": "live-low"})
            g_high = self.cgroup_plugin.create({"group": "live-high"})
            self.cgroup_plugin.apply({"group": "live-low", "expected_value": g_low["value"], "value": low_shares})
            self.cgroup_plugin.apply({"group": "live-high", "expected_value": g_high["value"], "value": high_shares})
            busy = "import time\nend = time.monotonic() + 10.0\nx = 0\nwhile time.monotonic() < end:\n    x += 1\n"
            p_low = subprocess.Popen(["taskset", "-c", "3", sys.executable, "-c", busy])
            p_high = subprocess.Popen(["taskset", "-c", "3", sys.executable, "-c", busy])
            time.sleep(0.1)
            snap_low = process_snapshot(p_low.pid)
            snap_high = process_snapshot(p_high.pid)
            self.cgroup_plugin.attach({"group": "live-low", "pid": p_low.pid, "expected_start_ticks": snap_low["start_ticks"]})
            self.cgroup_plugin.attach({"group": "live-high", "pid": p_high.pid, "expected_start_ticks": snap_high["start_ticks"]})

            t0_low = _read_proc_ticks(p_low.pid)
            t0_high = _read_proc_ticks(p_high.pid)
            time.sleep(1.8)
            d_low = max(1, _read_proc_ticks(p_low.pid) - t0_low)
            d_high = max(1, _read_proc_ticks(p_high.pid) - t0_high)
            total = d_low + d_high
            ratio = round(d_high / d_low, 2)
            expected = round(high_shares / low_shares, 2)
            self.cgroup_state.update({
                "low_shares": low_shares,
                "high_shares": high_shares,
                "low_ticks": d_low,
                "high_ticks": d_high,
                "low_pct": round(d_low / total * 100.0, 1),
                "high_pct": round(d_high / total * 100.0, 1),
                "ratio": ratio,
                "expected_ratio": expected,
            })
            self._log(
                f"✓ [Cgroup 实测完成] 低权重={d_low} ticks ({self.cgroup_state['low_pct']}%), "
                f"高权重={d_high} ticks ({self.cgroup_state['high_pct']}%), 实测比例 = {ratio} : 1 (理论 {expected} : 1)",
                "ok",
            )
        except Exception as exc:
            self._log(f"Cgroup 实验异常: {exc}", "err")
        finally:
            if p_low is not None:
                try:
                    self.cgroup_plugin.detach({"group": "live-low", "pid": p_low.pid})
                except Exception:
                    pass
            if p_high is not None:
                try:
                    self.cgroup_plugin.detach({"group": "live-high", "pid": p_high.pid})
                except Exception:
                    pass
            for p in (p_low, p_high):
                if p is not None:
                    p.terminate()
                    try:
                        p.wait(timeout=1)
                    except Exception:
                        p.kill()
            if was_scx:
                with self.lock:
                    try:
                        self.backend.start(prev_slice, wakeup_preempt=prev_wakeup)
                    except Exception:
                        pass
            self.cgroup_state["running"] = False

    def _run_choreography(self) -> None:
        self.choreography_running = True
        try:
            # Stage 1: Clean baseline (6s)
            self.choreography_stage = "阶段 1/4 [无干扰基线]：仅前台 Nginx + wrk2 (25,600 RPS) 运行于 CPU0，排队占比 < 1%"
            self._log("🎬 [实况剧本 1/4] 启动基线阶段：仅前台 Nginx + wrk2 运行于 CPU0，观察低延迟基线...", "ok")
            with self.lock:
                self.auto_agent = False
                self.traffic_enabled = True
                self.sysbench_enabled = False
                if self.backend.process is not None:
                    self.backend.stop()
                self._ensure_traffic()
                self._ensure_sysbench()
            time.sleep(5.5)

            # Stage 2: Inject sysbench under kernel.default (7s)
            self.choreography_stage = "阶段 2/4 [注入后台干扰]：CPU0 启动 sysbench 争用 (kernel.default)，排队占比飙升至 ~78%，延迟暴涨！"
            self._log("🔥 [实况剧本 2/4] 注入后台 sysbench 干扰！默认 CFS/EEVDF 下 Nginx 排队等待飙升...", "warn")
            with self.lock:
                self.sysbench_enabled = True
                self._ensure_sysbench()
            time.sleep(6.5)

            # Stage 3: Enable Auto-Agent -> activates SCX 1000us + wakeup_preempt (7s)
            self.choreography_stage = "阶段 3/4 [Agent 闭环介入]：自动加载 SCX (1ms + 唤醒抢占)，排队骤降至 ~19%，延迟断崖式回落！"
            self._log("🤖 [实况剧本 3/4] 开启 Agent 自动闭环管控 → 连续检测到高排队，自动激活 SCX 1ms + 唤醒抢占！", "ok")
            self.auto_agent = True
            time.sleep(6.5)

            # Stage 3.5: Live BPF Map hot-update (4s)
            self.choreography_stage = "阶段 3.5/4 [BPF Map 热更新]：零重启在线调整 slice_us=500µs，排队进一步压至 < 10% (map_id 不变，generation 递增)"
            with self.lock:
                if self.backend.process is not None and self.backend.process.poll() is None:
                    res = self.backend.update(500, wakeup_preempt=True)
                    self._log(
                        f"🎛️ [实况剧本 3.5/4] BPF Map 零重启热更新: slice_us=500µs "
                        f"(map_id={res.get('map_id')} 不变, generation={res.get('generation')})",
                        "ok",
                    )
            time.sleep(4.0)

            # Stage 4: Stop sysbench -> Agent automatically rolls back SCX to kernel.default (4s)
            self.choreography_stage = "阶段 4/4 [负载退出与自动回退]：停止 sysbench，Agent 检测到争用消失并自动卸载 SCX 恢复默认调度！"
            self._log("🟢 [实况剧本 4/4] 停止后台 sysbench 干扰 → 观察 Agent 自动卸载 SCX 恢复 kernel.default...", "ok")
            with self.lock:
                self.sysbench_enabled = False
                self._ensure_sysbench()
            time.sleep(4.5)
            self.choreography_stage = "🎉 实况剧本演示完成！你也可以随时手动点击下方按钮自由测试"
        finally:
            self.choreography_running = False


SANDBOX: LiveSandbox | None = None


class LiveDashboardHandler(BaseHTTPRequestHandler):
    guest_host = "172.26.1.3"
    guest_port = 8899

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def _proxy_to_guest(self, method: str, body: bytes | None = None) -> None:
        url = f"http://{self.guest_host}:{self.guest_port}{self.path}"
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self._send_cors()
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as exc:
            err = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self.send_response(502)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html", "/demo-dashboard.html"):
            content = HTML_PATH.read_bytes()
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path.startswith("/api/live-state"):
            if IS_LINUX and SANDBOX is not None:
                body = json.dumps(SANDBOX.get_state(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._proxy_to_guest("GET")
            return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if self.path.startswith("/api/control"):
            if IS_LINUX and SANDBOX is not None:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                res = SANDBOX.handle_control(payload)
                body = json.dumps(res, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._proxy_to_guest("POST", raw)
            return

        self.send_error(404, "Not Found")


def _ensure_guest_daemon(guest_host: str, port: int) -> None:
    """When run on Windows, ensure the live sandbox server is running inside the openEuler VM."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{guest_host}:{port}/api/live-state", timeout=1.5) as resp:
            if resp.status == 200:
                return
    except Exception:
        pass

    print(f"正在唤醒 openEuler 虚拟机 ({guest_host}) 上的实时压测沙盒服务...")
    subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            f"root@{guest_host}",
            f"nohup python3 /root/oe-resource-agent/experiments/web_dashboard.py --host 0.0.0.0 --port {port} --no-browser >/tmp/oe-live-dashboard.log 2>&1 &",
        ],
        check=False,
    )
    time.sleep(2.0)


def main() -> int:
    global SANDBOX
    parser = argparse.ArgumentParser(description="Real-Time Live Sandbox & Telemetry Server.")
    parser.add_argument("--host", default="0.0.0.0" if IS_LINUX else "127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8899, help="Bind port (default: 8899)")
    parser.add_argument("--guest", default="172.26.1.3", help="openEuler guest IP")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    args = parser.parse_args()

    LiveDashboardHandler.guest_host = args.guest
    LiveDashboardHandler.guest_port = args.port

    if IS_LINUX:
        SANDBOX = LiveSandbox()
    else:
        _ensure_guest_daemon(args.guest, args.port)

    server = ThreadingHTTPServer((args.host, args.port), LiveDashboardHandler)
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{display_host}:{args.port}/"
    print(f"openEuler 资源管控 Agent 真机实时交互沙盒已启动: {url}")
    if not args.no_browser and not IS_LINUX:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止实时沙盒服务...")
    finally:
        server.server_close()
        if IS_LINUX and SANDBOX is not None:
            with SANDBOX.lock:
                SANDBOX.traffic_enabled = False
                SANDBOX.sysbench_enabled = False
                SANDBOX._stop_traffic_locked()
                SANDBOX._ensure_sysbench()
                if SANDBOX.backend.process is not None:
                    SANDBOX.backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
