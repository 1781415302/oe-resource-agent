"""One bounded guest cycle, called by the Windows host-quality orchestrator."""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), "/root/euler-bench"]
from eulerbench.common import boot_id
from eulerbench.environment import inspect_environment
from eulerbench.metrics import process_stats
from oeagent.backend import sched_state
from oeagent.runtime import ResourceLock, load_profile
from run_dynamic import source_hashes, write
from run_fixed_rate import episode


def run_path(key):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key):
        raise ValueError("Unsafe run key")
    return ROOT / "results" / ("controlled-" + key)


def stop_owned(key):
    """Only signal this saved process instance; never a global benchmark owner."""
    path = run_path(key) / "owner.json"
    if not path.is_file():
        return {"stopped": False, "reason": "owner_not_published"}
    record = json.loads(path.read_text())
    if record["run_key"] != key or record["boot_id"] != boot_id():
        raise RuntimeError("Owner identity mismatch")
    try:
        handle = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        return {"stopped": True, "reason": "already_exited"}
    try:
        current = process_stats(record["pid"])
        if not current or current["start_ticks"] != record["start_ticks"]:
            return {"stopped": True, "reason": "original_instance_exited"}
        signal.pidfd_send_signal(handle, signal.SIGTERM)
        import select
        exited = bool(select.select([handle], [], [], 45)[0])
        return {"stopped": exited, "reason": "owned_SIGTERM"}
    finally:
        os.close(handle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument("--conditions", default="default,fixed-1000,preempt-1000")
    parser.add_argument("--mode", choices=("smoke", "quick"), default="quick")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.stop:
        print(json.dumps(stop_owned(args.run_key)))
        return 0
    profile = json.loads((ROOT / "experiments/wakeup-profile.json").read_text())
    conditions = args.conditions.split(",")
    if sorted(conditions) != sorted(profile["conditions"]) or args.cycle < 1:
        raise ValueError("Cycle must contain each predeclared condition exactly once")
    if args.mode == "smoke":
        profile.update(windows=1, window_seconds=20)
    profile["repeats"] = 1
    directory = run_path(args.run_key)
    directory.mkdir(parents=True)  # refuse accidental overwrite
    archive = Path("/tmp") / ("oe-controlled-" + args.run_key + ".tar.gz")
    record = process_stats(os.getpid())
    write(directory / "owner.json", {"run_key": args.run_key, "pid": os.getpid(),
        "start_ticks": record["start_ticks"], "boot_id": boot_id()})
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"Received signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    summary = {"status": "running", "cycle": args.cycle, "run_key": args.run_key,
        "guest_start_utc_seconds": time.time(), "performance_claim": None}
    results = []
    try:
        if not {0, 1, 2}.issubset(os.sched_getaffinity(0)):
            raise RuntimeError("CPU0/1/2 unavailable")
        native, wrk = (json.loads((ROOT / ("state/" + name + ".json")).read_text()) for name in ("build", "wrk2-build"))
        for path, expected in ((native["binary"], native["binary_sha256"]), (wrk["binary"], wrk["binary_sha256"])):
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise RuntimeError("Compiled binary hash mismatch")
        source = source_hashes()
        subprocess.run([sys.executable, str(ROOT / "tools/audit_python_cache.py")], check=True, stdout=subprocess.DEVNULL)
        shutil.copyfile(ROOT / "state/python-cache-audit.json", directory / "python-cache-audit.json")
        agent_profile = load_profile(ROOT / "profiles/nginx-slo.json")
        write(directory / "frozen-profile.json", profile)
        write(directory / "agent-profile.json", agent_profile)
        write(directory / "source-snapshot.json", {"sha256": source, "native": native, "wrk2": wrk,
            "environment": inspect_environment(native["binary"])})
        for name in source:
            target = directory / "source-files" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        shutil.copyfile(ROOT / "state/wrk2-measurement.patch", directory / "wrk2-measurement.patch")
        schedule = [{"cycle": args.cycle, "condition": value} for value in conditions]
        write(directory / "schedule.json", schedule)
        os.sched_setaffinity(0, set(profile["controller_cpus"]))
        with ResourceLock():
            if sched_state() != "disabled":
                raise RuntimeError("Another SCX scheduler is active")
            for index, item in enumerate(schedule, 1):
                if source_hashes() != source:
                    raise RuntimeError("Executed source changed")
                print(json.dumps({"episode": index, **item}), flush=True)
                row = episode(directory / f"episode-{index:03d}", item["condition"], args.cycle, profile, agent_profile, wrk)
                results.append(row)
                write(directory / "results.json", results)
                print(json.dumps({"episode": index, "valid": row["valid"], "error": row.get("error")}), flush=True)
                if row.get("error", {}).get("type") in ("KeyboardInterrupt", "SystemExit"):
                    raise KeyboardInterrupt("Interrupted episode cleaned up")
            if source_hashes() != source:
                raise RuntimeError("Executed source changed")
        summary["status"] = "completed" if all(row["valid"] for row in results) else "failed"
    except BaseException as exc:
        summary.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        summary.update(episodes=len(results), valid_episodes=sum(row["valid"] for row in results),
            windows=sum(len(row["windows"]) for row in results), final_sched_ext=sched_state(),
            guest_end_utc_seconds=time.time())
        write(directory / "summary.json", summary)
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(directory, arcname=directory.name)
        print(json.dumps({"summary": summary, "archive": str(archive),
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}), flush=True)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
