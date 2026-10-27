#!/usr/bin/env python3
"""Cgroup CPU-share A/B on the default scheduler: equal shares vs 4:1.

Two identical CPU-bound workers are pinned to the same physical CPU and fight
for it. Both arms go through the project's own agent path
(``AutoPluginCoordinator`` -> ``resource.cgroup.create/apply/attach``), so the
grouping, the weight write and the readback are the agent's real actions. Only
the requested shares differ:

    A  equal       latency group 1024 : batch group 1024
    B  weighted    latency group 4096 : batch group 1024

The backend has no cgroup-weight semantics under SCX, so this runs on the
default kernel scheduler with sched_ext verified disabled. Network handling and
the nice demotion in the same coordinator are switched off so the only
difference between A and B is ``cpu.shares``.

Timing per run: 5s warmup, 15s measured window. Nothing is sampled after the
workers are torn down, and no value is imputed.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oeagent.backend import sched_state  # noqa: E402
from oeagent.observe import process_snapshot, read  # noqa: E402
from oeagent.plugins import AutoPluginCoordinator, CgroupPlugin, PluginAutoConfig  # noqa: E402

WORKER_CODE = """
import os, sys, time
cpu, out, seconds = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
os.sched_setaffinity(0, {cpu})
end = time.monotonic() + seconds
n = 0
last = 0.0
with open(out, "w", buffering=1) as handle:
    while True:
        for _ in range(20000):
            n += 1
        now = time.monotonic()
        if now - last >= 0.25:
            handle.write("%.6f %d\\n" % (now, n))
            last = now
        if now >= end:
            break
    handle.write("%.6f %d\\n" % (time.monotonic(), n))
"""


def utc():
    return datetime.now(timezone.utc).isoformat()


def proc_ticks(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[11]) + int(fields[12])
    except Exception:
        return None


def schedstat_runtime(pid):
    try:
        return int(Path(f"/proc/{pid}/schedstat").read_text(encoding="utf-8").split()[0])
    except Exception:
        return None


def group_cpu_ns(path):
    try:
        return int((path / "cpuacct.usage").read_text(encoding="utf-8").strip())
    except Exception:
        return None


def work_at(output, when):
    """Iteration count interpolated to a monotonic instant from the worker's log."""
    points = []
    try:
        for line in Path(output).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                points.append((float(parts[0]), int(parts[1])))
    except Exception:
        return None
    if not points:
        return None
    if when <= points[0][0]:
        return points[0][1]
    if when >= points[-1][0]:
        return points[-1][1]
    for (t0, n0), (t1, n1) in zip(points, points[1:]):
        if t0 <= when <= t1:
            if t1 <= t0:
                return n0
            return n0 + (n1 - n0) * (when - t0) / (t1 - t0)
    return points[-1][1]


def snapshot_task(plugin, group, pid, label):
    info = plugin.inspect({"group": group})
    snap = process_snapshot(pid)
    return {"label": label, "pid": pid, "group": group, "group_path": info["path"],
            "shares_readback": info["value"], "group_procs": info["procs"],
            "start_ticks": snap["start_ticks"] if snap else None,
            "comm": snap["comm"] if snap else None,
            "in_group": pid in info["procs"]}


