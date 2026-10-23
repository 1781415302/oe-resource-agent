"""Exercise the persistent public tool session against the actual SCX backend."""
import json
import os
import select
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    directory = ROOT / "results" / f"tool-session-validation-{time.time_ns()}"
    directory.mkdir(parents=True)
    records = []
    evidence = {"schema_version": 1, "status": "running", "responses": records, "directory": str(directory)}
    with (directory / "stderr.log").open("w", encoding="utf-8") as error_stream:
        process = subprocess.Popen([str(ROOT / "agent"), "serve", "--profile", "nginx-cpu"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_stream,
                                   text=True, bufsize=1)
        def call(tool, arguments, request_id):
            process.stdin.write(json.dumps({"id": request_id, "tool": tool, "arguments": arguments}) + "\n")
            process.stdin.flush()
            if not select.select([process.stdout], [], [], 10)[0]:
                raise RuntimeError("Tool response timed out")
            result = json.loads(process.stdout.readline())
            records.append(result)
            return result
        try:
            first = call("policy.status", {}, 0)
            if not first["ok"] or first["request_id"] != 0 or first["data"]["policy_id"] != "kernel.default":
                raise RuntimeError("Initial public tool state failed")
            loaded = call("policy.apply", {"policy_id": "scx.slice", "slice_us": 1000, "expected_revision": 0}, "load")
            updated = call("policy.apply", {"policy_id": "scx.slice", "slice_us": 2000,
                "wakeup_preempt": True, "expected_revision": 1}, "update")
            if not loaded["ok"] or not updated["ok"] or loaded["data"]["pid"] != updated["data"]["pid"]:
                raise RuntimeError("Public tool online apply failed")
            if loaded["data"]["wakeup_preempt"] or not updated["data"]["wakeup_preempt"] or loaded["data"]["map_id"] != updated["data"]["map_id"]:
                raise RuntimeError("Public online preemption flag/readback failed")
            stale = call("policy.apply", {"policy_id": "scx.slice", "slice_us": 1000, "expected_revision": 0}, "stale")
            if stale["ok"] or stale["error"]["code"] != "STALE_REVISION":
                raise RuntimeError("Public stale revision not rejected")
            restored = call("policy.rollback", {"expected_revision": 2}, "rollback")
            if not restored["ok"] or restored["data"]["sched_ext"] != "disabled":
                raise RuntimeError("Public rollback failed")
            process.stdin.close()
            process.wait(timeout=10)
            if process.returncode:
                raise RuntimeError("Control session failed after EOF")
            evidence.update(status="passed", final_sched_ext=Path("/sys/kernel/sched_ext/state").read_text().strip())
        except BaseException as exc:
            evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            (directory / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (ROOT / "state/tool-session-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": evidence["status"], "responses": records, "directory": str(directory)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
