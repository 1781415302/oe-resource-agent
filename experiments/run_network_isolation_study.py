#!/usr/bin/env python3
"""eBPF target-port isolation A/B: observe-only vs agent-triggered TC drop.

Foreground business runs on Nginx ``:18086`` (CPU0) driven by one windowed wrk2
client on CPU1. The monitored helper service runs on ``:18095`` (CPU0, so it
really competes with Nginx), and a sustained high-rate connection generator is
aimed at it.

    A  observe      the TC hook counts the target port and always lets it pass
    B  auto         the project's AutoPluginCoordinator converts the measured
                    target-port packet rate into a TC_ACT_SHOT decision and
                    releases it after the cooldown, all by itself

Each round is: normal -> flood -> auto isolation -> flood stops -> recovery.
Two rounds per arm with the order swapped (A,B then B,A). Foreground windows are
5s HDR windows from the same wrk2 stream, so the timeline is continuous.

Boundary this does NOT claim: the decision is a packet rate on one port, so the
rule blocks the whole port once triggered -- legitimate connections to that port
are dropped too. Packet counts are not HTTP request counts.
"""
import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]

from eulerbench.environment import ensure_port_free  # noqa: E402
from eulerbench.runner import _prepare_nginx, _wait_ready  # noqa: E402
from oeagent.backend import sched_state  # noqa: E402
from oeagent.business import stream_window_to_frontend  # noqa: E402
from oeagent.observe import process_snapshot  # noqa: E402
from oeagent.plugins import AutoPluginCoordinator, NetworkHookPlugin, PluginAutoConfig  # noqa: E402

BUSINESS_PORT = 18086
MONITOR_PORT = 18095
OFFERED_RPS = 25600
SERVICE_CPUS = "0"
CLIENT_CPUS = "1"
FLOOD_CPUS = "2"
WINDOW_MS = 5000


def utc():
    return datetime.now(timezone.utc).isoformat()


AUX_CODE = """
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        x = 0
        for i in range(40000):
            x += i
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(body)
HTTPServer(('127.0.0.1', %d), H).serve_forever()
""" % MONITOR_PORT


class Flood:
    """Sustained high-rate connection generator aimed at the monitored port."""

    def __init__(self, port, threads=4, cpus=FLOOD_CPUS):
        self.port = port
        self.threads = threads
        self.cpus = cpus
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.attempts = 0
        self.ok = 0
        self.failed = 0
        self.samples = []
        self.started_monotonic = None
        self.stopped_monotonic = None
        self.workers = []

    def _worker(self):
        try:
            os.sched_setaffinity(0, {int(self.cpus)})
        except Exception:
            pass
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while not self.stop_event.is_set():
            succeeded = False
            try:
                with opener.open(f"http://127.0.0.1:{self.port}/ping", timeout=0.12) as resp:
                    resp.read()
                succeeded = True
            except Exception:
                succeeded = False
            with self.lock:
                self.attempts += 1
                if succeeded:
                    self.ok += 1
                else:
                    self.failed += 1

    def start(self):
        self.started_monotonic = time.monotonic()
        for _ in range(self.threads):
            thread = threading.Thread(target=self._worker, daemon=True)
            thread.start()
            self.workers.append(thread)

    def sample(self, clock):
        with self.lock:
            row = {"monotonic": clock, "attempts": self.attempts, "ok": self.ok, "failed": self.failed}
        self.samples.append(row)
        return row

    def stop(self):
        self.stop_event.set()
        for thread in self.workers:
            thread.join(timeout=1.0)
        self.stopped_monotonic = time.monotonic()
        return {"attempts": self.attempts, "ok": self.ok, "failed": self.failed,
                "started_monotonic": self.started_monotonic,
                "stopped_monotonic": self.stopped_monotonic}


