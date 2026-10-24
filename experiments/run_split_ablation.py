#!/usr/bin/env python3
"""Fine-grained Quantitative Ablation Suite for Multi-Resource Synergy.

Disentangles:
1. Baseline Default (EEVDF root, 1024:1024, nice 0)
2. Only Cgroup v1 shares (EEVDF, 4096:1024, nice 0)
3. Only Nice priority (EEVDF, root 1024:1024, nice 15 on batch)
4. Cgroup + Nice fallback (EEVDF, 4096:1024 + nice 15)
5. Full Multi-Resource Autonomy (SCX adaptive + Cgroup 4096:1024 + nice 15)

Also records the eBPF isolation experiment:
- Secondary service HTTP requests handled/s & CPU0 % in unmitigated vs mitigated states
- Dropped raw TCP packets at the TC qdisc/cls_bpf layer.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]

from eulerbench.environment import ensure_port_free
from eulerbench.metrics import BackgroundReader
from eulerbench.processes import ProcessManager
from eulerbench.runner import _prepare_nginx, _wait_ready
from oeagent.backend import NativeBackend, sched_state
from oeagent.business import align_background_intervals, stream_window_to_frontend
from oeagent.observe import WorkloadObserver, process_snapshot
from oeagent.plugins import CgroupPlugin, NetworkHookPlugin, SecurityHookPlugin
from experiments.run_rigorous_suite import (
    PolicyController,
    _read_schedstat,
    _read_nginx_tree_ticks,
    _read_proc_ticks,
    _mean_sd,
)

OUTPUT_FILE = ROOT / "state" / "split_ablation_results.json"


def run_single_ablation_condition(
    condition_id: str,
    work_dir: Path,
    wrk_bin: str,
    apply_cgroup: bool = False,
    apply_nice: int = 0,
    batch_threads: int = 2,
    offered_rps: int = 25600,
) -> list[dict]:
    port = 18089
    connections = 32
    service_cpus = [0]
    client_cpus = [1]

    d = work_dir / f"split-{condition_id}-cg{apply_cgroup}-nice{apply_nice}-{int(time.time()*1000)}"
    d.mkdir(parents=True, exist_ok=True)
    ctrl = PolicyController(condition_id, d)
    cg_plugin = CgroupPlugin() if apply_cgroup else None
    attached_cg: list[tuple[str, int]] = []

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

            batch_proc = manager.start(
                "background",
                [shutil.which("sysbench"), "cpu", f"--threads={batch_threads}", "--cpu-max-prime=10000", "--time=3600", "--report-interval=1", "run"],
                service_cpus,
                d / "bg.log",
                pipe=True,
            )
            reader = BackgroundReader(batch_proc.stdout, d / "bg.log", d / "bg-intervals.jsonl")

            time.sleep(0.15)
            # Apply cgroup if requested
            if apply_cgroup and cg_plugin:
                for grp, shares in (("auto_latency", 4096), ("auto_batch", 1024)):
                    info = cg_plugin.create({"group": grp})
                    if info["value"] != shares:
                        cg_plugin.apply({"group": grp, "expected_value": info["value"], "value": shares})
                for p_str in Path("/proc").iterdir():
                    if p_str.name.isdigit():
                        pid_i = int(p_str.name)
                        snap = process_snapshot(pid_i)
                        if snap and snap["comm"] == "nginx":
                            cg_plugin.attach({"group": "auto_latency", "pid": pid_i, "expected_start_ticks": snap["start_ticks"]})
                            attached_cg.append(("auto_latency", pid_i))
                snap_b = process_snapshot(batch_proc.pid)
                if snap_b:
                    cg_plugin.attach({"group": "auto_batch", "pid": batch_proc.pid, "expected_start_ticks": snap_b["start_ticks"]})
                    attached_cg.append(("auto_batch", batch_proc.pid))

            # Apply nice if requested
            if apply_nice != 0:
                os.setpriority(os.PRIO_PROCESS, batch_proc.pid, apply_nice)

            ctrl.start()
            time.sleep(0.3)

            client_argv = [
                "/usr/bin/env",
                "OE_WRK2_WINDOW_MS=5000",
                wrk_bin,
                "-t1",
                f"-c{connections}",
                "-d28s",
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
            ng_ticks_snaps: dict[int, int] = {}
            sb_ticks_snaps: dict[int, int] = {}

            deadline = time.monotonic() + 45.0
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
                        ng_ticks_snaps[0] = _read_nginx_tree_ticks(server.pid)
                        sb_ticks_snaps[0] = _read_proc_ticks(batch_proc.pid)
                    elif kind == "window" and not rec.get("final", False):
                        seq = int(rec["seq"])
                        sched_snaps[seq] = _read_schedstat(server.pid)
                        ng_ticks_snaps[seq] = _read_nginx_tree_ticks(server.pid)
                        sb_ticks_snaps[seq] = _read_proc_ticks(batch_proc.pid)

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

                        d_ng = max(1, ng_ticks_snaps[seq] - ng_ticks_snaps.get(seq - 1, ng_ticks_snaps[seq]))
                        d_sb = max(1, sb_ticks_snaps[seq] - sb_ticks_snaps.get(seq - 1, sb_ticks_snaps[seq]))
                        ng_share_pct = round(d_ng / (d_ng + d_sb) * 100.0, 1)

                        fe = stream_window_to_frontend(rec, offered_rps, raw_json.encode("utf-8"))
                        w_start = rec["start_ns"] / 1e9
                        w_end = rec["end_ns"] / 1e9
                        time.sleep(0.15)
                        bg = align_background_intervals(reader.points, w_start, w_end)
                        raw_eps = bg["events_per_sec"] or bg.get("whole_events_per_sec")
                        if not raw_eps and reader.points:
                            raw_eps = reader.points[-1]["events_per_sec"]
                        eps = round(float(raw_eps or 0.0), 1)

                        windows.append({
                            "seq": seq,
                            "p99_ms": round(fe["p99_ms"], 3),
                            "rps": round(fe["rps"], 1),
                            "rps_retention_pct": round(fe["rps"] / offered_rps * 100.0, 2),
                            "background_eps": eps,
                            "nginx_wait_pct": wait_pct,
                            "nginx_avg_wait_us": avg_wait_us,
                            "nginx_cpu_share_pct": ng_share_pct,
                        })
                        if seq >= 3:
                            break
            return windows
        finally:
            ctrl.stop()
            if cg_plugin and attached_cg:
                for grp, pid_i in attached_cg:
                    try:
                        cg_plugin.detach({"group": grp, "pid": pid_i})
                    except Exception:
                        pass
            manager.stop_all()


def run_detailed_ebpf_flood_ab(work_dir: Path, wrk_bin: str) -> dict:
    net_plugin = NetworkHookPlugin()
    port = 18089
    offered_rps = 25600
    service_cpus = [0]
    client_cpus = [1]
    d = work_dir / f"ebpf-detail-{int(time.time()*1000)}"
    d.mkdir(parents=True, exist_ok=True)
    ctrl = PolicyController("default", d)
    aux_proc = None
    flood_stop = threading.Event()
    results = {}

    with (d / "driver-events.jsonl").open("w", encoding="utf-8") as devents:
        manager = ProcessManager(devents, d)
        try:
            # Python HTTP server that records request count to /tmp/aux_count.txt
            aux_code = (
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "count = 0\n"
                "class H(BaseHTTPRequestHandler):\n"
                "    def log_message(self, *a): pass\n"
                "    def do_GET(self):\n"
                "        global count\n"
                "        count += 1\n"
                "        if count % 50 == 0:\n"
                "            try:\n"
                "                with open('/tmp/aux_count.txt', 'w') as f: f.write(str(count))\n"
                "            except: pass\n"
                "        x = 0\n"
                "        for i in range(40000): x += i\n"
                "        b = b'ok'\n"
                "        self.send_response(200)\n"
                "        self.send_header('Content-Length', '2')\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b)\n"
                "with open('/tmp/aux_count.txt', 'w') as f: f.write('0')\n"
                "HTTPServer(('127.0.0.1', 18095), H).serve_forever()\n"
            )
            aux_proc = subprocess.Popen(["taskset", "-c", "0", sys.executable, "-c", aux_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ensure_port_free(port)
            cfg, page = _prepare_nginx(d, {"port": port, "page_bytes": 4096, "service_cpus": service_cpus})
            server = manager.start("server", [shutil.which("nginx"), "-p", str(d) + "/", "-c", str(cfg)], service_cpus, d / "nginx.log")
            url = f"http://127.0.0.1:{port}/index.html"
            _wait_ready(server, url, page)

            net_plugin.attach({"interface": "lo", "direction": "both", "target_port": 18095, "drop_target": False})

            def _flood_worker():
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                while not flood_stop.is_set():
                    try:
                        with opener.open("http://127.0.0.1:18095/ping", timeout=0.06) as resp:
                            resp.read()
                    except Exception:
                        pass

            threads = [threading.Thread(target=_flood_worker, daemon=True) for _ in range(4)]
            for t in threads:
                t.start()

            client_argv = ["/usr/bin/env", "OE_WRK2_WINDOW_MS=5000", wrk_bin, "-t1", "-c32", "-d33s", "-T3s", f"-R{offered_rps}", "-L", url]
            client = manager.start("client", client_argv, client_cpus, d / "client-stderr.log", pipe=True)
            fd = client.stdout.fileno()
            pending = ""
            windows: list[dict] = []
            sched_snaps: dict[int, tuple[int, int, int] | None] = {}
            aux_ticks: dict[int, int] = {}
            aux_counts: dict[int, int] = {}

            deadline = time.monotonic() + 55.0
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
                        aux_ticks[0] = _read_proc_ticks(aux_proc.pid)
                    elif kind == "window" and not rec.get("final", False):
                        seq = int(rec["seq"])
                        sched_snaps[seq] = _read_schedstat(server.pid)
                        aux_ticks[seq] = _read_proc_ticks(aux_proc.pid)
                        cnt = 0
                        try:
                            cnt = int(Path("/tmp/aux_count.txt").read_text().strip())
                        except Exception:
                            pass
                        aux_counts[seq] = cnt

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
                        windows.append({
                            "seq": seq,
                            "p99_ms": round(fe["p99_ms"], 3),
                            "rps": round(fe["rps"], 1),
                            "rps_retention_pct": round(fe["rps"] / offered_rps * 100.0, 2),
                            "nginx_wait_pct": wait_pct,
                            "nginx_avg_wait_us": avg_wait_us,
                        })
                        if seq == 2:
                            # Enable eBPF TC_ACT_SHOT
                            net_plugin.attach({"interface": "lo", "direction": "both", "target_port": 18095, "drop_target": True})
                        elif seq >= 4:
                            break

            st_final = net_plugin.status()
            flood_stop.set()
            w_by_seq = {w["seq"]: w for w in windows}
            w_no = w_by_seq.get(2, w_by_seq.get(1, {}))
            w_yes = w_by_seq.get(4, w_by_seq.get(3, {}))

            reqs_w2 = max(0, aux_counts.get(2, 0) - aux_counts.get(1, 0))
            reqs_w4 = max(0, aux_counts.get(4, 0) - aux_counts.get(3, 0))
            dt_ticks_w2 = max(0, aux_ticks.get(2, 0) - aux_ticks.get(1, 0))
            dt_ticks_w4 = max(0, aux_ticks.get(4, 0) - aux_ticks.get(3, 0))

            results["without_ebpf_isolation"] = {
                "drop_target": False,
                "foreground_p99_ms": w_no.get("p99_ms"),
                "foreground_rps": w_no.get("rps"),
                "foreground_rps_retention_pct": w_no.get("rps_retention_pct"),
                "foreground_wait_pct": w_no.get("nginx_wait_pct"),
                "foreground_avg_wait_us": w_no.get("nginx_avg_wait_us"),
                "secondary_completed_reqs_per_5s": reqs_w2,
                "secondary_req_rate_rps": round(reqs_w2 / 5.0, 1),
                "secondary_cpu_ticks_per_5s": dt_ticks_w2,
            }
            results["with_ebpf_tc_act_shot"] = {
                "drop_target": True,
                "foreground_p99_ms": w_yes.get("p99_ms"),
                "foreground_rps": w_yes.get("rps"),
                "foreground_rps_retention_pct": w_yes.get("rps_retention_pct"),
                "foreground_wait_pct": w_yes.get("nginx_wait_pct"),
                "foreground_avg_wait_us": w_yes.get("nginx_avg_wait_us"),
                "secondary_completed_reqs_per_5s": reqs_w4,
                "secondary_req_rate_rps": round(reqs_w4 / 5.0, 1),
                "secondary_cpu_ticks_per_5s": dt_ticks_w4,
                "ebpf_stats": st_final.get("stats"),
            }
            return results
        finally:
            flood_stop.set()
            if aux_proc:
                try:
                    aux_proc.terminate()
                    aux_proc.wait(1.0)
                except Exception:
                    pass
            try:
                net_plugin.detach({"interface": "lo"})
            except Exception:
                pass
            ctrl.stop()
            manager.stop_all()


def summarize(s_list: list[dict]) -> dict:
    return {
        "p99_ms": _mean_sd([s["p99_ms"] for s in s_list]),
        "avg_wait_us": _mean_sd([s["nginx_avg_wait_us"] for s in s_list], 1),
        "wait_pct": _mean_sd([s["nginx_wait_pct"] for s in s_list], 1),
        "nginx_cpu_share_pct": _mean_sd([s["nginx_cpu_share_pct"] for s in s_list], 1),
        "rps_retention_pct": _mean_sd([s["rps_retention_pct"] for s in s_list], 2),
        "background_eps": _mean_sd([s["background_eps"] for s in s_list], 1),
    }


def main():
    wrk_meta = json.loads((ROOT / "state/wrk2-window-build.json").read_text(encoding="utf-8"))
    wrk_bin = wrk_meta["binary"]
    if not Path(wrk_bin).is_file():
        wrk_bin = shutil.which("wrk2") or "/root/euler-bench/bin/wrk2"
    work_dir = Path(tempfile.mkdtemp(prefix="oe-split-ablation-"))
    print(f"Starting split ablation suite with {wrk_bin} in {work_dir}...", flush=True)

    conditions_to_run = [
        ("1_baseline_default", "default", False, 0, "1. 默认调度 (EEVDF root, shares 1024:1024, nice 0)"),
        ("2_only_cgroup", "default", True, 0, "2. 仅启用 Cgroup 权重 (EEVDF, shares 4096:1024, nice 0)"),
        ("3_only_nice", "default", False, 15, "3. 仅调整 Nice 优先级 (EEVDF root, nice 15)"),
        ("4_cgroup_and_nice", "default", True, 15, "4. Cgroup + Nice 双重降权兜底 (EEVDF, shares 4096:1024 + nice 15)"),
        ("5_full_scx_multi", "adaptive", True, 15, "5. 全栈多资源自治联动 (SCX 1ms+抢占 + Cgroup + Nice)"),
    ]

    ablation_results = {}
    for key, cid, cg, nc, label in conditions_to_run:
        print(f"\n--> Running Condition: {label} ...", flush=True)
        samples = run_single_ablation_condition(cid, work_dir, wrk_bin, apply_cgroup=cg, apply_nice=nc, batch_threads=2)
        summary = summarize(samples)
        ablation_results[key] = {
            "label": label,
            "condition_id": cid,
            "apply_cgroup": cg,
            "apply_nice": nc,
            "summary": summary,
            "raw_samples": samples,
        }
        print(f"    Done: p99={summary['p99_ms']['mean']}ms, avg_wait={summary['avg_wait_us']['mean']}us, wait_pct={summary['wait_pct']['mean']}%, bg_eps={summary['background_eps']['mean']}")

    print("\n--> Running Detailed eBPF Flood A/B Test...", flush=True)
    ebpf_results = run_detailed_ebpf_flood_ab(work_dir, wrk_bin)
    print("    Done eBPF Flood A/B test.")
    print("    Without eBPF: p99 =", ebpf_results["without_ebpf_isolation"]["foreground_p99_ms"],
          "secondary reqs/5s =", ebpf_results["without_ebpf_isolation"]["secondary_completed_reqs_per_5s"])
    print("    With eBPF:    p99 =", ebpf_results["with_ebpf_tc_act_shot"]["foreground_p99_ms"],
          "secondary reqs/5s =", ebpf_results["with_ebpf_tc_act_shot"]["secondary_completed_reqs_per_5s"],
          "dropped pkts =", ebpf_results["with_ebpf_tc_act_shot"]["ebpf_stats"].get("dropped_packets"))

    final_payload = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "five_condition_ablation": ablation_results,
        "ebpf_detailed_ablation": ebpf_results,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(final_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved all split ablation results to {OUTPUT_FILE}")
    shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
