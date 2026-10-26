"""Convert real procfs/probe/window data to the scene-free Agent contract."""
import hashlib
import json
from pathlib import Path
import os
import time

from .live_agent import Observation
from .observe import process_snapshot, read, system_snapshot


class LiveSampler:
    def __init__(self):
        self.previous = {}
        self.previous_system = None
        self.previous_time = None
        self.clock_ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self.sequence = 0

    @staticmethod
    def _runnable(pid):
        count = 0
        for task in (Path("/proc") / str(pid) / "task").glob("*/stat"):
            value = read(task)
            if value and ") " in value and value.rsplit(") ", 1)[1].startswith("R "):
                count += 1
        return count

    def sample(self, metrics, process_ids, strategy, policy_since, frontend_window):
        now = time.monotonic()
        observed = {}
        for role in ("nginx", "compute", "sysbench"):
            pid = process_ids.get(role)
            if isinstance(pid, int) and pid > 0:
                value = process_snapshot(pid)
                if value is not None:
                    observed[role] = value
        system = system_snapshot()
        elapsed = now - self.previous_time if self.previous_time is not None else None
        features = {}
        for role, value in observed.items():
            old = self.previous.get(role)
            if not old or old["pid"] != value["pid"] or old["start_ticks"] != value["start_ticks"] or not elapsed or elapsed <= 0:
                features[role] = {}
                continue
            def rate(key):
                new, before = value.get(key), old.get(key)
                return (new-before)/elapsed if new is not None and before is not None and new >= before else None
            cpu = rate("cpu_ticks")
            switches = {}
            for key in ("voluntary", "involuntary"):
                new = (value.get("switches") or {}).get(key)
                before = (old.get("switches") or {}).get(key)
                switches[key] = (new-before)/elapsed if new is not None and before is not None and new >= before else None
            features[role] = {"cpu": cpu/self.clock_ticks*100 if cpu is not None else None, **switches}
        allowed = {cpu for process in observed.values() for cpu in (process.get("allowed_cpus") or [])}
        busy = total = 0
        if self.previous_system:
            for cpu in allowed:
                key = f"cpu{cpu}"
                current = system["cpus"].get(key)
                old = self.previous_system["cpus"].get(key)
                if current and old and current["total"] > old["total"] and current["busy"] >= old["busy"]:
                    busy += current["busy"] - old["busy"]
                    total += current["total"] - old["total"]
        cpu_busy = busy/total*100 if total else None
        frontend = any(role in observed for role in ("nginx", "compute"))
        background = "sysbench" in observed
        front_cpus = [features[role].get("cpu") for role in ("nginx", "compute") if role in features]
        frontend_cpu = sum(front_cpus) if front_cpus and all(value is not None for value in front_cpus) else (0.0 if not frontend else None)
        background_cpu = features.get("sysbench", {}).get("cpu") if background else 0.0
        nginx = observed.get("nginx")
        matched_probe = bool(nginx and metrics.get("nginx_pid") == nginx["pid"])
        identities = dict(observed)
        client_pid = process_ids.get("wrk2")
        if isinstance(client_pid, int) and client_pid > 0:
            client = process_snapshot(client_pid)
            if client is not None:
                identities["client"] = client
        identity = hashlib.sha256(json.dumps(sorted((role, p["pid"], p["start_ticks"], p["allowed_cpus"])
            for role, p in identities.items()), sort_keys=True).encode()).hexdigest()
        window = frontend_window or {}
        hdr_valid = bool(frontend and window.get("pid") == process_ids.get("wrk2")
                         and window.get("start_monotonic", -1) >= policy_since + 1
                         and 4.5 <= window.get("end_monotonic", 0) - window.get("start_monotonic", 0) <= 5.5
                         and 0 <= now - window.get("end_monotonic", -100) <= 12)
        self.sequence += 1
        result = Observation(
            sequence=self.sequence, now=now, identity=identity, strategy=strategy,
            frontend_present=frontend, background_present=background, cpu_busy_pct=cpu_busy,
            frontend_cpu_pct=frontend_cpu, background_cpu_pct=background_cpu,
            wait_pct=metrics.get("nginx_wait_pct") if matched_probe else None,
            avg_wait_us=metrics.get("nginx_avg_wait_us") if matched_probe else None,
            background_runnable=self._runnable(observed["sysbench"]["pid"]) if background else 0,
            voluntary_per_sec=features.get("sysbench", {}).get("voluntary"),
            involuntary_per_sec=features.get("sysbench", {}).get("involuntary"),
            probe_p95_ms=metrics.get("http_p95_ms") if matched_probe else None,
            probe_samples=metrics.get("probe_samples", 0) if matched_probe else 0,
            probe_errors=metrics.get("probe_errors", 0) if matched_probe else 0,
            eps=metrics.get("sysbench_eps") if background else None,
            hdr_id=f"{window.get('pid')}:{window.get('seq')}:{window.get('source_sha256')}" if hdr_valid else None,
            hdr_start=window.get("start_monotonic") if hdr_valid else None,
            hdr_end=window.get("end_monotonic") if hdr_valid else None,
            hdr_p99_ms=window.get("p99_ms") if hdr_valid else None,
            frontend_rps=window.get("rps") if hdr_valid else None,
            completion_fraction=window.get("completion_fraction") if hdr_valid else None,
            request_errors=sum(window["errors"].values()) if hdr_valid else None,
            valid=bool(not frontend or matched_probe))
        self.previous, self.previous_system, self.previous_time = observed, system, now
        return result
