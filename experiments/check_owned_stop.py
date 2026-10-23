"""Interrupt only this experiment instance, verify owned cleanup and default restore."""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oeagent.backend import sched_state


def main():
    key = "validation-cancel-" + str(time.time_ns())
    directory = ROOT / "results" / ("controlled-" + key)
    checker = ROOT / "experiments/run_controlled_cycle.py"
    evidence = {"schema_version": 1, "status": "running", "run_key": key, "directory": str(directory), "checks": []}
    if sched_state() != "disabled":
        raise RuntimeError("Foreign scheduler active")
    with (ROOT / "state/owned-stop-driver.log").open("w") as log:
        process = subprocess.Popen([sys.executable, str(checker), "--run-key", key, "--mode", "smoke",
            "--conditions", "fixed-1000,preempt-1000,default"], stdout=log, stderr=subprocess.STDOUT)
        try:
            until = time.monotonic() + 15
            while sched_state() != "enabled" and process.poll() is None and time.monotonic() < until:
                time.sleep(.05)
            if sched_state() != "enabled":
                raise RuntimeError("Owned experiment never loaded SCX")
            time.sleep(2)
            result = subprocess.run([sys.executable, str(checker), "--run-key", key, "--stop"],
                capture_output=True, text=True, timeout=50, check=True)
            response = json.loads(result.stdout)
            process.wait(timeout=10)
            summary = json.loads((directory / "summary.json").read_text())
            if (not response["stopped"] or response["reason"] != "owned_SIGTERM" or process.returncode != 1
                    or summary["error"]["type"] != "KeyboardInterrupt" or summary["final_sched_ext"] != "disabled"
                    or sched_state() != "disabled"):
                raise RuntimeError("Owned interrupt did not preserve failure evidence and restore default")
            evidence["checks"].append({"name": "pidfd_owned_interrupt_default_restored", "passed": True,
                "response": response, "experiment_returncode": process.returncode, "summary": summary})
            again = subprocess.run([sys.executable, str(checker), "--run-key", key, "--stop"],
                capture_output=True, text=True, timeout=10, check=True)
            if not json.loads(again.stdout)["stopped"]:
                raise RuntimeError("Repeated stop did not recognize exited instance")
            evidence["checks"].append({"name": "repeated_stop_exited_instance", "passed": True})
            unrelated = subprocess.run([sys.executable, str(checker), "--run-key", key + "-unknown", "--stop"],
                capture_output=True, text=True, timeout=10, check=True)
            if json.loads(unrelated.stdout) != {"stopped": False, "reason": "owner_not_published"}:
                raise RuntimeError("Unknown run did not abstain")
            evidence["checks"].append({"name": "unknown_run_abstains", "passed": True})
            evidence.update(status="passed", final_sched_ext=sched_state())
        except BaseException as exc:
            evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=40)
            (ROOT / "state/owned-stop-validation.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps({key: value for key, value in evidence.items() if key not in ("checks",)}, indent=2))


if __name__ == "__main__":
    main()
