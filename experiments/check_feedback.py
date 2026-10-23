"""Real metrics force probe/rejection and a separate lost-business-feedback case."""
import copy
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent), "/root/euler-bench"]
from oeagent.runtime import ResourceLock, load_profile
from oeagent.backend import sched_state
from run_fixed_rate import episode
from run_dynamic import write


def main():
    directory = ROOT / "results" / f"feedback-validation-{time.time_ns()}"
    directory.mkdir()
    profile = json.loads((Path(__file__).with_name("fixed-rate-profile.json")).read_text())
    profile.update(window_seconds=20, windows=2, port=18085)
    agent = load_profile(ROOT / "profiles/nginx-slo.json")
    agent["feedback"]["max_p99_ms"] = .001
    agent["feedback"]["require_slo"] = True
    agent["feedback"]["max_age_seconds"] = 2
    wrk = json.loads((ROOT / "state/wrk2-build.json").read_text())
    write(directory / "fault-test-profile.json", {"workload": profile, "agent": agent,
        "scope": "deliberately unattainable 1us goal verifies rejection, not a production performance claim"})
    result = {"schema_version": 1, "status": "running", "directory": str(directory), "checks": []}
    os.sched_setaffinity(0, {2})
    try:
        with ResourceLock():
            rejected = episode(directory / "reject-goal", "feedback", 1, profile, agent, wrk)
            if not rejected["valid"] or rejected["windows"][0]["feedback_response"]["decision"]["policy_id"] != "scx.slice":
                raise RuntimeError(f"Real baseline probe failed: {rejected.get('error')}")
            decision = rejected["windows"][1]["feedback_response"]["decision"]
            if decision["reason"] != "business_guard_rejected_candidate" or decision["evaluation"]["checks"]["p99_target"]:
                raise RuntimeError("Unmet measured business target did not reject candidate")
            if rejected["windows"][1]["policy_after"]["policy_id"] != "kernel.default":
                raise RuntimeError("Rejected probe did not roll back")
            result["checks"].append({"name": "real_business_baseline_probe_and_reject", "passed": True})
            timeout_profile, timeout_agent = copy.deepcopy(profile), copy.deepcopy(agent)
            timeout_profile.update(windows=1, verify_feedback_timeout=True)
            timeout_agent["policy"]["confirmations"] = 100
            timeout_agent["feedback"]["feedback_timeout_seconds"] = 20
            write(directory / "timeout-test-profile.json", {"workload": timeout_profile, "agent": timeout_agent})
            expired = episode(directory / "lost-business-feedback", "feedback", 1, timeout_profile, timeout_agent, wrk)
            if not expired["valid"] or not expired.get("feedback_timeout_verified", {}).get("passed"):
                raise RuntimeError(f"Lost metrics check failed: {expired.get('error')}")
            result["checks"].append({"name": "lost_business_metrics_restore_default", "passed": True,
                "evidence": expired["feedback_timeout_verified"]})
        result.update(status="passed", final_sched_ext=sched_state())
    except BaseException as exc:
        result.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        write(directory / "validation.json", result)
        write(ROOT / "state/feedback-validation.json", result)
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
