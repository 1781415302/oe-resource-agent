"""Public SLO protocol: batched requests, rejected metrics, signal cleanup."""
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oeagent.backend import sched_state


def main():
    directory = ROOT / "results" / f"feedback-session-validation-{time.time_ns()}"
    directory.mkdir()
    records, checks = [], []
    evidence = {"schema_version": 1, "status": "running", "directory": str(directory), "checks": checks, "responses": records}
    with (directory / "stderr.log").open("wb") as errors:
        process = subprocess.Popen([str(ROOT / "agent"), "serve", "--profile", "nginx-slo"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        buffered = b""
        def receive():
            nonlocal buffered
            deadline = time.monotonic() + 10
            while b"\n" not in buffered:
                if time.monotonic() > deadline or not select.select([process.stdout], [], [], .2)[0]:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Protocol response timeout")
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Protocol closed before response")
                buffered += chunk
            raw, buffered = buffered.split(b"\n", 1)
            result = json.loads(raw)
            records.append(result)
            return result
        def send(tool, args, identifier):
            process.stdin.write((json.dumps({"tool": tool, "arguments": args, "id": identifier}) + "\n").encode())
            process.stdin.flush()
            return receive()
        try:
            process.stdin.write(b'{"id":0,"tool":"policy.status","arguments":{}}\n{"id":1,"tool":"metrics.status","arguments":{}}\n')
            process.stdin.flush()
            first, second = receive(), receive()
            if not first["ok"] or not second["ok"] or first["request_id"] != 0 or second["request_id"] != 1:
                raise RuntimeError("Coalesced requests lost or reordered")
            checks.append({"name": "coalesced_requests_and_ids", "passed": True})
            denied = send("policy.apply", {"policy_id": "scx.slice", "slice_us": 1000, "expected_revision": 0}, "manual")
            if denied["ok"] or denied["error"]["code"] != "AUTOMATIC_CONTROL_ACTIVE":
                raise RuntimeError("Manual tuning bypassed feedback controller")
            checks.append({"name": "feedback_control_ownership", "passed": True})
            now = time.monotonic()
            bad = {"schema_version": 1, "window_id": "invalid-revision", "profile_id": first["data"]["profile_id"],
                "boot_id": first["data"]["boot_id"], "policy_revision": 9, "workload_key": "test", "source": "wrk2.hdr.aligned",
                "source_sha256": "a" * 64, "start_monotonic": now - 10, "end_monotonic": now - 1,
                "frontend_p99_ms": 1, "completed_requests": 9000, "request_errors": 0, "offered_rps": 1000,
                "background_eps": 100, "background_covered_seconds": 9}
            response = send("metrics.submit", bad, "stale")
            if response["ok"] or response["error"]["code"] != "STALE_METRIC_REVISION":
                raise RuntimeError("Stale business window accepted")
            checks.append({"name": "public_metrics_stale_revision_rejected", "passed": True})
            nested = send("agent.run", {"profile": "nginx-cpu", "duration_seconds": 1}, "nested")
            if nested["ok"] or nested["error"]["code"] != "CONTROL_SESSION_OWNED":
                raise RuntimeError("Nested controller accepted")
            checks.append({"name": "nested_agent_rejected", "passed": True})
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
            if process.returncode != 0 or sched_state() != "disabled":
                raise RuntimeError("Idle-input SIGTERM did not close cleanly")
            checks.append({"name": "signal_closes_idle_protocol", "passed": True})
            evidence.update(status="passed", final_sched_ext=sched_state())
        except BaseException as exc:
            evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=20)
            process.stdin.close()
            process.stdout.close()
            (directory / "validation.json").write_text(json.dumps(evidence, indent=2) + "\n")
            (ROOT / "state/feedback-session-validation.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps({k: v for k, v in evidence.items() if k != "responses"}, indent=2))


if __name__ == "__main__":
    main()
