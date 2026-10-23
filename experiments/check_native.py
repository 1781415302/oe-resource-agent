"""Guest integration checks for real load, online updates and lease rollback."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oeagent.backend import NativeBackend, sched_state
from oeagent.runtime import EventLog, ResourceLock


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    directory = ROOT / "results" / f"native-validation-{time.time_ns()}"
    log = EventLog(directory)
    checks = []
    evidence = {"schema_version": 1, "status": "running", "checks": checks, "directory": str(directory)}
    backend = NativeBackend(log, directory, lease_seconds=10)
    try:
        with ResourceLock():
            try:
                first = backend.start(20000)
                require(first["sched_ext"] == "enabled" and first["slice_us"] == 20000, "initial load/readback failed")
                checks.append({"name": "real_load_and_initial_readback", "passed": True, "state": first})
                second = backend.update(1000)
                third = backend.update(2000)
                require(first["pid"] == second["pid"] == third["pid"] and first["map_id"] == third["map_id"], "backend restarted during update")
                require(second["slice_us"] == 1000 and third["slice_us"] == 2000 and third["generation"] == 2, "online updates did not apply")
                checks.append({"name": "online_update_without_restart", "passed": True, "states": [second, third]})
                base = [str(backend.binary), "--set-pin", str(backend.pin_dir / "control"), "--expected-id", str(backend.map_id)]
                stale = subprocess.run(base + ["--expected-generation", "0", "--slice-us", "1000"], capture_output=True, text=True)
                require(stale.returncode == 3 and backend.snapshot()["generation"] == 2, "stale generation accepted")
                checks.append({"name": "stale_generation_rejected", "passed": True, "returncode": stale.returncode, "stderr": stale.stderr.strip()})
                bad = subprocess.run(base + ["--expected-generation", "2", "--slice-us", "99"], capture_output=True, text=True)
                require(bad.returncode == 2 and backend.snapshot()["slice_us"] == 2000, "invalid slice accepted")
                checks.append({"name": "invalid_slice_rejected", "passed": True, "returncode": bad.returncode})
                bad_id = subprocess.run([str(backend.binary), "--get-pin", str(backend.pin_dir / "control"),
                                         "--expected-id", str(backend.map_id + 1)], capture_output=True, text=True)
                require(bad_id.returncode != 0, "wrong map identity accepted")
                checks.append({"name": "wrong_map_identity_rejected", "passed": True, "returncode": bad_id.returncode})
                backend.heartbeat()
                checks.append({"name": "heartbeat_readback", "passed": True, "state": backend.snapshot()})
            finally:
                backend.stop()
            require(sched_state() == "disabled", "normal shutdown did not restore default")
            checks.append({"name": "normal_shutdown_default_restored", "passed": True})
            lease = NativeBackend(log, directory, lease_seconds=2)
            try:
                lease.start(1000)
                started = time.monotonic()
                while lease.process.poll() is None and time.monotonic() - started < 6:
                    time.sleep(0.1)
                require(lease.process.poll() == 1, "lost heartbeat did not stop backend")
                require(sched_state() == "disabled", "lease expiry did not restore default")
                checks.append({"name": "lost_heartbeat_default_restored", "passed": True,
                               "elapsed_seconds": time.monotonic() - started, "returncode": lease.process.returncode})
            finally:
                lease.stop()
        evidence.update(status="passed", build=backend.manifest, final_sched_ext=sched_state())
    except BaseException as exc:
        evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)}, final_sched_ext=sched_state())
        raise
    finally:
        log.close()
        (directory / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (ROOT / "state/native-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": evidence["status"], "checks": checks, "directory": str(directory)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
