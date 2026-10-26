"""Bounded, read-only kernel evidence for the demo and its independent SSH view."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import socket
import subprocess
import time

VIEWS = ("system", "processes", "scheduler", "cgroup", "network")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def identity(project_root, session_directory):
    release = {}
    for line in (_text("/etc/os-release") or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            release[key] = value.strip('"')
    collector = Path(project_root) / "experiments/web_dashboard.py"
    return {
        "hostname": socket.gethostname(), "os": release.get("PRETTY_NAME", platform.system()),
        "kernel": platform.release(), "architecture": platform.machine(), "vcpus": os.cpu_count(),
        "boot_id": _text("/proc/sys/kernel/random/boot_id"), "agent_pid": os.getpid(),
        "session_id": Path(session_directory).name, "started_utc": utc_now(),
        "collector_sha256": hashlib.sha256(collector.read_bytes()).hexdigest(),
    }


def run_readonly(argv):
    """Only callers below choose argv. No shell, command or path comes from HTTP."""
    started = time.monotonic()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=2)
        return {"command": shlex.join(argv), "exit_code": result.returncode,
                "stdout": result.stdout[:24000], "stderr": result.stderr[:4000],
                "duration_ms": round((time.monotonic() - started) * 1000, 2)}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": shlex.join(argv), "exit_code": None, "stdout": "",
                "stderr": str(exc), "duration_ms": round((time.monotonic() - started) * 1000, 2)}


def readbacks(view, process_ids, control_pin=None):
    if view not in VIEWS:
        raise ValueError("Unknown evidence view")
    # PID values are obtained from the sandbox's own Popen instances.
    pids = sorted({pid for pid in process_ids.values() if isinstance(pid, int) and pid > 0})
    commands = []
    notes = []
    if view == "system":
        commands = [["uname", "-a"], ["cat", "/proc/sys/kernel/random/boot_id"],
                    ["cat", "/proc/uptime"], ["cat", "/proc/sys/kernel/sched_schedstats"]]
    elif view == "processes":
        if pids:
            commands = [["ps", "-p", ",".join(map(str, pids)), "-o", "pid,ppid,psr,comm,args"]]
            for role, pid in process_ids.items():
                if pid in pids and role in ("nginx", "sysbench"):
                    commands.extend([["cat", f"/proc/{pid}/schedstat"],
                                     ["taskset", "-pc", str(pid)]])
        else:
            notes.append("当前没有运行中的受管负载进程。")
    elif view == "scheduler":
        commands = [["cat", "/sys/kernel/sched_ext/state"]]
        if control_pin is not None:
            pin = str(control_pin)
            commands.extend([["bpftool", "-j", "map", "show", "pinned", pin],
                             ["bpftool", "-j", "map", "lookup", "pinned", pin,
                              "key", "hex", "00", "00", "00", "00"]])
        else:
            notes.append("当前为默认调度，Agent 没有挂载 SCX 控制 Map。")
    elif view == "cgroup":
        for role, pid in process_ids.items():
            if pid in pids and role in ("nginx", "sysbench"):
                commands.append(["cat", f"/proc/{pid}/cgroup"])
        mount = next((path for path in (Path("/sys/fs/cgroup/cpu,cpuacct"), Path("/sys/fs/cgroup/cpu"))
                      if path.is_dir()), None)
        if mount:
            for name in ("auto_latency", "auto_batch", "live-low", "live-high"):
                group = mount / "oeagent" / name
                if group.is_dir():
                    commands.extend([["cat", str(group / "cpu.shares")],
                                     ["cat", str(group / "cgroup.procs")]])
    elif view == "network":
        commands = [["tc", "-s", "filter", "show", "dev", "lo", "egress"]]
        pin = Path("/sys/fs/bpf/oeagent_net/oe_net_stats_map")
        if pin.exists():
            commands.append(["bpftool", "-j", "map", "lookup", "pinned", str(pin),
                             "key", "hex", "00", "00", "00", "00"])
        else:
            notes.append("当前未挂载网络统计 Map。")
    return {"schema_version": 1, "view": view, "captured_utc": utc_now(),
            "captured_monotonic": time.monotonic(), "process_ids": process_ids,
            "notes": notes, "results": [run_readonly(argv) for argv in commands]}


def wrk_window(record, raw_bytes, offered_rps=25600):
    from .business import stream_window_to_frontend
    if record.get("kind") != "window" or record.get("final"):
        return None
    counters = ("seq", "start_ns", "end_ns", "requests", "terminal_exclusions", "histogram_overflows")
    if any(type(record.get(name)) is not int or record[name] < 0 for name in counters):
        raise ValueError("Invalid window counter")
    if record["end_ns"] <= record["start_ns"] or record["histogram_overflows"]:
        raise ValueError("Invalid window duration or HDR overflow")
    bins = record.get("bins_us")
    if not isinstance(bins, list) or any(not isinstance(row, list) or len(row) != 2
            or any(type(value) is not int or value < 0 for value in row) for row in bins):
        raise ValueError("Invalid HDR bins")
    if sum(row[1] for row in bins) + record["terminal_exclusions"] != record["requests"]:
        raise ValueError("HDR sample and completed request counts disagree")
    errors = record.get("errors", {})
    if any(type(errors.get(key)) is not int or errors[key] < 0
           for key in ("connect", "read", "write", "status", "timeout")):
        raise ValueError("Invalid error counter")
    summary = stream_window_to_frontend(record, offered_rps, raw_bytes)
    return {**summary, "raw_record": raw_bytes.decode("utf-8"),
            "seq": record["seq"], "received_utc": utc_now(),
            "received_monotonic": time.monotonic(), "valid": True}
