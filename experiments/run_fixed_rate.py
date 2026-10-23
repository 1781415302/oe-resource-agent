"""Compare fixed offered load with aligned HDR, throughput and batch intervals."""
import argparse
import datetime
import hashlib
import json
import os
import random
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]
from eulerbench.environment import ensure_port_free, inspect_environment
from eulerbench.metrics import BackgroundReader, Sampler, resource_summary
from eulerbench.processes import ProcessManager
from eulerbench.runner import _prepare_nginx, _wait_ready
from oeagent.backend import sched_state
from oeagent.business import business_window, parse_wrk2
from oeagent.protocol import ToolError
from oeagent.runtime import AgentRuntime, ResourceLock, load_profile
from run_dynamic import source_hashes, write


def episode(directory, condition, cycle, profile, agent_profile, wrk):
    directory.mkdir(parents=True)
    mode = "fixed" if condition.startswith(("fixed-", "preempt-")) else condition
    preempt = condition.startswith("preempt-")
    runtime = AgentRuntime(agent_profile, directory, mode=mode,
                           fixed_slice_us=1000 if mode == "fixed" else None, emit_changes=False, fixed_wakeup_preempt=preempt)
    controller_errors = []
    def run():
        try:
            runtime.run(profile["windows"] * (profile["window_seconds"] + 20) + 60)
        except BaseException as exc:
            controller_errors.append({"type": type(exc).__name__, "message": str(exc)})
    control = threading.Thread(target=run)
    result = {"schema_version": 1, "condition": condition, "cycle": cycle, "valid": False, "status": "running", "windows": []}
    sampler = Sampler(directory / "samples.jsonl", os.sysconf("SC_CLK_TCK"))
    reader = None
    with (directory / "driver-events.jsonl").open("w") as events:
        manager = ProcessManager(events, directory)
        pids = {"controller": os.getpid()}
        try:
            ensure_port_free(profile["port"])
            config, page = _prepare_nginx(directory, profile)
            result["page_sha256"] = hashlib.sha256(page).hexdigest()
            server = manager.start("server", [shutil.which("nginx"), "-p", str(directory) + "/", "-c", str(config)],
                                   profile["service_cpus"], directory / "nginx.log")
            url = f"http://127.0.0.1:{profile['port']}/index.html"
            _wait_ready(server, url, page)
            batch = manager.start("background", [shutil.which("sysbench"), "cpu", "--threads=1", "--cpu-max-prime=10000",
                "--time=3600", "--report-interval=1", "run"], profile["service_cpus"], directory / "background.log", pipe=True)
            reader = BackgroundReader(batch.stdout, directory / "background.log", directory / "background-intervals.jsonl")
            pids.update(server=server.pid, background=batch.pid)
            key = hashlib.sha256(json.dumps({"server_pid": server.pid, "batch_pid": batch.pid, "rate": profile["rate"],
                "connections": profile["connections"], "page_sha256": result["page_sha256"], "service_cpus": profile["service_cpus"]}, sort_keys=True).encode()).hexdigest()
            control.start()
            if mode == "fixed":
                until = time.monotonic() + 10
                while runtime.policy_snapshot()["policy_id"] != "scx.slice" and time.monotonic() < until and not controller_errors:
                    time.sleep(.02)
                ready = runtime.policy_snapshot()
                if ready["slice_us"] != 1000 or ready.get("wakeup_preempt", False) != preempt:
                    raise RuntimeError(f"Fixed reference did not initialize: {controller_errors}")
            for index in range(profile["windows"]):
                before = runtime.policy_snapshot()
                log = directory / f"window-{index + 1:02d}.log"
                client = manager.start("client", [wrk["binary"], "-t1", f"-c{profile['connections']}", f"-d{profile['window_seconds']}s",
                    f"-R{profile['rate']}", "-L", "-U", "-s", "/root/euler-bench/scripts/wrk_stats.lua", url], profile["client_cpus"], log)
                pids["client"] = client.pid
                deadline, next_sample = time.monotonic() + profile["window_seconds"] + 20, 0
                while client.poll() is None:
                    if time.monotonic() > deadline or controller_errors or not control.is_alive() or server.poll() is not None or batch.poll() is not None:
                        raise RuntimeError(f"Client/workload/controller failed: {controller_errors}")
                    if time.monotonic() >= next_sample:
                        native = runtime.backend.process
                        if native is not None and native.poll() is None:
                            pids["native"] = native.pid
                        else:
                            pids.pop("native", None)
                        sampler.sample(f"window-{index + 1}", pids)
                        next_sample = time.monotonic() + profile["sample_seconds"]
                    time.sleep(.03)
                if client.returncode:
                    raise RuntimeError(f"wrk2 exited {client.returncode}")
                manager.stop(client)
                pids.pop("client", None)
                frontend = parse_wrk2(log)
                if any(frontend["errors"].values()) or any(frontend["whole_run"]["errors"].values()):
                    raise RuntimeError("Client request errors")
                if frontend["offered_rps"] != profile["rate"]:
                    raise RuntimeError("Actual offered rate differs from profile")
                bg = reader.window(frontend["start_monotonic"], frontend["end_monotonic"])
                if bg["events_per_sec"] is None:
                    raise RuntimeError("Aligned background intervals unavailable")
                policy_after_client = runtime.policy_snapshot()
                if before["revision"] != policy_after_client["revision"] or frontend["start_monotonic"] < before["policy_since_monotonic"]:
                    raise RuntimeError("Measured HDR window crossed a policy action")
                response = None
                metric = business_window(frontend, bg, before, key, f"cycle-{cycle}-{condition}-window-{index + 1}")
                if mode == "feedback":
                    until = time.monotonic() + 4
                    while True:
                        try:
                            response = runtime.submit_metrics(metric)
                            break
                        except ToolError as exc:
                            if exc.code != "OBSERVATION_COVERAGE_INSUFFICIENT" or time.monotonic() > until:
                                raise
                            time.sleep(.05)
                row = {"index": index + 1, "frontend": frontend, "background": bg,
                       "policy_measured": before, "metric_submission": metric, "feedback_response": response,
                       "policy_after": runtime.policy_snapshot(),
                       "resources": resource_summary(directory / "samples.jsonl", frontend["start_monotonic"], frontend["end_monotonic"])}
                result["windows"].append(row)
                write(directory / "result.json", result)
            if profile.get("verify_feedback_timeout"):
                if runtime.policy_snapshot()["policy_id"] != "scx.slice":
                    raise RuntimeError("Fault test did not enter SCX probe")
                started = time.monotonic()
                until = started + agent_profile["feedback"]["feedback_timeout_seconds"] + 5
                while runtime.policy_snapshot()["policy_id"] == "scx.slice" and time.monotonic() < until:
                    if controller_errors or not control.is_alive():
                        raise RuntimeError("Controller failed during feedback timeout test")
                    time.sleep(.05)
                evidence_events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
                timed_out = any(row["event"] == "feedback_decided" and row["decision"]["reason"] == "business_feedback_timeout" for row in evidence_events)
                if not timed_out or runtime.policy_snapshot()["policy_id"] != "kernel.default" or sched_state() != "disabled":
                    raise RuntimeError("Missing business feedback did not restore default")
                result["feedback_timeout_verified"] = {"passed": True, "elapsed_seconds": time.monotonic() - started,
                    "note": "native heartbeats continue; workload fallback confirmation deliberately delayed"}
            result.update(status="completed", valid=True)
        except BaseException as exc:
            result.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        finally:
            runtime.stop_requested = True
            if control.ident:
                control.join(timeout=20)
            cleanup = manager.stop_all()
            if reader:
                reader.finish()
            sampler.close()
            result.update(controller_errors=controller_errors, cleanup_errors=cleanup, final_sched_ext=sched_state())
            event_path = directory / "events.jsonl"
            agent_events = [json.loads(line) for line in event_path.read_text().splitlines()] if event_path.is_file() else []
            result["actions"] = [row for row in agent_events if row["event"] == "policy_applied"]
            native_audits = []
            stops = [row for row in agent_events if row["event"] == "backend_stopped"]
            for path in directory.glob("native-*.log"):
                lines = path.read_text().splitlines()
                stats = [json.loads(line.split(" ", 1)[1]) for line in lines if line.startswith("OE_ADAPTIVE_STATS ")]
                stop = [row for row in stops if row["pid"] == int(path.stem.split("-")[1])]
                native_audits.append({"log": path.name, "passed": bool(stats) and all(row["accounting_errors"] == 0 and row["slice_ns"] == 1000000 and (mode != "fixed" or row.get("wakeup_preempt",False) == preempt) for row in stats)
                    and len(stop) == 1 and stop[0]["returncode"] == 0 and any("EXIT: unregistered from user space" in line for line in lines)})
            result["native_audits"] = native_audits
            if (controller_errors or cleanup or control.is_alive() or sched_state() != "disabled"
                or any(not row["passed"] for row in native_audits) or (mode == "fixed" and not native_audits)
                or (mode == "default" and native_audits)):
                result.update(valid=False, status="failed")
            write(directory / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["smoke", "quick", "survey"], default="quick")
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("fixed-rate-profile.json"))
    args = parser.parse_args()
    def stop(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    profile = json.loads(args.profile.read_text())
    if args.mode == "smoke":
        profile.update(repeats=1, windows=1, window_seconds=20)
    elif args.mode == "survey":
        profile.update(repeats=1, windows=1, conditions=["default", "fixed-1000"])
    if not {0, 1, 2}.issubset(os.sched_getaffinity(0)):
        raise SystemExit("CPU0/1/2 unavailable")
    native = json.loads((ROOT / "state/build.json").read_text())
    wrk = json.loads((ROOT / "state/wrk2-build.json").read_text())
    if hashlib.sha256(Path(wrk["binary"]).read_bytes()).hexdigest() != wrk["binary_sha256"]:
        raise SystemExit("wrk2 binary changed")
    source = source_hashes()
    agent_profile = load_profile(ROOT / "profiles/nginx-slo.json")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = ROOT / "results" / f"{stamp}-fixed-rate-{args.mode}-{os.getpid()}"
    directory.mkdir(parents=True)
    write(directory / "frozen-profile.json", profile)
    write(directory / "agent-profile.json", agent_profile)
    write(directory / "source-snapshot.json", {"sha256": source, "native": native, "wrk2": wrk,
          "environment": inspect_environment(str(native["binary"]))})
    for name in source:
        target = directory / "source-files" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    shutil.copyfile(ROOT / "state/wrk2-measurement.patch", directory / "wrk2-measurement.patch")
    os.sched_setaffinity(0, set(profile["controller_cpus"]))
    rng, schedule = random.Random(profile["seed"]), []
    for cycle in range(1, profile["repeats"] + 1):
        conditions = list(profile["conditions"])
        rng.shuffle(conditions)
        schedule.extend({"cycle": cycle, "condition": condition} for condition in conditions)
    write(directory / "schedule.json", schedule)
    print(f"固定速率实验：{directory}", flush=True)
    results = []
    with ResourceLock():
        if sched_state() != "disabled":
            raise SystemExit("Foreign scheduler active")
        for index, item in enumerate(schedule, 1):
            if source_hashes() != source:
                raise RuntimeError("Executed source changed")
            print(f"[{index}/{len(schedule)}] {item}", flush=True)
            result = episode(directory / f"episode-{index:03d}", item["condition"], item["cycle"], profile, agent_profile, wrk)
            results.append(result)
            write(directory / "results.json", results)
            print(f"  {result['status']} {result.get('error', '')}", flush=True)
            if result.get("error", {}).get("type") in ("KeyboardInterrupt", "SystemExit"):
                raise KeyboardInterrupt("Stopped after cleanup")
        if source_hashes() != source:
            raise RuntimeError("Executed source changed")
    write(directory / "summary.json", {"episodes": len(results), "valid_episodes": sum(row["valid"] for row in results),
        "windows": sum(len(row["windows"]) for row in results), "final_sched_ext": sched_state()})
    print(f"完成：{sum(row['valid'] for row in results)}/{len(results)}；{directory}", flush=True)
    return 0 if all(row["valid"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
