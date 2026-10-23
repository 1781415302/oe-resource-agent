"""Observe workloads from procfs, retaining identity and missing-data reasons."""
import os
import platform
import re
import time
from pathlib import Path

from .protocol import ToolError


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return None


def cpu_list(text):
    result = set()
    for part in text.strip().split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = map(int, part.split("-", 1))
            if lo < 0 or hi < lo or hi - lo > 4096:
                raise ValueError("Invalid CPU range")
            result.update(range(lo, hi + 1))
        else:
            result.add(int(part))
    return sorted(result)


def process_snapshot(pid, proc_root=Path("/proc")):
    directory = proc_root / str(pid)
    contents = read(directory / "stat")
    status = read(directory / "status")
    if contents is None or status is None:
        return None
    end = contents.rfind(")")
    if end < 0:
        return None
    fields = contents[end + 2:].split()
    if len(fields) < 22:
        return None
    switches = {"voluntary": 0, "involuntary": 0}
    readable, waiting, executions = 0, 0, 0
    waiting_available = False
    for task in (directory / "task").glob("*"):
        detail = read(task / "status")
        if detail is not None:
            readable += 1
            for name, key in (("voluntary", "voluntary_ctxt_switches"), ("involuntary", "nonvoluntary_ctxt_switches")):
                match = re.search(rf"^{key}:\s+(\d+)", detail, re.M)
                if match:
                    switches[name] += int(match.group(1))
        schedstat = read(task / "schedstat")
        if schedstat and len(schedstat.split()) >= 3:
            values = schedstat.split()
            waiting += int(values[1])
            executions += int(values[2])
            waiting_available = True
    last = read(directory / "stat")
    if last is None:
        return None
    last_fields = last[last.rfind(")") + 2:].split()
    if len(last_fields) < 22 or last_fields[19] != fields[19]:
        return None
    cpus = re.search(r"^Cpus_allowed_list:\s+(.*)$", status, re.M)
    return {"pid": pid, "start_ticks": int(fields[19]), "comm": contents[contents.find("(") + 1:end],
            "state": fields[0], "cpu_ticks": int(fields[11]) + int(fields[12]),
            "threads": int(fields[17]), "switches": switches if readable else None,
            "run_wait_ns": waiting if waiting_available else None,
            "schedule_count": executions if waiting_available else None,
            "allowed_cpus": cpu_list(cpus.group(1)) if cpus else None,
            "cgroups": read(directory / "cgroup")}


def system_snapshot(proc_root=Path("/proc")):
    contents = read(proc_root / "stat")
    if contents is None:
        raise ToolError("OBSERVATION_UNAVAILABLE", "无法读取 /proc/stat。")
    cpus = {}
    for line in contents.splitlines():
        pieces = line.split()
        if pieces and re.fullmatch(r"cpu\d+", pieces[0]):
            ticks = list(map(int, pieces[1:9]))
            if len(ticks) == 8:
                cpus[pieces[0]] = {"total": sum(ticks), "busy": sum(ticks[i] for i in (0, 1, 2, 5, 6)), "steal": ticks[7]}
    pressure = read(proc_root / "pressure/cpu")
    return {"cpus": cpus, "loadavg": read(proc_root / "loadavg"), "cpu_pressure_raw": pressure,
            "schedstats_enabled": read(proc_root / "sys/kernel/sched_schedstats")}


class WorkloadObserver:
    def __init__(self, selectors, proc_root=Path("/proc"), clock_ticks=None):
        self.selectors = selectors
        self.proc_root = Path(proc_root)
        if platform.system() != "Linux" and self.proc_root == Path("/proc"):
            raise ToolError("PLATFORM_UNSUPPORTED", "实时采样需要 Linux。")
        self.clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
        self.boot_id = (read(self.proc_root / "sys/kernel/random/boot_id") or "unknown").strip()

    def snapshot(self):
        tasks = []
        for directory in self.proc_root.iterdir():
            if not directory.name.isdigit():
                continue
            pid = int(directory.name)
            comm = (read(directory / "comm") or "").strip()
            selected = [s for s in self.selectors if comm in s.get("comm", []) or pid in s.get("pids", [])]
            if not selected:
                continue
            proc = process_snapshot(pid, self.proc_root)
            if proc:
                proc["roles"] = sorted(set(s["role"] for s in selected))
                tasks.append(proc)
        return {"monotonic": time.monotonic(), "system": system_snapshot(self.proc_root), "tasks": tasks}

    def window(self, seconds=1):
        if platform.system() != "Linux" and self.proc_root == Path("/proc"):
            raise ToolError("PLATFORM_UNSUPPORTED", "实时采样需要 Linux。")
        first = self.snapshot()
        time.sleep(seconds)
        second = self.snapshot()
        dt = second["monotonic"] - first["monotonic"]
        if dt <= 0:
            raise ToolError("OBSERVATION_INVALID", "采样时间无效。")
        prior = {(p["pid"], p["start_ticks"]): p for p in first["tasks"]}
        observed, missing = [], []
        for proc in second["tasks"]:
            row = {**proc, "identity": {"boot_id": self.boot_id, "pid": proc["pid"], "start_ticks": proc["start_ticks"]},
                   "cpu_percent": None, "voluntary_per_sec": None, "involuntary_per_sec": None, "run_wait_fraction": None}
            old = prior.get((proc["pid"], proc["start_ticks"]))
            if old and proc["cpu_ticks"] >= old["cpu_ticks"]:
                row["cpu_percent"] = (proc["cpu_ticks"] - old["cpu_ticks"]) / self.clock_ticks / dt * 100
                if proc["switches"] is not None and old["switches"] is not None:
                    for key in ("voluntary", "involuntary"):
                        delta = proc["switches"][key] - old["switches"][key]
                        if delta >= 0:
                            row[key + "_per_sec"] = delta / dt
                enabled = (second["system"]["schedstats_enabled"] or "").strip() == "1"
                if enabled and proc["run_wait_ns"] is not None and old["run_wait_ns"] is not None and proc["run_wait_ns"] >= old["run_wait_ns"]:
                    row["run_wait_fraction"] = (proc["run_wait_ns"] - old["run_wait_ns"]) / (dt * 1e9)
            else:
                missing.append({"pid": proc["pid"], "reason": "new, replaced or unreadable in first sample"})
            observed.append(row)
        cpu_rows = {}
        for name, current in second["system"]["cpus"].items():
            previous = first["system"]["cpus"].get(name)
            total = current["total"] - previous["total"] if previous else 0
            cpu_rows[name] = {key + "_percent": (current[key] - previous[key]) / total * 100 if total > 0 else None for key in ("busy", "steal")}
        return {"schema_version": 1, "window_seconds": dt, "ended_monotonic": second["monotonic"],
                "boot_id": self.boot_id, "tasks": observed, "cpus": cpu_rows,
                "quality": {"missing": missing, "cpu_psi_available": second["system"]["cpu_pressure_raw"] is not None,
                            "schedstats_enabled": (second["system"]["schedstats_enabled"] or "").strip() == "1"}}
