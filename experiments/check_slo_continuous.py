"""Stage 2 verification: continuous-window wrk2 feeding online nginx-slo business feedback."""
import copy
import hashlib
import json
import os
import select
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]
from eulerbench.environment import ensure_port_free
from eulerbench.metrics import BackgroundReader, Sampler, resource_summary
from eulerbench.processes import ProcessManager
from eulerbench.runner import _prepare_nginx, _wait_ready
from oeagent.backend import sched_state
from oeagent.business import align_background_intervals, business_window, stream_window_to_frontend
from oeagent.protocol import ToolError
from oeagent.runtime import AgentRuntime, ResourceLock, load_profile


def main():
    if sched_state() != "disabled":
        raise RuntimeError("sched_ext must be disabled before starting check_slo_continuous")
    wrk_meta = json.loads((ROOT / "state/wrk2-window-build.json").read_text(encoding="utf-8"))
    wrk_bin = wrk_meta["binary"]
    directory = ROOT / "results" / f"slo-continuous-{time.time_ns()}"
    directory.mkdir(parents=True, exist_ok=True)

    agent_profile = copy.deepcopy(load_profile(ROOT / "profiles/nginx-wakeup-slo.json"))
    agent_profile["sample_seconds"] = 0.5
    agent_profile["feedback"]["baseline_confirmations"] = 2
    agent_profile["feedback"]["min_window_seconds"] = 4.5
    agent_profile["feedback"]["max_p99_ms"] = 2.5
    agent_profile["feedback"]["min_background_fraction"] = 0.80
    agent_profile["feedback"]["min_improvement_fraction"] = 0.05

    offered_rps = 25600
    connections = 32
    port = 18088
    service_cpus = [0]
    client_cpus = [1]
    controller_cpu = 2

    os.sched_setaffinity(0, {controller_cpu})
    runtime = AgentRuntime(agent_profile, directory, mode="feedback", emit_changes=False)
    controller_errors = []

    def _run_controller():
        try:
            runtime.run(120)
        except BaseException as exc:
            controller_errors.append({"type": type(exc).__name__, "message": str(exc)})

    control_thread = threading.Thread(target=_run_controller, daemon=True)
    sampler = Sampler(directory / "samples.jsonl", os.sysconf("SC_CLK_TCK"))
    reader = None
    evidence = {
        "schema_version": 1,
        "status": "running",
        "directory": str(directory),
        "offered_rps": offered_rps,
        "window_ms": 5000,
        "baseline_confirmations": 2,
        "windows": [],
    }

    with ResourceLock(), (directory / "driver-events.jsonl").open("w", encoding="utf-8") as devents:
        manager = ProcessManager(devents, directory)
        pids = {"controller": os.getpid()}
        try:
            ensure_port_free(port)
            bench_profile = {"port": port, "page_bytes": 4096, "service_cpus": service_cpus}
            config, page = _prepare_nginx(directory, bench_profile)
            page_sha256 = hashlib.sha256(page).hexdigest()
            server = manager.start("server", [shutil.which("nginx"), "-p", str(directory) + "/", "-c", str(config)],
                                   service_cpus, directory / "nginx.log")
            url = f"http://127.0.0.1:{port}/index.html"
            _wait_ready(server, url, page)
            batch = manager.start("background", [shutil.which("sysbench"), "cpu", "--threads=1",
                                                 "--cpu-max-prime=10000", "--time=3600", "--report-interval=1", "run"],
                                  service_cpus, directory / "background.log", pipe=True)
            reader = BackgroundReader(batch.stdout, directory / "background.log", directory / "background-intervals.jsonl")
            pids.update(server=server.pid, background=batch.pid)
            workload_key = hashlib.sha256(json.dumps({
                "server_pid": server.pid, "batch_pid": batch.pid, "rate": offered_rps,
                "connections": connections, "page_sha256": page_sha256, "service_cpus": service_cpus
            }, sort_keys=True).encode()).hexdigest()

            control_thread.start()
            time.sleep(1.5)

            client_log = (directory / "client.log").open("w", encoding="utf-8")
            client_argv = ["/usr/bin/env", "OE_WRK2_WINDOW_MS=5000", wrk_bin,
                           "-t1", f"-c{connections}", "-d45s", "-T3s", f"-R{offered_rps}", "-L", url]
            client = manager.start("client", client_argv, client_cpus, directory / "client-stderr.log", pipe=True)
            pids["client"] = client.pid

            pending = ""
            footer = None
            next_sample = 0.0
            deadline = time.monotonic() + 75.0
            fd = client.stdout.fileno()

            while True:
                if time.monotonic() > deadline or controller_errors:
                    raise RuntimeError(f"Continuous SLO run timed out or controller errored: {controller_errors}")
                if time.monotonic() >= next_sample:
                    native = runtime.backend.process
                    if native is not None and native.poll() is None:
                        pids["native"] = native.pid
                    else:
                        pids.pop("native", None)
                    sampler.sample("continuous", pids)
                    next_sample = time.monotonic() + 1.0

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
                text = chunk.decode("utf-8", errors="replace")
                client_log.write(text)
                client_log.flush()
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if not line.startswith("OE_WRK2_STREAM "):
                        continue
                    raw_json = line.split(" ", 1)[1]
                    record = json.loads(raw_json)
                    kind = record.get("kind")
                    if kind == "footer":
                        footer = record
                    elif kind == "window" and not record.get("final", False):
                        w_start = record["start_ns"] / 1e9
                        w_end = record["end_ns"] / 1e9
                        snap = runtime.policy_snapshot()
                        if w_start < snap["policy_since_monotonic"]:
                            evidence["windows"].append({
                                "seq": record["seq"],
                                "status": "skipped_policy_transition",
                                "start_monotonic": w_start,
                                "end_monotonic": w_end,
                                "policy_revision": snap["revision"],
                                "policy_since_monotonic": snap["policy_since_monotonic"]
                            })
                            continue
                        frontend = stream_window_to_frontend(record, offered_rps, raw_json.encode("utf-8"))
                        deadline_bg = time.monotonic() + 2.5
                        bg = align_background_intervals(reader.points, w_start, w_end)
                        while (bg["events_per_sec"] is None or bg["covered_seconds"] / (w_end - w_start) < 0.8) and time.monotonic() < deadline_bg:
                            time.sleep(0.1)
                            bg = align_background_intervals(reader.points, w_start, w_end)
                        metric = business_window(frontend, bg, snap, workload_key, f"stream-win-{record['seq']}")
                        wait_obs = time.monotonic() + 2.5
                        response = None
                        while True:
                            try:
                                response = runtime.submit_metrics(metric)
                                break
                            except ToolError as exc:
                                if exc.code != "OBSERVATION_COVERAGE_INSUFFICIENT" or time.monotonic() > wait_obs:
                                    raise
                                time.sleep(0.1)
                        evidence["windows"].append({
                            "seq": record["seq"],
                            "status": "submitted",
                            "policy_before": snap["policy_id"],
                            "revision_before": snap["revision"],
                            "frontend_p99_ms": frontend["p99_ms"],
                            "frontend_rps": round(frontend["rps"], 2),
                            "background_eps": round(bg["events_per_sec"], 2),
                            "background_covered_seconds": round(bg["covered_seconds"], 3),
                            "background_whole_covered_seconds": round(bg["whole_covered_seconds"], 3),
                            "decision": response["decision"],
                            "policy_after": response["actual"]["policy_id"],
                            "revision_after": response["actual"]["revision"],
                            "feedback_stage": response["feedback"]["stage"]
                        })
            client_log.close()
            manager.stop(client)
            pids.pop("client", None)
        finally:
            runtime.stop_requested = True
            if control_thread.ident:
                control_thread.join(timeout=20)
            cleanup_errors = manager.stop_all()
            if reader:
                reader.finish()
            sampler.close()

    submitted = [w for w in evidence["windows"] if w.get("status") == "submitted"]
    confirming = [w for w in submitted if w["decision"].get("reason") == "confirming_baseline_stability"]
    probed = [w for w in submitted if w["decision"].get("reason") == "baseline_business_goal_violated"]
    evaluated = [w for w in submitted if w["policy_before"] == "scx.slice"]

    evidence.update({
        "footer_export_duration_ns": footer.get("export_duration_ns") if footer else None,
        "agent_thread_cpu_seconds": round(runtime.thread_cpu_seconds, 6),
        "controller_errors": controller_errors,
        "cleanup_errors": cleanup_errors,
        "final_sched_ext": sched_state(),
        "summary": {
            "total_stream_windows": len(evidence["windows"]),
            "submitted_windows": len(submitted),
            "confirming_windows": len(confirming),
            "probe_triggered": bool(probed),
            "scx_evaluated_windows": len(evaluated),
            "baseline_p99_ms": probed[0]["frontend_p99_ms"] if probed else None,
            "scx_p99_ms": evaluated[0]["frontend_p99_ms"] if evaluated else None,
            "final_feedback_stage": submitted[-1]["feedback_stage"] if submitted else None
        }
    })
    if (controller_errors or cleanup_errors or evidence["final_sched_ext"] != "disabled"
            or not confirming or not probed or not evaluated):
        evidence["status"] = "failed"
        (ROOT / "state/slo-continuous-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError(f"Continuous SLO validation incomplete: {json.dumps(evidence['summary'])}")

    evidence["status"] = "passed"
    (ROOT / "state/slo-continuous-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
