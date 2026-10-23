"""Live off/on/off workload check; counter proof is not performance proof."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]
from eulerbench.environment import ensure_port_free
from eulerbench.metrics import parse_wrk
from eulerbench.processes import ProcessManager
from eulerbench.runner import _prepare_nginx, _wait_ready
from oeagent.backend import NativeBackend, sched_state
from oeagent.runtime import EventLog, ResourceLock


def main():
    directory = ROOT / "results" / f"wakeup-validation-{time.time_ns()}"
    log = EventLog(directory)
    backend = NativeBackend(log, directory, lease_seconds=30)
    evidence = {"schema_version": 1, "status": "running", "directory": str(directory), "checks": [],
                "scope": "real load and off/on/off counter validation; not a performance comparison"}
    os.sched_setaffinity(0, {2})
    try:
        with ResourceLock(), (directory / "driver-events.jsonl").open("w") as events:
            manager = ProcessManager(events, directory)
            try:
                ensure_port_free(18087)
                config, page = _prepare_nginx(directory, {"port": 18087, "page_bytes": 4096, "service_cpus": [0]})
                server = manager.start("server", [shutil.which("nginx"), "-p", str(directory) + "/", "-c", str(config)], [0], directory / "nginx.log")
                url = "http://127.0.0.1:18087/index.html"
                _wait_ready(server, url, page)
                first = backend.start(1000, False)
                batch = manager.start("background", [shutil.which("sysbench"), "cpu", "--threads=1", "--time=40", "--cpu-max-prime=10000", "run"], [0], directory / "background.log")
                client = manager.start("client", [shutil.which("wrk"), "-t1", "-c32", "-d26s", "--latency", "-s",
                    "/root/euler-bench/scripts/wrk_stats.lua", url], [1], directory / "wrk.log")
                def wait(seconds):
                    until = time.monotonic() + seconds
                    while time.monotonic() < until:
                        if backend.process.poll() is not None or server.poll() is not None or batch.poll() is not None:
                            raise RuntimeError("Owned scheduler/workload exited")
                        time.sleep(.05)
                native = directory / f"native-{first['pid']}.log"
                def last_stats():
                    values = []
                    for line in native.read_text().splitlines():
                        if line.startswith("OE_ADAPTIVE_STATS "):
                            try:
                                values.append(json.loads(line.split(" ", 1)[1]))
                            except json.JSONDecodeError:
                                pass
                    if not values:
                        raise RuntimeError("No native counters")
                    return values[-1]
                wait(6)
                off = last_stats()
                if first["wakeup_preempt"] or off["preempt_kicks"]:
                    raise RuntimeError("Disabled mode issued preempt kicks")
                evidence["checks"].append({"name": "default_off_has_no_kicks", "passed": True, "stats": off})
                enabled = backend.update(1000, True)
                wait(10)
                on = last_stats()
                if not enabled["wakeup_preempt"] or on["preempt_kicks"] <= 0 or on["accounting_errors"]:
                    raise RuntimeError(f"Enabled policy did not issue valid kicks: {on}")
                evidence["checks"].append({"name": "real_short_wakeup_kicks", "passed": True, "stats": on})
                disabled = backend.update(1000, False)
                wait(3)
                stable = last_stats()
                wait(3)
                later = last_stats()
                if stable["wakeup_preempt"] or later["preempt_kicks"] != stable["preempt_kicks"]:
                    raise RuntimeError("Kicks continued after disable/quiescence")
                if not (first["pid"] == enabled["pid"] == disabled["pid"] and first["map_id"] == disabled["map_id"]):
                    raise RuntimeError("Policy toggle restarted backend")
                evidence["checks"].append({"name": "online_disable_stops_new_kicks_without_restart", "passed": True,
                    "before": first, "enabled": enabled, "disabled": disabled, "stats": later})
                client.wait(timeout=10)
                frontend = parse_wrk(directory / "wrk.log")
                if client.returncode or any(frontend["errors"].values()):
                    raise RuntimeError("Functional HTTP load failed")
                evidence["frontend"] = frontend
            finally:
                backend.stop()
                cleanup = manager.stop_all()
                if cleanup:
                    raise RuntimeError(str(cleanup))
        if sched_state() != "disabled":
            raise RuntimeError("Default scheduler restoration failed")
        evidence["checks"].append({"name": "default_restored", "passed": True})
        evidence.update(status="passed", final_sched_ext=sched_state())
    except BaseException as exc:
        evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        log.close()
        for path in (directory / "validation.json", ROOT / "state/wakeup-validation.json"):
            path.write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps({k: v for k, v in evidence.items() if k != "frontend"}, indent=2))


if __name__ == "__main__":
    main()
