#!/usr/bin/env python3
"""Full 6-Condition x 4-Scenario Matrix Benchmark matching Opening Defense PPT Slide 10."""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]

from eulerbench.environment import ensure_port_free
from eulerbench.metrics import BackgroundReader
from eulerbench.processes import ProcessManager
from eulerbench.runner import _prepare_nginx, _wait_ready
from oeagent.backend import NativeBackend, sched_state
from oeagent.business import align_background_intervals, stream_window_to_frontend
from oeagent.observe import WorkloadObserver
from oeagent.runtime import EventLog, ResourceLock


CONDITIONS = [
    {"id": "default", "label": "默认调度 (CFS/EEVDF)"},
    {"id": "scx_simple", "label": "原始 scx_simple"},
    {"id": "fixed-20ms", "label": "固定 SCX (20ms)"},
    {"id": "fixed-1ms", "label": "固定 SCX (1ms 无抢占)"},
    {"id": "fixed-1ms-preempt", "label": "固定 SCX (1ms + 唤醒抢占)"},
    {"id": "adaptive", "label": "自适应 Agent (按需抢占+自动回退)"},
]


def _find_scx_simple() -> str:
    candidates = sorted(glob.glob("/root/rpmbuild/BUILD/kernel-*/linux-*/tools/sched_ext/build/bin/scx_simple"))
    for c in candidates:
        if Path(c).is_file() and os.access(c, os.X_OK):
            return c
    raise FileNotFoundError("scx_simple binary not found")


def _read_schedstat(pid: int) -> tuple[int, int, int] | None:
    try:
        parts = Path(f"/proc/{pid}/schedstat").read_text(encoding="utf-8").split()
        return int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return None


def _read_ctxt_switches(pid: int) -> tuple[int, int]:
    """Return (voluntary, nonvoluntary) summed across all threads of pid."""
    vol = 0
    nonvol = 0
    try:
        task_dir = Path(f"/proc/{pid}/task")
        for tid_dir in task_dir.iterdir():
            txt = (tid_dir / "status").read_text(encoding="utf-8")
            m_v = re.search(r"^voluntary_ctxt_switches:\s+(\d+)", txt, re.M)
            m_nv = re.search(r"^nonvoluntary_ctxt_switches:\s+(\d+)", txt, re.M)
            if m_v:
                vol += int(m_v.group(1))
            if m_nv:
                nonvol += int(m_nv.group(1))
    except Exception:
        pass
    return vol, nonvol


