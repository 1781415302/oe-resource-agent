"""Real dynamic comparisons with merged histograms and explicit transition windows."""
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
BENCH = Path("/root/euler-bench")
sys.path[:0] = [str(ROOT), str(BENCH)]
from eulerbench.environment import ensure_port_free, inspect_environment
from eulerbench.metrics import BackgroundReader, Sampler, parse_wrk, resource_summary
from eulerbench.processes import ProcessManager
from eulerbench.runner import _prepare_nginx, _wait_ready
from oeagent.backend import sched_state
from oeagent.histogram import percentile, summarize
from oeagent.runtime import AgentRuntime, ResourceLock, load_profile


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def source_hashes():
    files = []
    for folder in ("oeagent", "native", "scripts", "experiments", "profiles"):
        files.extend(path for path in (ROOT / folder).rglob("*") if path.suffix in (".py", ".c", ".h", ".lua", ".json"))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


def audit_native(directory, condition, agent_events):
    """Reject a silent fallback or accounting failure from a nominally completed run."""
    stops = [row for row in agent_events if row["event"] == "backend_stopped"]
    checks = []
    for path in sorted(directory.glob("native-*.log")):
        lines = path.read_text().splitlines()
        configs = [json.loads(line.split(" ", 1)[1]) for line in lines if line.startswith("OE_ADAPTIVE_CONFIG ")]
        stats = [json.loads(line.split(" ", 1)[1]) for line in lines if line.startswith("OE_ADAPTIVE_STATS ")]
        pid = int(path.stem.split("-")[1])
        own_stop = [row for row in stops if row["pid"] == pid]
        expected = int(condition.split("-", 1)[1]) * 1000 if condition.startswith("fixed-") else 1000000
        passed = (len(configs) == 1 and configs[0]["slice_ns"] == expected and bool(stats)
                  and all(row["accounting_errors"] == 0 and row["slice_ns"] == expected for row in stats)
                  and len(own_stop) == 1 and own_stop[0]["returncode"] == 0
                  and any("EXIT: unregistered from user space" in line for line in lines))
        checks.append({"log": path.name, "pid": pid, "passed": passed,
                       "stats_records": len(stats), "expected_slice_ns": expected})
    needs_backend = condition.startswith("fixed-") or condition == "adaptive"
    return {"passed": (bool(checks) if needs_backend else not checks) and all(row["passed"] for row in checks),
            "checks": checks}