def read_stream_line(stream, pending):
    ready, _, _ = select.select([stream.fileno()], [], [], 0.05)
    if not ready:
        return [], pending
    chunk = os.read(stream.fileno(), 65536)
    if not chunk:
        return [], pending
    pending += chunk.decode("utf-8", errors="replace")
    records = []
    while "\n" in pending:
        line, pending = pending.split("\n", 1)
        if not line.startswith("OE_WRK2_STREAM "):
            continue
        raw = line.split(" ", 1)[1]
        try:
            records.append((json.loads(raw), raw))
        except json.JSONDecodeError:
            continue
    return records, pending


def run_round(arm, block, position, workdir, args):
    round_dir = Path(workdir) / f"block-{block:02d}-{arm.lower()}"
    round_dir.mkdir(parents=True, exist_ok=True)
    result = {"arm": arm, "block": block, "position": position, "started_utc": utc(),
              "business_port": BUSINESS_PORT, "monitor_port": MONITOR_PORT,
              "offered_rps": OFFERED_RPS,
              "schedule_seconds": {"baseline": args.baseline, "flood": args.flood,
                                   "recovery": args.recovery},
              "events": [], "samples": [], "windows": [], "flood_samples": []}
    net_plugin = NetworkHookPlugin()
    coordinator = None
    nginx = None
    aux = None
    client = None
    flood = None
    wrk = json.loads((ROOT / "state" / "wrk2-window-build.json").read_text(encoding="utf-8"))
    try:
        ensure_port_free(BUSINESS_PORT)
        ensure_port_free(MONITOR_PORT)
        net_plugin.detach()

        aux = subprocess.Popen(["taskset", "-c", SERVICE_CPUS, sys.executable, "-c", AUX_CODE],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (round_dir / "nginx").mkdir(parents=True, exist_ok=True)
        cfg, page = _prepare_nginx(round_dir / "nginx", {"port": BUSINESS_PORT, "page_bytes": 4096,
                                                         "service_cpus": [int(SERVICE_CPUS)]})
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "keepalive_timeout 65;", "keepalive_timeout 65; keepalive_requests 10000000;"), encoding="utf-8")
        nginx = subprocess.Popen(["taskset", "-c", SERVICE_CPUS, shutil.which("nginx"),
                                  "-p", str(round_dir / "nginx") + "/", "-c", str(cfg)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{BUSINESS_PORT}/index.html"
        _wait_ready(nginx, url, page)
        nginx_pid = nginx.pid
        result["business_pid"] = nginx_pid
        result["monitor_pid"] = aux.pid

        if arm == "B":
            config = PluginAutoConfig(enabled=True, cgroup_enabled=False, ebpf_enabled=True,
                                      monitor_port=MONITOR_PORT,
                                      drop_threshold_pps=args.threshold_pps,
                                      drop_hold_windows=args.hold_windows,
                                      lsm_audit_enabled=False, lsm_enforce_batch_nice=False)

            def log_event(event):
                entry = dict(event)
                entry.setdefault("monotonic", time.monotonic())
                result["events"].append(entry)

            coordinator = AutoPluginCoordinator(config, log=log_event, dry_run=False)
        else:
            # Same attach arguments as the coordinator uses in arm B, so the only
            # difference between the arms is the drop decision itself.
            net_plugin.attach({"interface": "lo", "target_port": MONITOR_PORT, "drop_target": False})
            result["events"].append({"event": "manual_observe_attach", "monotonic": time.monotonic(),
                                     "drop_target": False})

        client_env = dict(os.environ, OE_WRK2_WINDOW_MS=str(WINDOW_MS))
        client = subprocess.Popen(["taskset", "-c", CLIENT_CPUS, wrk["binary"], "-t1", "-c32",
                                   "-d" + str(int(args.baseline + args.flood + args.recovery + 25)) + "s",
                                   "-T3s", f"-R{OFFERED_RPS}", "-L", url],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=client_env)
        pending = ""
        clock0 = None
        flood_start = flood_stop_at = None
        next_sample = 0.0
        next_step = 0.0
        total = args.baseline + args.flood + args.recovery
        while True:
            records, pending = read_stream_line(client.stdout, pending)
            for record, raw in records:
                if record.get("kind") == "ready" and clock0 is None:
                    clock0 = time.monotonic()
                    result["stream_ready_monotonic"] = clock0
                elif record.get("kind") == "window" and not record.get("final", False) and clock0:
                    front = stream_window_to_frontend(record, OFFERED_RPS, raw.encode("utf-8"))
                    front["seq"] = int(record["seq"])
                    result["windows"].append({key: front[key] for key in
                                              ("seq", "start_monotonic", "end_monotonic", "duration_seconds",
                                               "requests", "rps", "completion_fraction", "p50_ms", "p95_ms",
                                               "p99_ms", "errors", "source_sha256")})
            if clock0 is None:
                if client.poll() is not None:
                    raise RuntimeError("wrk2 stream ended before calibration; refusing to report a run")
                continue
            clock = time.monotonic() - clock0

            if clock >= args.baseline and flood is None:
                flood = Flood(MONITOR_PORT)
                flood.start()
                flood_start = time.monotonic()
                result["events"].append({"event": "flood_started", "monotonic": flood_start,
                                         "clock": round(clock, 2)})
            if flood is not None and clock >= args.baseline + args.flood and flood_stop_at is None:
                result["flood_totals"] = flood.stop()
                flood_stop_at = time.monotonic()
                result["events"].append({"event": "flood_stopped", "monotonic": flood_stop_at,
                                         "clock": round(clock, 2)})

            if clock >= next_sample:
                next_sample = clock + 0.5
                stats = net_plugin.status()
                dropping = coordinator.ebpf_dropping if coordinator is not None else False
                row = {"clock": round(clock, 3), "monotonic": time.monotonic(),
                       "active": bool(stats.get("active")),
                       "target_packets": int((stats.get("stats") or {}).get("target_packets", 0)),
                       "dropped_packets": int((stats.get("stats") or {}).get("dropped_packets", 0)),
                       "total_packets": int((stats.get("stats") or {}).get("total_packets", 0)),
                       "drop_target": bool((stats.get("config") or {}).get("drop_target", 0)) if stats.get("config") else dropping,
                       "coordinator_dropping": dropping,
                       "phase": ("baseline" if clock < args.baseline
                                 else "flood" if clock < args.baseline + args.flood else "recovery")}
                if flood is not None and flood_stop_at is None:
                    flood_row = flood.sample(row["monotonic"])
                    row["flood_attempts"] = flood_row["attempts"]
                    row["flood_ok"] = flood_row["ok"]
                    row["flood_failed"] = flood_row["failed"]
                result["samples"].append(row)

            if coordinator is not None and clock >= next_step:
                next_step = clock + 1.0
                snap = process_snapshot(nginx_pid)
                observation = {"tasks": [{"pid": nginx_pid, "comm": snap["comm"] if snap else "nginx",
                                          "roles": ["latency"],
                                          "identity": {"start_ticks": snap["start_ticks"] if snap else None}}],
                               "ended_monotonic": time.monotonic()}
                before = coordinator.ebpf_dropping
                coordinator.step(observation, "kernel.default")
                if coordinator.ebpf_dropping != before:
                    result["events"].append({"event": "drop_state_changed", "clock": round(clock, 2),
                                             "monotonic": time.monotonic(),
                                             "drop_target": coordinator.ebpf_dropping,
                                             "last_observed_pps": coordinator.last_observed_pps})
            if clock > total:
                break
        result["flood_totals"] = result.get("flood_totals") or (flood.stop() if flood else None)
        result["coordinator_status"] = coordinator.status() if coordinator is not None else None
        result["ebpf_final"] = net_plugin.status()
        result["kernel_visibility"] = {
            "tc_filter": subprocess.run(["tc", "filter", "show", "dev", "lo", "egress"],
                                        capture_output=True, text=True).stdout.strip(),
            "sched_ext": sched_state(),
        }
        result["quality"] = "complete"
    except BaseException as exc:
        result["quality"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if flood is not None:
            flood.stop()
        if client is not None and client.poll() is None:
            client.terminate()
            try:
                client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
        if coordinator is not None:
            coordinator.close()
        try:
            net_plugin.detach()
        except Exception:
            pass
        for proc in (nginx, aux):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        result["detached_after_round"] = not net_plugin.status().get("active", False)
        result["ended_utc"] = utc()
        (round_dir / "round.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--baseline", type=float, default=12.0)
    parser.add_argument("--flood", type=float, default=20.0)
    parser.add_argument("--recovery", type=float, default=15.0)
    parser.add_argument("--threshold-pps", type=float, default=20.0)
    parser.add_argument("--hold-windows", type=int, default=3)
    parser.add_argument("--stop-dashboard", action="store_true")
    args = parser.parse_args()
    if args.rounds not in (1, 2):
        raise SystemExit("这个对照只为两轮设计；--rounds 只能是 1 或 2")
    args.output.mkdir(parents=True, exist_ok=False)

    stopped = False
    if args.stop_dashboard:
        stopped = subprocess.run(["systemctl", "stop", "oe-live-dashboard"], check=False).returncode == 0
        time.sleep(2.0)
    subprocess.run(["pkill", "-9", "-f", "scx_oe_adaptive"], check=False)

    report = {"schema_version": 1, "study": "ebpf-port-isolation-ab", "status": "running",
              "started_utc": utc(), "rounds": args.rounds, "runs": [],
              "dashboard_stopped": stopped, "sched_ext_initial": sched_state(),
              "plugin": NetworkHookPlugin().describe(),
              "protection_scope": "target port only; when triggered, every connection to that port is dropped",
              "method": {"design": "two arms x two rounds with swapped order (A,B / B,A)",
                         "arm_a": "TC hook in observe mode; the target port is always allowed",
                         "arm_b": "AutoPluginCoordinator decides from the measured target-port packet rate "
                                  f"(threshold {args.threshold_pps} pps, hold {args.hold_windows}s) and releases after cooldown",
                         "business": f"Nginx :{BUSINESS_PORT} on CPU{SERVICE_CPUS}, one windowed wrk2 client on CPU{CLIENT_CPUS}, offered {OFFERED_RPS} RPS",
                         "monitor": f"helper HTTP server :{MONITOR_PORT} on CPU{SERVICE_CPUS}, flooded from CPU{FLOOD_CPUS}",
                         "phases": f"{args.baseline}s normal + {args.flood}s flood + {args.recovery}s recovery",
                         "windows": f"continuous {WINDOW_MS // 1000}s HDR windows from one wrk2 stream",
                         "packet_rate_note": "BPF target packet counters are packets, not HTTP requests",
                         "scope": "single VMware guest, descriptive course-design comparison"}}
    order = {1: ["A", "B"], 2: ["B", "A"]}
    try:
        for block in range(1, args.rounds + 1):
            for position, arm in enumerate(order[block], 1):
                print(json.dumps({"event": "round_start", "block": block, "arm": arm,
                                  "position": position}, ensure_ascii=False), flush=True)
                run = run_round(arm, block, position, args.output, args)
                report["runs"].append({key: run.get(key) for key in
                                       ("arm", "block", "position", "quality", "flood_totals",
                                        "coordinator_status", "detached_after_round", "error")})
                (args.output / "study.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
                print(json.dumps({"event": "round_end", "arm": arm, "block": block,
                                  "windows": len(run.get("windows", [])),
                                  "events": [e.get("event") for e in run.get("events", [])]},
                                 ensure_ascii=False), flush=True)
        report["status"] = "complete"
    except BaseException as exc:
        report.update(status="interrupted", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        try:
            NetworkHookPlugin().detach()
        except Exception:
            pass
        report["ended_utc"] = utc()
        report["sched_ext_final"] = sched_state()
        (args.output / "study.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
        print(json.dumps({"event": "study_end", "status": report["status"],
                          "runs": len(report["runs"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