def run_once(arm, low, high, order, seconds_warmup, seconds_measure, cpu, workdir):
    """One A/B run: agent groups two workers, then the measured window is taken."""
    run = {"arm": arm, "order": order, "requested_low_shares": low, "requested_high_shares": high,
           "cpu": cpu, "warmup_seconds": seconds_warmup, "measure_seconds": seconds_measure,
           "sampling_interval_seconds": 0.25, "started_utc": utc(), "events": [], "workers": []}
    suffix = f"{arm}{order}".lower()
    config = PluginAutoConfig(enabled=True, cgroup_enabled=True,
                              latency_group=f"lab-{suffix}-lat", batch_group=f"lab-{suffix}-bat",
                              latency_shares=high, batch_shares=low,
                              ebpf_enabled=False, lsm_audit_enabled=False,
                              lsm_enforce_batch_nice=False)
    coordinator = AutoPluginCoordinator(config, log=run["events"].append, dry_run=False)
    plugin = coordinator.cgroup
    procs = []
    try:
        # ---- agent action 1: create both groups, write the weights, migrate ----
        files = []
        for name in ("lat", "bat"):
            out = Path(workdir) / f"work-{suffix}-{name}.txt"
            code = WORKER_CODE
            procs.append(subprocess.Popen([sys.executable, "-c", code, str(cpu), str(out),
                                           str(seconds_warmup + seconds_measure + 12)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            files.append(out)
        time.sleep(0.4)
        snap_lat, snap_bat = process_snapshot(procs[0].pid), process_snapshot(procs[1].pid)
        tasks = [{"pid": procs[0].pid, "comm": snap_lat["comm"], "roles": ["latency"],
                  "identity": {"start_ticks": snap_lat["start_ticks"]}},
                 {"pid": procs[1].pid, "comm": snap_bat["comm"], "roles": ["batch"],
                  "identity": {"start_ticks": snap_bat["start_ticks"]}}]
        observation = {"tasks": tasks, "ended_monotonic": time.monotonic()}
        actions = coordinator.step(observation, "kernel.default")["actions"]
        run["agent_actions"] = actions
        if not coordinator.cgroup_active:
            raise RuntimeError("Agent did not report an active cgroup placement; refusing to measure")

        # ---- readback before the measured window ----
        run["readback_before"] = {
            "latency": snapshot_task(plugin, config.latency_group, procs[0].pid, "high"),
            "batch": snapshot_task(plugin, config.batch_group, procs[1].pid, "normal"),
        }
        lat_path = Path(run["readback_before"]["latency"]["group_path"])
        bat_path = Path(run["readback_before"]["batch"]["group_path"])
        if not (run["readback_before"]["latency"]["in_group"] and run["readback_before"]["batch"]["in_group"]):
            raise RuntimeError("PID migration readback failed")

        # ---- warmup then measured window ----
        time.sleep(seconds_warmup)
        t0 = time.monotonic()
        base_lat = {"ticks": proc_ticks(procs[0].pid), "schedstat": schedstat_runtime(procs[0].pid),
                    "group_ns": group_cpu_ns(lat_path)}
        base_bat = {"ticks": proc_ticks(procs[1].pid), "schedstat": schedstat_runtime(procs[1].pid),
                    "group_ns": group_cpu_ns(bat_path)}
        time.sleep(seconds_measure)
        t1 = time.monotonic()
        end_lat = {"ticks": proc_ticks(procs[0].pid), "schedstat": schedstat_runtime(procs[0].pid),
                   "group_ns": group_cpu_ns(lat_path)}
        end_bat = {"ticks": proc_ticks(procs[1].pid), "schedstat": schedstat_runtime(procs[1].pid),
                   "group_ns": group_cpu_ns(bat_path)}

        run["window"] = {"start_monotonic": t0, "end_monotonic": t1, "seconds": t1 - t0}
        ticks_per_s = os.sysconf("SC_CLK_TCK")

        def share(a, b):
            total = (a or 0) + (b or 0)
            return round((a or 0) / total * 100.0, 2) if total > 0 else None

        lat_ticks = (end_lat["ticks"] or 0) - (base_lat["ticks"] or 0)
        bat_ticks = (end_bat["ticks"] or 0) - (base_bat["ticks"] or 0)
        lat_ns = (end_lat["group_ns"] or 0) - (base_lat["group_ns"] or 0)
        bat_ns = (end_bat["group_ns"] or 0) - (base_bat["group_ns"] or 0)
        lat_run = (end_lat["schedstat"] or 0) - (base_lat["schedstat"] or 0)
        bat_run = (end_bat["schedstat"] or 0) - (base_bat["schedstat"] or 0)
        work_lat = (work_at(files[0], t1) or 0) - (work_at(files[0], t0) or 0)
        work_bat = (work_at(files[1], t1) or 0) - (work_at(files[1], t0) or 0)
        run.update({
            "result": {
                "high": {
                    "cpu_seconds": round(lat_ticks / ticks_per_s, 3),
                    "cpu_share_pct": share(lat_ticks, bat_ticks),
                    "cpuacct_seconds": round(lat_ns / 1e9, 3),
                    "cpuacct_share_pct": share(lat_ns, bat_ns),
                    "sched_runtime_seconds": round(lat_run / 1e9, 3),
                    "sched_runtime_share_pct": share(lat_run, bat_run),
                    "work_units": int(round(work_lat)),
                },
                "normal": {
                    "cpu_seconds": round(bat_ticks / ticks_per_s, 3),
                    "cpu_share_pct": share(bat_ticks, lat_ticks),
                    "cpuacct_seconds": round(bat_ns / 1e9, 3),
                    "cpuacct_share_pct": share(bat_ns, lat_ns),
                    "sched_runtime_seconds": round(bat_run / 1e9, 3),
                    "sched_runtime_share_pct": share(bat_run, lat_run),
                    "work_units": int(round(work_bat)),
                },
                "expected_share_pct": {"high": round(high / (low + high) * 100.0, 2),
                                       "normal": round(low / (low + high) * 100.0, 2)},
                "measured_high_to_normal_ratio": round(lat_ticks / bat_ticks, 3) if bat_ticks else None,
                "configured_ratio": round(high / low, 3),
            },
            "readback_after": {
                "latency": snapshot_task(plugin, config.latency_group, procs[0].pid, "high"),
                "batch": snapshot_task(plugin, config.batch_group, procs[1].pid, "normal"),
            },
        })
        # ---- agent action 2: release both processes, then verify the groups are empty ----
        coordinator.step({"tasks": [tasks[0]], "ended_monotonic": time.monotonic()}, "kernel.default")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        coordinator.close()
        run["cleanup"] = {"groups": {}}
        for group in (config.latency_group, config.batch_group):
            try:
                path = plugin.group_path(group)
            except Exception as exc:
                run["cleanup"]["groups"][group] = {"error": str(exc)}
                continue
            if not path.is_dir():
                run["cleanup"]["groups"][group] = {"present": False, "removed": None, "remaining_procs": []}
                continue
            remaining = plugin._read_procs(path)
            removed = None
            if not remaining:
                try:
                    path.rmdir()
                    removed = True
                except OSError as exc:
                    removed = f"failed: {exc}"
            run["cleanup"]["groups"][group] = {"present": True, "removed": removed,
                                               "remaining_procs": remaining}
        run["finished_utc"] = utc()
        run["sched_ext_after"] = sched_state()
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpu", type=int, default=3)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--measure", type=float, default=15.0)
    parser.add_argument("--stop-dashboard", action="store_true",
                        help="stop oe-live-dashboard first so no other load shares the CPU")
    args = parser.parse_args()
    if args.rounds not in (1, 2):
        raise SystemExit("这个对照只为两轮设计；--rounds 只能是 1 或 2")
    args.output.mkdir(parents=True, exist_ok=False)

    dashboard_stopped = False
    if args.stop_dashboard:
        dashboard_stopped = subprocess.run(["systemctl", "stop", "oe-live-dashboard"],
                                           check=False).returncode == 0
        time.sleep(2.0)
    state = sched_state()
    report = {"schema_version": 1, "study": "cgroup-share-ab", "status": "running",
              "started_utc": utc(), "rounds": args.rounds, "runs": [],
              "dashboard_stopped": dashboard_stopped, "sched_ext_initial": state,
              "machine": {"kernel": Path("/proc/sys/kernel/osrelease").read_text().strip(),
                          "cpus": os.cpu_count()},
              "plugin": CgroupPlugin().describe(),
              "method": {"design": "two arms x two rounds with swapped order (A,B / B,A)",
                         "arms": {"A": "cpu.shares 1024:1024 (equal)", "B": "cpu.shares 4096:1024 (4:1)"},
                         "scheduler": "default kernel scheduler; sched_ext must be disabled because this backend has no cgroup weight semantics under SCX",
                         "disabled_for_control": ["network eBPF handling", "LSM nice demotion"],
                         "timing": f"{args.warmup}s warmup + {args.measure}s measured window",
                         "accounting": "per-task /proc utime+stime ticks, per-group cpuacct.usage, per-task schedstat runtime, and each worker's own iteration counter",
                         "scope": "single VMware guest, descriptive course-design comparison"}}
    arms = [("A", 1024, 1024), ("B", 1024, 4096)]
    try:
        if state == "enabled":
            raise RuntimeError("sched_ext 仍为 enabled；Cgroup 权重对照必须在内核默认调度下进行")
        workdir = args.output / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        planes = {1: list(arms), 2: list(reversed(arms))}
        for block in range(1, args.rounds + 1):
            for position, (arm, low, high) in enumerate(planes[block], 1):
                print(json.dumps({"event": "run_start", "block": block, "arm": arm,
                                  "position": position, "low": low, "high": high}, ensure_ascii=False),
                      flush=True)
                run = run_once(arm, low, high, block, args.warmup, args.measure, args.cpu, workdir)
                run["block"] = block
                run["position"] = position
                name = f"block-{block:02d}-{arm}.json"
                (args.output / name).write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
                report["runs"].append({"block": block, "position": position, "arm": arm,
                                       "low_shares": low, "high_shares": high,
                                       "result": run.get("result"), "cleanup": run.get("cleanup"),
                                       "file": name})
                (args.output / "study.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
                print(json.dumps({"event": "run_end", "arm": arm,
                                  "share": (run.get("result") or {}).get("high", {}).get("cpu_share_pct")},
                                 ensure_ascii=False), flush=True)
        report["status"] = "complete"
    except BaseException as exc:
        report.update(status="interrupted", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["ended_utc"] = utc()
        report["sched_ext_final"] = sched_state()
        (args.output / "study.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
        print(json.dumps({"event": "study_end", "status": report["status"],
                          "runs": len(report["runs"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