def episode(directory, condition, cycle, profile, agent_profile):
    directory.mkdir(parents=True)
    native_mode = "fixed" if condition.startswith("fixed-") else condition
    fixed = int(condition.split("-", 1)[1]) if native_mode == "fixed" else None
    runtime = AgentRuntime(agent_profile, directory, mode=native_mode, fixed_slice_us=fixed)
    thread_errors = []
    duration = profile["warmup_seconds"] + sum(p["windows"] for p in profile["phases"]) * (profile["window_seconds"] + 15) + 30
    def worker():
        try:
            runtime.run(duration)
        except BaseException as exc:
            thread_errors.append({"type": type(exc).__name__, "message": str(exc)})
    control = threading.Thread(target=worker)
    result = {"schema_version": 1, "condition": condition, "cycle": cycle, "status": "running", "valid": False,
              "windows": [], "phases": [], "directory": str(directory)}
    sampler = Sampler(directory / "samples.jsonl", os.sysconf("SC_CLK_TCK"))
    reader, background, client, server = None, None, None, None
    previous_end = None
    with (directory / "driver-events.jsonl").open("a", encoding="utf-8") as events:
        manager = ProcessManager(events, directory)
        pids = {"controller": os.getpid()}
        try:
            ensure_port_free(profile["port"])
            config, page = _prepare_nginx(directory, profile)
            result["page_sha256"] = hashlib.sha256(page).hexdigest()
            server = manager.start("server", [shutil.which("nginx"), "-p", str(directory) + "/", "-c", str(config)],
                                   profile["service_cpus"], directory / "nginx.log")
            pids["server"] = server.pid
            url = f"http://127.0.0.1:{profile['port']}/index.html"
            _wait_ready(server, url, page)
            control.start()
            if native_mode == "fixed":
                until = time.monotonic() + 10
                while runtime.current["policy_id"] != "scx.slice" and time.monotonic() < until and not thread_errors:
                    time.sleep(0.02)
                if runtime.current["slice_us"] != fixed:
                    raise RuntimeError(f"Fixed policy did not initialize: {thread_errors}")
            def run_client(seconds, logfile, phase):
                nonlocal client
                client = manager.start("client", [shutil.which("wrk"), "-t1", f"-c{profile['connections']}",
                                       "--latency", "--timeout", "2s", "-s", str(ROOT / "scripts/wrk_histogram.lua"),
                                       f"-d{seconds}s", url], profile["client_cpus"], logfile)
                pids["client"] = client.pid
                deadline = time.monotonic() + seconds + 20
                next_sample = 0
                while client.poll() is None:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Window client timed out")
                    if thread_errors or not control.is_alive() or server.poll() is not None:
                        raise RuntimeError(f"Controller/server exited: {thread_errors}")
                    if background is not None and background.poll() is not None:
                        raise RuntimeError("Background process exited during active phase")
                    if time.monotonic() >= next_sample:
                        native_process = runtime.backend.process
                        if native_process is not None and native_process.poll() is None:
                            pids["native"] = native_process.pid
                        else:
                            pids.pop("native", None)
                        sample = sampler.sample(phase, pids)
                        if native_mode in ("default", "fixed"):
                            expected_state = "disabled" if native_mode == "default" else "enabled"
                            if sample["sched_ext"] != expected_state:
                                raise RuntimeError(f"Frozen reference changed state: {sample['sched_ext']}")
                        next_sample = time.monotonic() + profile["sample_seconds"]
                    time.sleep(0.03)
                if client.returncode:
                    raise RuntimeError(f"Client exit {client.returncode}")
                manager.stop(client)
                pids.pop("client", None)
                return parse_wrk(logfile)
            if profile["warmup_seconds"]:
                run_client(profile["warmup_seconds"], directory / "warmup.log", "warmup")
            for phase in profile["phases"]:
                if phase["background_threads"] and background is None:
                    background = manager.start("background", [shutil.which("sysbench"), "cpu",
                        f"--threads={phase['background_threads']}", "--cpu-max-prime=10000", "--time=3600", "--report-interval=1", "run"],
                        profile["service_cpus"], directory / f"{phase['id']}-background.log", pipe=True)
                    reader = BackgroundReader(background.stdout, directory / f"{phase['id']}-background.log", directory / f"{phase['id']}-background-intervals.jsonl")
                    pids["background"] = background.pid
                if not phase["background_threads"] and background is not None:
                    manager.stop(background)
                    reader.finish()
                    pids.pop("background", None)
                    background, reader = None, None
                phase_started = time.monotonic()
                for index in range(phase["windows"]):
                    policy_before = runtime.policy_snapshot()
                    before = time.monotonic()
                    log = directory / f"{phase['id']}-window-{index + 1:02d}.log"
                    frontend = run_client(profile["window_seconds"], log, phase["id"])
                    after = time.monotonic()
                    if any(frontend["errors"].values()):
                        raise RuntimeError("Request/socket errors in window")
                    histogram = frontend.get("histogram_us")
                    if abs(percentile(histogram, 99) - frontend["p99_us"]) > 1:
                        raise RuntimeError("Exported histogram percentile differs from wrk")
                    start, end = frontend["start_monotonic_estimate"], frontend["finished_monotonic"]
                    background_metrics = reader.window(start, end) if reader else None
                    if reader and background_metrics["events_per_sec"] is None:
                        raise RuntimeError("Background interval alignment unavailable")
                    policy_after = runtime.policy_snapshot()
                    row = {"phase_id": phase["id"], "window": index + 1, "frontend": frontend,
                           "background": background_metrics, "controller_before": policy_before, "controller_after": policy_after,
                           "transition_window": policy_before["revision"] != policy_after["revision"],
                           "wall_seconds": after - before, "start_monotonic": start, "end_monotonic": end,
                           "gap_since_previous_window_seconds": max(0, start - previous_end) if previous_end is not None else None,
                           "export_and_cleanup_seconds": max(0, after - end),
                           "resources": resource_summary(directory / "samples.jsonl", start, end)}
                    result["windows"].append(row)
                    previous_end = end
                    write(directory / "result.json", result)
                windows = [row for row in result["windows"] if row["phase_id"] == phase["id"]]
                counts = summarize(row["frontend"]["histogram_us"] for row in windows)
                active_seconds = sum(row["frontend"]["duration_seconds"] for row in windows)
                requests = sum(row["frontend"]["requests"] for row in windows)
                backgrounds = [row["background"] for row in windows if row["background"]]
                phase_ended = time.monotonic()
                result["phases"].append({"phase_id": phase["id"], "requests": requests, "active_seconds": active_seconds,
                    "start_monotonic": phase_started, "end_monotonic": phase_ended,
                    "wall_seconds": phase_ended - phase_started, "active_rps": requests / active_seconds,
                    "latency": counts, "p99_slo_pass_windows": sum(row["frontend"]["p99_ms"] <= profile["p99_target_ms"] for row in windows),
                    "window_count": len(windows), "transition_windows": sum(row["transition_window"] for row in windows),
                    "background_eps": sum(row["estimated_events"] for row in backgrounds) / sum(row["covered_seconds"] for row in backgrounds) if backgrounds else None})
            result.update(valid=True, status="completed", thread_cpu_seconds=runtime.thread_cpu_seconds)
        except BaseException as exc:
            result.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)}, thread_errors=thread_errors)
        finally:
            runtime.stop_requested = True
            if control.ident:
                control.join(timeout=20)
            if control.is_alive():
                result.update(valid=False, status="failed", error={"message": "Controller thread did not stop"})
            cleanup = manager.stop_all()
            if reader:
                reader.finish()
            sampler.close()
            result["thread_cpu_seconds"] = runtime.thread_cpu_seconds
            result["thread_errors"] = thread_errors
            result["cleanup_errors"] = cleanup
            result["final_sched_ext"] = sched_state()
            if cleanup or sched_state() != "disabled":
                result.update(valid=False, status="failed")
            events_path = directory / "events.jsonl"
            agent_events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.is_file() else []
            result["actions"] = [row for row in agent_events if row["event"] == "policy_applied"]
            result["native_audit"] = audit_native(directory, condition, agent_events)
            if thread_errors or not result["native_audit"]["passed"]:
                result.update(valid=False, status="failed")
            for window in result["windows"]:
                window["transition_window"] = any(
                    action["action_started_monotonic"] < window["end_monotonic"]
                    and action["action_finished_monotonic"] > window["start_monotonic"]
                    for action in result["actions"])
            for phase in result["phases"]:
                phase["transition_windows"] = sum(row["transition_window"] for row in result["windows"] if row["phase_id"] == phase["phase_id"])
            result["scope"] = "phase percentiles merge reported histogram counts; transitions included"
            write(directory / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["smoke", "quick"], default="quick")
    parser.add_argument("--profile", type=Path, default=Path(__file__).with_name("dynamic-profile.json"))
    args = parser.parse_args()
    def stop(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    profile = json.loads(args.profile.read_text())
    if args.mode == "smoke":
        profile.update(repeats=1, window_seconds=3, warmup_seconds=1, cooldown_seconds=1)
        profile["phases"] = [{**phase, "windows": 2} for phase in profile["phases"]]
    if not {0, 1, 2}.issubset(os.sched_getaffinity(0)):
        raise SystemExit("Dynamic benchmark needs CPU0/1/2")
    initial_affinity = sorted(os.sched_getaffinity(0))
    agent_profile = load_profile(ROOT / "profiles/nginx-cpu.json")
    source = source_hashes()
    native = json.loads((ROOT / "state/build.json").read_text())
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = ROOT / "results" / f"{stamp}-dynamic-{args.mode}-{os.getpid()}"
    directory.mkdir(parents=True)
    write(directory / "frozen-profile.json", profile)
    write(directory / "agent-profile.json", agent_profile)
    environment = inspect_environment(str(native["binary"]))
    environment["controller_cpus_after_snapshot"] = profile["controller_cpus"]
    environment["initial_affinity"] = initial_affinity
    write(directory / "source-snapshot.json", {"sha256": source, "native": native, "environment": environment})
    os.sched_setaffinity(0, set(profile["controller_cpus"]))
    rng = random.Random(profile["seed"])
    schedule = []
    for cycle in range(1, profile["repeats"] + 1):
        conditions = list(profile["conditions"])
        rng.shuffle(conditions)
        schedule.extend({"cycle": cycle, "condition": condition} for condition in conditions)
    write(directory / "schedule.json", schedule)
    results = []
    print(f"动态实验目录：{directory}", flush=True)
    with ResourceLock():
        if sched_state() != "disabled":
            raise SystemExit("sched_ext must be disabled before experiment")
        for index, item in enumerate(schedule, 1):
            if source_hashes() != source:
                raise RuntimeError("Executed source changed during frozen experiment")
            print(f"[{index}/{len(schedule)}] cycle {item['cycle']} / {item['condition']}", flush=True)
            result = episode(directory / f"episode-{index:03d}", item["condition"], item["cycle"], profile, agent_profile)
            results.append(result)
            print(f"  {result['status']}", flush=True)
            write(directory / "results.json", results)
            if result.get("error", {}).get("type") in ("KeyboardInterrupt", "SystemExit"):
                raise KeyboardInterrupt("Experiment interrupted after cleanup")
            time.sleep(profile["cooldown_seconds"])
        if source_hashes() != source:
            raise RuntimeError("Executed source changed during frozen experiment")
    write(directory / "summary.json", {"schema_version": 1, "mode": args.mode,
        "episodes": len(results), "valid_episodes": sum(row["valid"] for row in results),
        "measurement_windows": sum(len(row["windows"]) for row in results),
        "final_sched_ext": sched_state(), "scope": "dynamic exploratory workload comparison"})
    print(f"动态实验完成：{sum(row['valid'] for row in results)}/{len(results)}；{directory}", flush=True)
    return 0 if all(row["valid"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
