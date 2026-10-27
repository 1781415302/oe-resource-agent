#!/usr/bin/env python3
"""Real-Time Live Sandbox & Telemetry Server for openEuler Resource Control Agent."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
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
from urllib.parse import parse_qs, urlsplit
import webbrowser


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from oeagent.demo_tuning import SCENARIOS, STRATEGIES, SLICE_MAX_US, SLICE_MIN_US, scx_strategy_id, strategy_spec
from oeagent.demo_control import DemoControl
from oeagent.weighted_agent import AgentWeights, WeightedAgent
from oeagent.live_observe import LiveSampler
HTML_PATH = REPO_ROOT / "docs" / "demo-dashboard.html"
IS_LINUX = platform.system().lower() == "linux"

if IS_LINUX:
    sys.path[:0] = [str(REPO_ROOT), "/root/euler-bench"]
    from eulerbench.runner import _prepare_nginx, _wait_ready
    from oeagent.backend import NativeBackend, sched_state
    from oeagent.observe import process_snapshot
    from oeagent.plugins import CgroupPlugin, NetworkHookPlugin, SecurityHookPlugin
    from oeagent import demo_evidence


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


class LiveSandbox(DemoControl):
    """Manages real Nginx, wrk2, sysbench, SCX, Cgroup, and eBPF processes in the VM."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.repo_root = REPO_ROOT
        self.work_dir = Path(tempfile.mkdtemp(prefix="oe-live-sandbox-"))
        self.nginx_port = 18091
        self.ebpf_port = 18095
        self.nginx_proc: subprocess.Popen | None = None
        self.wrk_proc: subprocess.Popen | None = None
        self.sysbench_proc: subprocess.Popen | None = None
        self.ebpf_http_proc: subprocess.Popen | None = None
        self.compute_proc = None
        self.burst_proc = None
        self.auxiliary_logs = {}
        self._idle_flags = None
        self.scenario_id = "service"
        self.scenario = deepcopy(SCENARIOS[self.scenario_id])
        self.offered_rps = self.scenario["rps"]
        self.batch_threads = self.scenario["threads"]
        self.batch_kind = "sysbench"
        self.request_path = "/index.html"
        self.workload_revision = 0
        self.policy_revision = 0
        self.policy_since = time.monotonic()
        self.scenario_since = time.monotonic()
        self.control_mode = "autonomous"
        self.experiment_sampling = False
        self.live_agent = WeightedAgent()
        self.live_sampler = LiveSampler()
        self.network_auto_requested = False
        self.background_points = deque(maxlen=2400)
        self.front_windows = deque(maxlen=240)
        self.tuning = None
        self.tuning_running = False
        self.tuning_run_id = None
        self.tuning_started_utc = None
        self.tuning_seen = set()
        self.tuning_last_batch_end = None
        self.last_decision = None
        self.monitor_since = None
        self.monitor_bad_streak = 0
        self.next_search_after = None
        self.auto_search_options = {"repeats": 1, "windows": 3}
        self.suite = {"running": False, "results": [], "remaining": [], "reason": "尚未运行本机典型场景测试"}
        self.saved_suite = None
        suite_path = REPO_ROOT / "state/demo-suite-latest.json"
        if suite_path.is_file():
            try:
                self.saved_suite = json.loads(suite_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

        self.traffic_enabled = True
        self._auto_idled = False
        self.sysbench_enabled = False
        self.auto_agent = True
        self.choreography_running = False
        self.choreography_stage = "就绪：可随时点击下方开关或启动实况剧本"

        self.sysbench_latest_eps = None
        self.sysbench_last_report = None
        self.last_client_poll = time.monotonic()
        self.seq = 0
        self.history: deque[dict] = deque(maxlen=180)
        self.scene_events: deque[dict] = deque(maxlen=32)
        self.logs: deque[dict] = deque(maxlen=50)
        self.machine = demo_evidence.identity(REPO_ROOT, self.work_dir)
        self.wrk_window = None
        self.wrk_phase = "calibrating"
        self._evidence_cache = {}

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
            "measured": False,
            "measured_utc": None,
            "low_ticks": None,
            "high_ticks": None,
            "low_pct": None,
            "high_pct": None,
            "ratio": None,
            "expected_ratio": 4.0,
        }

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
        with (self.work_dir / "native-events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"utc": demo_evidence.utc_now(), **ev}, ensure_ascii=False) + "\n")
        self._log("[原生后端回读] " + json.dumps(ev, ensure_ascii=False), "info")

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
                if self.scenario.get("compute"):
                    self.compute_proc = self._start_auxiliary("compute", "0")
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            with socket.create_connection(("127.0.0.1", 18093), timeout=.2):
                                break
                        except OSError:
                            time.sleep(.05)
                    else:
                        raise RuntimeError("请求内计算服务未就绪")
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
                if self.scenario.get("compute"):
                    cfg.write_text(cfg.read_text(encoding="utf-8").replace(
                        "location = /index.html { }", "location = /index.html { }\n"
                        "    location = /compute { proxy_pass http://127.0.0.1:18093; "
                        "proxy_http_version 1.1; proxy_set_header Connection \"\"; }"), encoding="utf-8")
                self.nginx_proc = subprocess.Popen(
                    ["taskset", "-c", "0", shutil.which("nginx"), "-p", str(d) + "/", "-c", str(cfg)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _wait_ready(self.nginx_proc, f"http://127.0.0.1:{self.nginx_port}/index.html", page)

            if self.wrk_proc is None or self.wrk_proc.poll() is not None:
                self.wrk_window = None
                self.wrk_phase = "calibrating"
                environment = {**os.environ, "OE_WRK2_WINDOW_MS": "5000"}
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
                        f"-R{self.offered_rps}",
                        f"http://127.0.0.1:{self.nginx_port}{self.request_path}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    env=environment,
                )
                threading.Thread(target=self._read_wrk_stdout, args=(self.wrk_proc,), daemon=True).start()
            if self.scenario.get("burst") and (self.burst_proc is None or self.burst_proc.poll() is not None):
                self.burst_proc = self._start_auxiliary("burst", "3")

    def _start_auxiliary(self, mode, cpu):
        path = self.work_dir / f"{mode}-{time.time_ns()}.log"
        self.auxiliary_logs[mode] = str(path)
        with path.open("w", encoding="utf-8") as stream:
            return subprocess.Popen(["taskset", "-c", cpu, sys.executable,
                                     str(REPO_ROOT / "experiments/demo_load_worker.py"), mode],
                                    stdout=stream, stderr=subprocess.STDOUT)

    @staticmethod
    def _terminate(proc):
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    def _read_wrk_stdout(self, proc) -> None:
        with (self.work_dir / f"wrk2-{proc.pid}.log").open("a", encoding="utf-8") as stream:
            for line in proc.stdout:
                stream.write(line)
                stream.flush()
                if self.wrk_proc is not proc or not line.startswith("OE_WRK2_STREAM "):
                    continue
                try:
                    raw = line.split(" ", 1)[1].strip()
                    record = json.loads(raw)
                    with self.lock:
                        if self.wrk_proc is not proc:
                            continue
                        if record.get("kind") == "ready":
                            self.wrk_phase = "awaiting_window"
                        summary = demo_evidence.wrk_window(record, raw.encode("utf-8"), self.offered_rps)
                        if summary is not None and summary["start_monotonic"] >= self.scenario_since:
                            self.wrk_window = {**summary, "pid": proc.pid, "raw_log": str(stream.name)}
                            self.front_windows.append(self.wrk_window)
                            self.wrk_phase = "measuring"
                except (ValueError, KeyError, TypeError) as exc:
                    self._log(f"wrk2 窗口无效: {exc}", "err")

    def _stop_traffic_locked(self) -> None:
        self.wrk_phase = "stopped"
        for attribute in ("burst_proc", "compute_proc"):
            self._terminate(getattr(self, attribute, None))
            setattr(self, attribute, None)
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
        pattern = re.compile(r"\[\s*([0-9.]+)s\s*\].*eps:\s*([0-9.]+)")
        with (self.work_dir / f"sysbench-{proc.pid}.log").open("a", encoding="utf-8") as stream:
            for line in proc.stdout:
                stream.write(line)
                stream.flush()
                m = pattern.search(line)
                with self.lock:
                    if m and self.sysbench_proc is proc:
                        self.sysbench_latest_eps = float(m.group(2))
                        self.sysbench_last_report = time.monotonic()
                        self.background_points.append({"elapsed_seconds": float(m.group(1)),
                            "events_per_sec": self.sysbench_latest_eps, "raw_line": line.rstrip("\n"),
                            "raw_log": str(stream.name),
                            "received_monotonic": self.sysbench_last_report, "pid": proc.pid})

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
                self.sysbench_last_report = None
                return
            if self.sysbench_proc is None or self.sysbench_proc.poll() is not None:
                self.sysbench_latest_eps = None
                self.sysbench_last_report = None
                self.background_points.clear()
                argv = [shutil.which("sysbench"), "cpu", f"--threads={self.batch_threads}",
                        "--cpu-max-prime=10000", "--time=3600", "--report-interval=1", "run"]
                if self.batch_kind != "sysbench":
                    argv = [sys.executable, "-u", str(REPO_ROOT / "experiments/demo_load_worker.py"),
                            self.batch_kind, "--threads", str(self.batch_threads)]
                self.sysbench_proc = subprocess.Popen(
                    ["stdbuf", "-oL", "taskset", "-c", "0", *argv],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                t = threading.Thread(target=self._read_sysbench_stdout, args=(self.sysbench_proc,), daemon=True)
                t.start()

    def _probe_http_and_sched(self, duration: float = 0.55, http_enabled=True, max_requests=None) -> dict:
        nginx_pid = self.nginx_proc.pid if (self.nginx_proc and self.nginx_proc.poll() is None) else None
        sysb_pid = self.sysbench_proc.pid if (self.sysbench_proc and self.sysbench_proc.poll() is None) else None

        s0 = _read_schedstat(nginx_pid) if nginx_pid else None
        b0 = _read_proc_ticks(sysb_pid) if sysb_pid else None
        t0_wall = time.monotonic()

        latencies: list[float] = []
        probe_errors = probe_attempts = 0
        if nginx_pid is not None and http_enabled:
            req = f"GET {getattr(self, 'request_path', '/index.html')} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n".encode()
            buf = bytearray(8192)
            sock: socket.socket | None = None
            deadline = t0_wall + duration
            while time.monotonic() < deadline:
                try:
                    probe_attempts += 1
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
                    if max_requests is not None and len(latencies) >= max_requests:
                        break
                    time.sleep(0.002)
                except Exception:
                    probe_errors += 1
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
            if max_requests is not None:
                time.sleep(max(0, duration - (time.monotonic() - t0_wall)))
        else:
            time.sleep(duration)

        dt = max(time.monotonic() - t0_wall, 0.05)
        s1 = _read_schedstat(nginx_pid) if nginx_pid else None
        b1 = _read_proc_ticks(sysb_pid) if sysb_pid else None

        wait_pct = None
        avg_wait_us = None
        slices_per_s = None
        nginx_cpu_pct = None
        sched_valid = bool(s0 and s1 and all(after >= before for before, after in zip(s0, s1)))
        if sched_valid:
            dr = max(0, s1[0] - s0[0])
            dw = max(0, s1[1] - s0[1])
            ds = max(0, s1[2] - s0[2])
            if (dr + dw) > 0:
                wait_pct = round(dw / (dr + dw) * 100.0, 1)
            if ds > 0:
                avg_wait_us = round((dw / 1000.0) / ds, 1)
            slices_per_s = round(ds / dt, 1)
            nginx_cpu_pct = round((dr / 1e9) / dt * 100.0, 1)

        sysb_cpu_pct = 0.0 if sysb_pid is None else None
        if b0 is not None and b1 is not None:
            clk_tck = os.sysconf("SC_CLK_TCK")
            b_dticks = max(0, b1 - b0)
            sysb_cpu_pct = min(100.0, round((b_dticks / clk_tck) / dt * 100.0, 1))

        latencies.sort()
        n_lat = len(latencies)
        p50 = round(latencies[int(n_lat * 0.50)], 3) if n_lat else None
        p95 = round(latencies[int(n_lat * 0.95)], 3) if n_lat else None
        p99 = round(latencies[int(n_lat * 0.99)], 3) if n_lat else None
        last_report = getattr(self, "sysbench_last_report", None)
        report_age = time.monotonic() - last_report if last_report is not None else None
        eps = (round(self.sysbench_latest_eps, 1) if self.sysbench_latest_eps is not None
               and report_age is not None and report_age <= 3 else None) if self.sysbench_enabled else 0.0

        return {
            "http_p50_ms": p50,
            "http_p95_ms": p95,
            "http_p99_ms": p99,
            "nginx_wait_pct": wait_pct,
            "nginx_avg_wait_us": avg_wait_us,
            "nginx_slices_per_s": slices_per_s,
            "nginx_cpu_pct": nginx_cpu_pct,
            "sysbench_cpu_pct": sysb_cpu_pct,
            "sysbench_eps": eps,
            "sysbench_report_age_seconds": round(report_age, 3) if report_age is not None else None,
            "probe_samples": n_lat,
            "probe_errors": probe_errors,
            "probe_attempts": probe_attempts,
            "sample_seconds": round(dt, 4),
            "sample_ended_monotonic": time.monotonic(),
            "sample_utc": demo_evidence.utc_now(),
            "nginx_pid": nginx_pid,
            "sysbench_pid": sysb_pid,
            "raw_schedstat": {"before": s0, "after": s1},
            "schedstat_valid": sched_valid,
        }

    def process_ids(self) -> dict:
        owned = {"agent": os.getpid(), "nginx": None, "wrk2": None, "sysbench": None, "scx": None,
                 "compute": None, "burst": None}
        for role, proc in (("nginx", self.nginx_proc), ("wrk2", self.wrk_proc),
                           ("sysbench", self.sysbench_proc), ("scx", self.backend.process)):
            if proc is not None and proc.poll() is None:
                owned[role] = proc.pid
        for role in ("compute", "burst"):
            proc = getattr(self, role + "_proc", None)
            if proc is not None and proc.poll() is None:
                owned[role] = proc.pid
        return owned

    def evidence(self, view="system") -> dict:
        if view not in demo_evidence.VIEWS:
            raise ValueError("Unknown evidence view")
        with self.lock:
            pids = self.process_ids()
            pin = self.backend.pin_dir / "control" if self.backend.process is not None and self.backend.pin_dir else None
            key = (view, tuple(pids.items()), str(pin))
            cached = self._evidence_cache.get(key)
            if cached is None or time.monotonic() - cached[0] >= 1:
                cached = (time.monotonic(), demo_evidence.readbacks(view, pids, pin))
                self._evidence_cache = {key: cached}
        return {**cached[1], "machine": self.machine,
                "age_seconds": round(time.monotonic() - cached[1]["captured_monotonic"], 3)}

    def export_evidence(self) -> dict:
        state = self.get_state()
        return {"schema_version": 1, "exported_utc": demo_evidence.utc_now(), "state": state,
                "readbacks": {view: self.evidence(view) for view in demo_evidence.VIEWS},
                "measurement_notes": {
                    "http_probe": "short HTTP probe window, separate from wrk2 HDR",
                    "nginx_wait_pct": "delta wait_ns / (delta runtime_ns + delta wait_ns)",
                    "nginx_avg_wait_us": "delta wait_ns / delta schedule_count / 1000; not wakeup-only",
                    "wrk2": "actual completions and HDR statistics from continuous 5s windows",
                    "cgroup": "last completed experiment; null until measured",
                }}

    def export_tuning(self) -> dict:
        with self.lock:
            return {"schema_version": 1, "exported_utc": demo_evidence.utc_now(), "machine": self.machine,
                    "current_scenario": self._effective_scenario(), "running": self.tuning_running,
                    "current_search": self.tuning.status(raw=True) if self.tuning else None,
                    "suite": deepcopy(self.suite), "saved_suite": deepcopy(self.saved_suite)}

    def experiment_snapshot(self) -> dict:
        """Read-only raw observations for reproducible external experiments."""
        with self.lock:
            return {"state": self._get_state(), "captured_monotonic": time.monotonic(),
                    "policy_since": self.policy_since, "policy_revision": self.policy_revision,
                    "scenario_since": self.scenario_since, "uniform_probes": self.experiment_sampling,
                    "frontend_windows": list(self.front_windows), "background_points": list(self.background_points),
                    "kernel_snapshot": self.backend.snapshot()}

    def _get_nginx_worker_pids(self) -> list[int]:
        """Find workers belonging to this sandbox's Nginx instance only."""
        if self.nginx_proc is None or self.nginx_proc.poll() is not None:
            return []
        parent_pid = self.nginx_proc.pid
        parent = process_snapshot(parent_pid)
        if parent is None or parent["comm"] != "nginx":
            return []
        try:
            children = Path(f"/proc/{parent_pid}/task/{parent_pid}/children").read_text().split()
        except OSError:
            children = []
        workers = []
        for value in children:
            if value.isdigit():
                child_pid = int(value)
                child = process_snapshot(child_pid)
                if child and child["comm"] == "nginx":
                    workers.append(child_pid)
        # The benchmark config uses master_process off, so its own PID is the worker.
        return workers or [parent_pid]

    def _loop(self) -> None:
        while True:
            try:
                # Auto-idle if no browser has polled for > 90 seconds
                idle_sec = time.monotonic() - self.last_client_poll
                if idle_sec > 90.0 and not self.choreography_running and not self.tuning_running:
                    if self.traffic_enabled or self.sysbench_enabled:
                        with self.lock:
                            self._idle_flags = (self.traffic_enabled, self.sysbench_enabled)
                            self.traffic_enabled = False
                            self._auto_idled = True
                            self.sysbench_enabled = False
                            self._stop_traffic_locked()
                            self._ensure_sysbench()
                            self._cancel_search("长时间没有页面访问，已停止负载并恢复默认")
                    time.sleep(1.0)
                    continue

                self._ensure_traffic()
                self._ensure_sysbench()
                if self.backend.process is not None:
                    try:
                        self.backend.heartbeat()
                    except Exception:
                        pass

                # Extra probes would change the fixed-rate comparison workload.
                probe_revision = self.workload_revision
                probe_policy_revision = self.policy_revision
                observed_policy = self.backend.snapshot()
                autonomous = self.control_mode == "autonomous"
                use_hdr = (self.tuning_running or (self.auto_agent and not autonomous) or (self.scenario.get("compute", False) and not autonomous)) and not self.experiment_sampling
                metrics = self._probe_http_and_sched(duration=0.55, http_enabled=not use_hdr,
                                                     max_requests=5 if autonomous or self.experiment_sampling else None)
                if probe_revision != self.workload_revision or probe_policy_revision != self.policy_revision:
                    continue
                self._run_auto_agent_step(metrics)

                snap = self.backend.snapshot()
                self.seq += 1
                point = {
                    "seq": self.seq,
                    "workload_revision": probe_revision,
                    "scenario_id": self.scenario_id,
                    "scenario_label": self.scenario["label"],
                    "eps_unit": {"sysbench": "计算事件/s", "wakeup": "周期/s", "cache": "散列/s"}.get(self.batch_kind, "EPS"),
                    "ts": _now_hms(),
                    "sched_ext": observed_policy.get("sched_ext", sched_state()),
                    "policy_id": observed_policy.get("policy_id", "kernel.default"),
                    "slice_us": observed_policy.get("slice_us"),
                    "wakeup_preempt": observed_policy.get("wakeup_preempt", False),
                    "sysbench_on": self.sysbench_enabled,
                    **metrics,
                }
                hdr = self.wrk_window
                point["latency_source"] = "wrk2.hdr.window" if use_hdr else "short_http_probe"
                hdr_fresh = hdr and 0 <= time.monotonic() - hdr["end_monotonic"] <= 12
                point["chart_p50_ms"] = hdr.get("p50_ms") if use_hdr and hdr_fresh else (None if use_hdr else metrics["http_p50_ms"])
                point["chart_p95_ms"] = hdr.get("p95_ms") if use_hdr and hdr_fresh else (None if use_hdr else metrics["http_p95_ms"])
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

    def _current_scene_sample(self):
        if self.history and self.history[-1].get("workload_revision") == self.workload_revision:
            return self.history[-1]
        return None

    def get_state(self) -> dict:
        with self.lock:
            return self._get_state()

    def _get_state(self) -> dict:
        self.last_client_poll = time.monotonic()
        # If traffic was auto-idled and browser reconnects, wake it back up
        if self._auto_idled:
            self._auto_idled = False
            self.traffic_enabled, self.sysbench_enabled = self._idle_flags or (True, False)
            self.control_mode = "autonomous"
            self.auto_agent = True
            self.live_agent.reset("页面恢复访问，重新观察实际进程和运行数据")
            self.live_sampler = LiveSampler()
        snap = self.backend.snapshot()
        current_sample = self._current_scene_sample()
        latest = current_sample or {
            "seq": 0,
            "ts": _now_hms(),
            "http_p50_ms": None,
            "http_p95_ms": None,
            "http_p99_ms": None,
            "nginx_wait_pct": None,
            "nginx_avg_wait_us": None,
            "nginx_slices_per_s": None,
            "nginx_cpu_pct": None,
            "sysbench_cpu_pct": None,
            "sysbench_eps": None,
            "probe_samples": 0,
            "schedstat_valid": False,
        }
        workload = {"phase": self.wrk_phase, "offered_rps": self.offered_rps, "window": self.wrk_window,
                    "age_seconds": round(time.monotonic() - self.wrk_window["end_monotonic"], 3) if self.wrk_window else None,
                    "running": bool(self.wrk_proc and self.wrk_proc.poll() is None)}
        workload["fresh"] = bool(workload["running"] and workload["age_seconds"] is not None
                                 and 0 <= workload["age_seconds"] <= 12)
        return {
            "ok": True,
            "timestamp": _now_hms(),
            "machine": self.machine,
            "process_ids": self.process_ids(),
            "sample_age_seconds": round(time.monotonic() - latest["sample_ended_monotonic"], 3)
                                  if "sample_ended_monotonic" in latest else None,
            "workload": workload,
            "traffic_running": self.traffic_enabled,
            "sysbench_running": self.sysbench_enabled,
            "auto_agent": self.auto_agent,
            "control_mode": self.control_mode,
            "agent": {**self.live_agent.status(), "enabled": self.auto_agent and self.control_mode == "autonomous",
                      "current_strategy": self._strategy_id(snap)},
            "scenario_id": self.scenario_id,
            "sample_pending": current_sample is None,
            "scene_events": list(self.scene_events),
            "workload_revision": self.workload_revision,
            "scenario": self._effective_scenario(),
            "scenarios": deepcopy(SCENARIOS),
            "strategies": deepcopy(STRATEGIES),
            "tuning": self.tuning_state(),
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
        with self.lock:
            try:
                if not isinstance(payload, dict):
                    raise ValueError("请求必须为 JSON 对象")
                action = payload.get("action", "")
                if self.cgroup_state["running"]:
                    raise ValueError("Cgroup 独立实验进行中，请等待结束")
                if self.tuning_running and action not in ("stop_tuning", "set_policy", "set_scenario", "toggle_auto_agent"):
                    raise ValueError("四策略测试进行中；请先停止测试，再改变负载或扩展配置")
                if action == "set_scenario":
                    sid = payload.get("scenario")
                    if sid not in SCENARIOS:
                        raise ValueError("未知测试场景")
                    if set(payload) - {"action", "scenario"}:
                        raise ValueError("场景接口只改变负载；Agent 权重需要单独设置")
                    if self.tuning_running:
                        self._cancel_search("负载变化，可选对照已停止", restore=False)
                    self._apply_scenario(sid)
                    return {"ok": True}
                if action == "set_agent_goals":
                    raise ValueError("性能达标门槛已停用，请使用 set_agent_weights 设置相对权重")
                if action == "set_agent_weights":
                    if set(payload) - {"action", "latency", "throughput"}:
                        raise ValueError("未知 Agent 权重字段")
                    changes = {key: payload[key] for key in ("latency", "throughput") if key in payload}
                    weights = replace(self.live_agent.weights, **changes)
                    self.live_agent.configure(weights)
                    self._log("[Agent 权重] 已更新相对评分权重，不设置 p99 或 EPS 达标线。", "info")
                    return {"ok": True}
                if action == "set_experiment_sampling":
                    if type(payload.get("enable")) is not bool:
                        raise ValueError("enable 必须是布尔值")
                    self.experiment_sampling = payload["enable"]
                    self._log("[实验采样] " + ("所有配置统一每轮最多 5 次 HTTP 探测" if self.experiment_sampling else "恢复日常采样方式"), "info")
                    return {"ok": True}
                if action in ("start_tuning", "start_choreography"):
                    if self.tuning_running:
                        raise ValueError("已有测试正在运行")
                    self._start_search(repeats=payload.get("repeats", 1), windows=payload.get("windows", 3),
                                       automatic=bool(payload.get("automatic", False)))
                    return {"ok": True}
                if action == "start_suite":
                    self._start_suite(payload.get("repeats", 2), payload.get("windows", 3), payload.get("scenarios"))
                    return {"ok": True}
                if action == "stop_tuning":
                    self._cancel_search("用户停止测试，已恢复默认调度")
                    return {"ok": True}
                if action == "toggle_auto_agent":
                    enable = bool(payload.get("enable", not self.auto_agent))
                    if enable:
                        if self.tuning_running:
                            raise ValueError("请先停止当前测试")
                        self._enable_autonomous()
                    else:
                        self._cancel_search("实时 Agent 已暂停，已恢复默认调度")
                    return {"ok": True}
                if action == "set_policy":
                    if payload.get("policy", "default") == "default":
                        strategy_id = "default"
                    else:
                        slice_us = payload.get("slice_us", 1000)
                        preempt = payload.get("wakeup_preempt", True)
                        if type(slice_us) is not int or type(preempt) is not bool:
                            raise ValueError("时间片需要整数微秒，抢占开关需要布尔值")
                        if not SLICE_MIN_US <= slice_us <= SLICE_MAX_US:
                            raise ValueError(f"时间片需要落在 {SLICE_MIN_US}µs–{SLICE_MAX_US}µs 的可验证范围")
                        strategy_id = scx_strategy_id(slice_us, preempt)
                    self._cancel_search("手动接管，旧测试已停止", restore=False)
                    snapshot = self._apply_strategy(strategy_id)
                    self._log(f"[手动配置] {strategy_spec(strategy_id)['label']}，map_id={snapshot.get('map_id')}，generation={snapshot.get('generation')}", "info")
                    return {"ok": True}
                if action in ("toggle_traffic", "toggle_sysbench"):
                    result = self._handle_control(payload)
                    self.workload_revision += 1
                    self.scenario_since = time.monotonic()
                    self.front_windows.clear()
                    return result
                if action == "run_cgroup":
                    low, high = payload.get("low_shares", 1024), payload.get("high_shares", 4096)
                    if any(type(v) is not int or not 2 <= v <= 262144 for v in (low, high)):
                        raise ValueError("CPU shares 需要是 2–262144 的整数")
                    self._cancel_search("Cgroup 独立实验使用默认调度；自动寻优已关闭")
                    self.cgroup_state["running"] = True
                    threading.Thread(target=self._run_cgroup_experiment, args=(low, high), daemon=True).start()
                    return {"ok": True}
                return self._handle_control(payload)
            except (ValueError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                self._log(f"控制请求失败：{exc}", "err")
                return {"ok": False, "error": str(exc)}

    def _handle_control(self, payload: dict) -> dict:
        self.last_client_poll = time.monotonic()
        action = payload.get("action", "")

        if action == "toggle_traffic":
            enable = bool(payload.get("enable", not self.traffic_enabled))
            with self.lock:
                self.traffic_enabled = enable
                self._ensure_traffic()
            self._log(f"{'🟢 已启动' if enable else '⏸ 已暂停'} 前台 Nginx + wrk2 ({self.offered_rps} RPS) 实时压测流")
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

        if action == "ebpf_mode":
            self.network_auto_requested = False
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
            self.network_auto_requested = True
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
                "status": f"HTTP 探测失败 ({type(exc).__name__}, {lat_ms} ms)，请结合 TC 丢包计数核验原因",
                "latency_ms": lat_ms,
            }
            self._log(f"[HTTP 探测失败] port={self.ebpf_port}, error={type(exc).__name__}，请核对 TC 原始计数", "warn")

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
        self.cgroup_state["running"] = True
        self.cgroup_state.update(measured=False, measured_utc=None,
                                 low_shares=low_shares, high_shares=high_shares)
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
            d_low = _read_proc_ticks(p_low.pid) - t0_low
            d_high = _read_proc_ticks(p_high.pid) - t0_high
            if d_low <= 0 or d_high <= 0 or p_low.poll() is not None or p_high.poll() is not None:
                raise RuntimeError("Cgroup 进程未产生有效 CPU 时间，拒绝输出伪造比例")
            total = d_low + d_high
            ratio = round(d_high / d_low, 2)
            expected = round(high_shares / low_shares, 2)
            self.cgroup_state.update({
                "measured": True,
                "measured_utc": demo_evidence.utc_now(),
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

    def do_HEAD(self) -> None:
        self.do_GET()

    def _json_response(self, value, status=200, filename=None) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in ("/api/evidence", "/api/evidence/export", "/api/tuning/export", "/api/experiment/snapshot"):
            if not IS_LINUX or SANDBOX is None:
                self._proxy_to_guest("GET")
                return
            if parsed.path == "/api/tuning/export":
                self._json_response(SANDBOX.export_tuning(), filename="oe-four-strategy-results.json")
                return
            if parsed.path == "/api/experiment/snapshot":
                self._json_response(SANDBOX.experiment_snapshot())
                return
            if parsed.path.endswith("/export"):
                self._json_response(SANDBOX.export_evidence(), filename="oe-demo-evidence.json")
                return
            view = parse_qs(parsed.query).get("view", ["system"])[0]
            try:
                self._json_response(SANDBOX.evidence(view))
            except ValueError as exc:
                self._json_response({"ok": False, "error": str(exc)}, status=400)
            return
        if self.path in ("/", "/index.html", "/demo-dashboard.html"):
            content = HTML_PATH.read_bytes()
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)
            return

        if self.path in ("/report", "/experiment-report.html"):
            rep_path = REPO_ROOT / "docs" / "experiment-report.html"
            if rep_path.is_file():
                content = rep_path.read_bytes()
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                if self.command != "HEAD":
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
                SANDBOX._cancel_search("演示服务停止，已恢复默认调度")
                try:
                    SANDBOX.net_plugin.detach()
                except Exception as exc:
                    SANDBOX._log(f"网络钩子清理失败：{exc}", "err")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