class PolicyController:
    """Manages one of the 6 conditions across a test episode."""

    def __init__(self, condition_id: str, work_dir: Path) -> None:
        self.condition_id = condition_id
        self.work_dir = work_dir
        self.event_log = EventLog(work_dir)
        self.backend = NativeBackend(self.event_log, work_dir, lease_seconds=30)
        self.scx_simple_proc: subprocess.Popen | None = None
        self.stop_flag = False
        self.thread: threading.Thread | None = None
        self.active_policy = "kernel.default"
        self.switch_in_ms: float | None = None
        self.rollback_ms: float | None = None
        self.contention_started_at: float | None = None
        self.contention_stopped_at: float | None = None

    def start(self) -> None:
        cid = self.condition_id
        if cid == "default":
            self.active_policy = "kernel.default"
        elif cid == "scx_simple":
            bin_path = _find_scx_simple()
            self.scx_simple_proc = subprocess.Popen(
                [bin_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5.0
            while sched_state() != "enabled" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.active_policy = "scx_simple (20ms)"
        elif cid == "fixed-20ms":
            self.backend.start(20000, wakeup_preempt=False)
            self.active_policy = "scx.slice (20000us)"
        elif cid == "fixed-1ms":
            self.backend.start(1000, wakeup_preempt=False)
            self.active_policy = "scx.slice (1000us, no_preempt)"
        elif cid == "fixed-1ms-preempt":
            self.backend.start(1000, wakeup_preempt=True)
            self.active_policy = "scx.slice (1000us + preempt)"
        elif cid == "adaptive":
            self.active_policy = "kernel.default"

        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        observer = WorkloadObserver([
            {"role": "latency", "comm": ["nginx"]},
            {"role": "batch", "comm": ["sysbench"]},
        ])
        mixed_streak = 0
        nonmixed_streak = 0
        scx_on = False

        while not self.stop_flag:
            if self.backend.process is not None and self.backend.process.poll() is None:
                try:
                    self.backend.heartbeat()
                except Exception:
                    pass
            if self.condition_id != "adaptive":
                time.sleep(0.5)
                continue

            obs = observer.window(0.35)
            tasks = [t for t in obs["tasks"] if t["cpu_percent"] is not None]
            has_latency = any("latency" in t["roles"] for t in tasks)
            has_batch = any("batch" in t["roles"] and (t["cpu_percent"] or 0) >= 10.0 for t in tasks)

            if has_latency and has_batch:
                mixed_streak += 1
                nonmixed_streak = 0
            else:
                nonmixed_streak += 1
                mixed_streak = 0

            if not scx_on and mixed_streak >= 2:
                self.backend.start(1000, wakeup_preempt=True)
                scx_on = True
                self.active_policy = "scx.slice (1000us + preempt)"
                if self.contention_started_at is not None and self.switch_in_ms is None:
                    self.switch_in_ms = round((time.monotonic() - self.contention_started_at) * 1000.0, 1)
            elif scx_on and nonmixed_streak >= 2:
                self.backend.stop()
                scx_on = False
                self.active_policy = "kernel.default"
                if self.contention_stopped_at is not None and self.rollback_ms is None:
                    self.rollback_ms = round((time.monotonic() - self.contention_stopped_at) * 1000.0, 1)

    def stop(self) -> None:
        self.stop_flag = True
        if self.thread is not None:
            self.thread.join(timeout=3.0)
        if self.scx_simple_proc is not None:
            self.scx_simple_proc.terminate()
            try:
                self.scx_simple_proc.wait(timeout=3.0)
            except Exception:
                self.scx_simple_proc.kill()
            self.scx_simple_proc = None
            deadline = time.monotonic() + 3.0
            while sched_state() != "disabled" and time.monotonic() < deadline:
                time.sleep(0.05)
        if self.backend.process is not None:
            self.backend.stop()
        self.event_log.close()


def run_web_scenarios_for_condition(condition_id: str, work_dir: Path, wrk_bin: str) -> dict:
    """Run continuous wrk2 windows covering service_only, mixed, and dynamic recovery."""
    port = 18089
    offered_rps = 25600
    connections = 32
    service_cpus = [0]
    client_cpus = [1]

    d = work_dir / f"web-{condition_id}"
    d.mkdir(parents=True, exist_ok=True)
    ctrl = PolicyController(condition_id, d)

    with (d / "driver-events.jsonl").open("w", encoding="utf-8") as devents:
        manager = ProcessManager(devents, d)
        batch_proc = None
        reader = None
        try:
            ensure_port_free(port)
            cfg, page = _prepare_nginx(d, {"port": port, "page_bytes": 4096, "service_cpus": service_cpus})
            server = manager.start(
                "server",
                [shutil.which("nginx"), "-p", str(d) + "/", "-c", str(cfg)],
                service_cpus,
                d / "nginx.log",
            )
            url = f"http://127.0.0.1:{port}/index.html"
            _wait_ready(server, url, page)

            ctrl.start()
            time.sleep(0.4)

            client_argv = [
                "/usr/bin/env",
                "OE_WRK2_WINDOW_MS=5000",
                wrk_bin,
                "-t1",
                f"-c{connections}",
                "-d37s",
                "-T3s",
                f"-R{offered_rps}",
                "-L",
                url,
            ]
            client = manager.start("client", client_argv, client_cpus, d / "client-stderr.log", pipe=True)
            fd = client.stdout.fileno()
            pending = ""
            windows: list[dict] = []
            sched_snaps: dict[int, tuple[int, int, int] | None] = {}

            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                ready_fds, _, _ = select.select([fd], [], [], 0.1)
                if not ready_fds:
                    if client.poll() is not None and not pending:
                        break
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    if client.poll() is not None:
                        break
                    continue
                pending += chunk.decode("utf-8", errors="replace")
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if not line.startswith("OE_WRK2_STREAM "):
                        continue
                    raw_json = line.split(" ", 1)[1]
                    rec = json.loads(raw_json)
                    kind = rec.get("kind")
                    if kind == "ready":
                        sched_snaps[0] = _read_schedstat(server.pid)
                    elif kind == "window" and not rec.get("final", False):
                        seq = int(rec["seq"])
                        sched_snaps[seq] = _read_schedstat(server.pid)
                        s_prev = sched_snaps.get(seq - 1)
                        s_curr = sched_snaps.get(seq)
                        wait_pct = 0.0
                        avg_wait_us = 0.0
                        if s_prev and s_curr:
                            dr = max(0, s_curr[0] - s_prev[0])
                            dw = max(0, s_curr[1] - s_prev[1])
                            ds = max(0, s_curr[2] - s_prev[2])
                            if (dr + dw) > 0:
                                wait_pct = round(dw / (dr + dw) * 100.0, 1)
                            if ds > 0:
                                avg_wait_us = round((dw / 1000.0) / ds, 1)

                        fe = stream_window_to_frontend(rec, offered_rps, raw_json.encode("utf-8"))
                        w_start = rec["start_ns"] / 1e9
                        w_end = rec["end_ns"] / 1e9
                        eps = 0.0
                        if batch_proc is not None and reader is not None:
                            time.sleep(0.35)
                            bg = align_background_intervals(reader.points, w_start, w_end)
                            raw_eps = bg["events_per_sec"] or bg.get("whole_events_per_sec")
                            if not raw_eps and reader.points:
                                raw_eps = reader.points[-1]["events_per_sec"]
                            eps = round(float(raw_eps or 0.0), 1)

                        windows.append({
                            "seq": seq,
                            "p50_ms": round(fe["p50_ms"], 3),
                            "p95_ms": round(fe["p95_ms"], 3),
                            "p99_ms": round(fe["p99_ms"], 3),
                            "rps": round(fe["rps"], 1),
                            "errors": sum(fe["errors"].values()),
                            "background_eps": eps,
                            "nginx_wait_pct": wait_pct,
                            "nginx_avg_wait_us": avg_wait_us,
                            "active_policy": ctrl.active_policy,
                            "sched_ext": sched_state(),
                        })

                        # Choreography across the 5 windows:
                        # Window 1: pure service_only
                        # After Window 1 -> start sysbench (Windows 2 & 3 are mixed; Window 3 is steady-state mixed)
                        # After Window 3 -> stop sysbench (Window 4 is rollback transition; Window 5 is steady service_after)
                        if seq == 1:
                            ctrl.contention_started_at = time.monotonic()
                            batch_proc = manager.start(
                                "background",
                                [
                                    shutil.which("sysbench"),
                                    "cpu",
                                    "--threads=1",
                                    "--cpu-max-prime=10000",
                                    "--time=3600",
                                    "--report-interval=1",
                                    "run",
                                ],
                                service_cpus,
                                d / "background.log",
                                pipe=True,
                            )
                            reader = BackgroundReader(
                                batch_proc.stdout,
                                d / "background.log",
                                d / "background-intervals.jsonl",
                            )
                        elif seq == 3 and batch_proc is not None:
                            ctrl.contention_stopped_at = time.monotonic()
                            manager.stop(batch_proc)
                            batch_proc = None
                        elif seq >= 5:
                            break

            w_by_seq = {w["seq"]: w for w in windows}
            return {
                "service_only": w_by_seq.get(1, {}),
                "mixed": w_by_seq.get(3, w_by_seq.get(2, {})),
                "dynamic": {
                    "service_before": w_by_seq.get(1, {}),
                    "mixed_transition": w_by_seq.get(2, {}),
                    "mixed_steady": w_by_seq.get(3, {}),
                    "recover_transition": w_by_seq.get(4, {}),
                    "service_after": w_by_seq.get(5, {}),
                    "switch_in_ms": ctrl.switch_in_ms,
                    "rollback_ms": ctrl.rollback_ms,
                },
            }
        finally:
            ctrl.stop()
            manager.stop_all()


def run_batch_only_for_condition(condition_id: str, work_dir: Path) -> dict:
    """Scenario 4: Pure background batch workload (2 threads of sysbench on CPU0, no Nginx)."""
    d = work_dir / f"batch-{condition_id}"
    d.mkdir(parents=True, exist_ok=True)
    ctrl = PolicyController(condition_id, d)
    proc = None
    try:
        ctrl.start()
        time.sleep(0.4)
        proc = subprocess.Popen(
            [
                "taskset",
                "-c",
                "0",
                shutil.which("sysbench"),
                "cpu",
                "--threads=2",
                "--cpu-max-prime=10000",
                "--time=5",
                "run",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(0.4)
        v0, nv0 = _read_ctxt_switches(proc.pid)
        t0 = time.monotonic()
        time.sleep(3.8)
        dt = max(0.1, time.monotonic() - t0)
        v1, nv1 = _read_ctxt_switches(proc.pid)
        active_pol = ctrl.active_policy
        active_scx = sched_state()
        out, _ = proc.communicate(timeout=10)
        m = re.search(r"events per second:\s+([0-9.]+)", out)
        eps = round(float(m.group(1)), 1) if m else 0.0
        invol_per_s = round(max(0, nv1 - nv0) / dt, 1)
        vol_per_s = round(max(0, v1 - v0) / dt, 1)
        return {
            "background_eps": eps,
            "involuntary_switches_per_s": invol_per_s,
            "voluntary_switches_per_s": vol_per_s,
            "active_policy": active_pol,
            "sched_ext": active_scx,
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
        ctrl.stop()


def main() -> int:
    os.sched_setaffinity(0, {2})
    if sched_state() != "disabled":
        raise RuntimeError("sched_ext must be disabled before starting run_ppt_matrix")

    wrk_meta = json.loads((ROOT / "state/wrk2-window-build.json").read_text(encoding="utf-8"))
    wrk_bin = wrk_meta["binary"]
    work_dir = Path(tempfile.mkdtemp(prefix="oe-ppt-matrix-"))

    matrix_results: dict[str, dict] = {}
    with ResourceLock():
        for item in CONDITIONS:
            cid = item["id"]
            label = item["label"]
            print(f"=== Running condition: {cid} ({label}) ===", flush=True)
            web_res = run_web_scenarios_for_condition(cid, work_dir, wrk_bin)
            batch_res = run_batch_only_for_condition(cid, work_dir)
            matrix_results[cid] = {
                "id": cid,
                "label": label,
                "service_only": web_res["service_only"],
                "mixed": web_res["mixed"],
                "dynamic": web_res["dynamic"],
                "batch_only": batch_res,
            }
            print(
                f"  [service_only] p99={web_res['service_only'].get('p99_ms')}ms | "
                f"[mixed] p99={web_res['mixed'].get('p99_ms')}ms, EPS={web_res['mixed'].get('background_eps')} | "
                f"[dynamic after] p99={web_res['dynamic']['service_after'].get('p99_ms')}ms | "
                f"[batch_only] EPS={batch_res['background_eps']}, invol_ctx={batch_res['involuntary_switches_per_s']}/s",
                flush=True,
            )

    # Compute batch_only throughput retention relative to default
    default_batch_eps = matrix_results["default"]["batch_only"]["background_eps"] or 1.0
    for cid, row in matrix_results.items():
        eps = row["batch_only"]["background_eps"]
        row["batch_only"]["retention_pct"] = round(eps / default_batch_eps * 100.0, 2)

    output = {
        "schema_version": 1,
        "status": "passed",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "offered_rps": 25600,
        "window_ms": 5000,
        "conditions": [c["id"] for c in CONDITIONS],
        "scenarios": ["service_only", "mixed", "dynamic", "batch_only"],
        "results": matrix_results,
        "final_sched_ext": sched_state(),
    }
    out_path = ROOT / "state" / "ppt-matrix-validation.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(work_dir, ignore_errors=True)
    print(f"\nSaved full 6x4 matrix validation to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
