"""Functional loop check with real nginx/wrk/sysbench phase changes."""
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = Path("/root/euler-bench")
sys.path[:0] = [str(ROOT), str(BENCH)]
from eulerbench.metrics import parse_wrk
from eulerbench.processes import ProcessManager
from eulerbench.runner import _prepare_nginx, _wait_ready
from eulerbench.environment import ensure_port_free
from oeagent.backend import sched_state
from oeagent.runtime import AgentRuntime, ResourceLock, load_profile


def main():
    if not {0, 1, 2}.issubset(os.sched_getaffinity(0)):
        raise SystemExit("此功能用例需要 CPU0/1/2。")
    os.sched_setaffinity(0, {2})
    directory = ROOT / "results" / f"workload-loop-{time.time_ns()}"
    directory.mkdir(parents=True)
    profile = load_profile(ROOT / "profiles/nginx-cpu.json")
    runtime = AgentRuntime(profile, directory)
    evidence = {"schema_version": 1, "status": "running", "scope": "functional real-workload transition check; not a performance comparison",
                "profile": profile, "directory": str(directory)}
    errors = []
    def worker():
        try:
            runtime.run(35)
        except BaseException as exc:
            errors.append({"type": type(exc).__name__, "message": str(exc)})
    thread = threading.Thread(target=worker)
    try:
        with ResourceLock(), (directory / "driver-events.jsonl").open("a", encoding="utf-8") as stream:
            manager = ProcessManager(stream, directory)
            try:
                fixture = {"port": 18081, "page_bytes": 4096, "service_cpus": [0]}
                ensure_port_free(fixture["port"])
                config, page = _prepare_nginx(directory, fixture)
                server = manager.start("server", [shutil.which("nginx"), "-p", str(directory) + "/", "-c", str(config)], [0], directory / "nginx.log")
                url = "http://127.0.0.1:18081/index.html"
                _wait_ready(server, url, page)
                thread.start()
                client = manager.start("client", [shutil.which("wrk"), "-t1", "-c32", "--latency", "--timeout", "2s",
                                       "-s", str(BENCH / "scripts/wrk_stats.lua"), "-d33s", url], [1], directory / "wrk.log")
                time.sleep(8)
                background = manager.start("background", [shutil.which("sysbench"), "cpu", "--threads=1", "--cpu-max-prime=10000",
                                           "--time=30", "--report-interval=1", "run"], [0], directory / "background.log")
                evidence["background_started_monotonic"] = time.monotonic()
                time.sleep(13)
                manager.stop(background)
                evidence["background_stopped_monotonic"] = time.monotonic()
                client.wait(timeout=20)
                if client.returncode:
                    raise RuntimeError("wrk exited unsuccessfully")
                thread.join(timeout=15)
                if thread.is_alive() or errors:
                    raise RuntimeError(f"Agent loop did not finish cleanly: {errors}")
                events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
                applied = [row for row in events if row["event"] == "policy_applied"]
                policies = [row["after"]["policy_id"] for row in applied]
                if "scx.slice" not in policies or not policies or policies[-1] != "kernel.default":
                    raise RuntimeError(f"Expected actual default->SCX->default transitions; found {policies}")
                frontend = parse_wrk(directory / "wrk.log")
                if any(frontend["errors"].values()):
                    raise RuntimeError("HTTP load contained errors")
                evidence.update(status="passed", applied=applied, frontend_aggregate=frontend,
                                observation_windows=sum(row["event"] == "workload_observed" for row in events),
                                page_sha256=hashlib.sha256(page).hexdigest(), final_sched_ext=sched_state())
            finally:
                runtime.stop_requested = True
                if thread.ident:
                    thread.join(timeout=20)
                cleanup = manager.stop_all()
                if cleanup:
                    raise RuntimeError(f"Owned workload cleanup failed: {cleanup}")
    except BaseException as exc:
        evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)}, agent_errors=errors,
                        final_sched_ext=sched_state())
        raise
    finally:
        (directory / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (ROOT / "state/workload-loop-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in evidence.items() if key not in ("profile", "frontend_aggregate")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
