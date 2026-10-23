"""Verify real owned cgroup configuration, process migration, and CPU share differentiation."""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oeagent.observe import process_snapshot
from oeagent.plugins import CgroupPlugin
from oeagent.runtime import ResourceLock
from oeagent.tools import create_registry


def _start_cpu_worker():
    code = "import os, time; os.sched_setaffinity(0, {0});\nwhile True: pass"
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.1)
    snap = process_snapshot(proc.pid)
    if snap is None:
        proc.terminate()
        raise RuntimeError("Failed to snapshot worker process")
    return proc, snap


def main():
    plugin = CgroupPlugin()
    description = plugin.describe()
    base = "validation-" + uuid.uuid4().hex[:8]
    g_low, g_high = base + "-low", base + "-high"
    responses = []
    evidence = {"schema_version": 1, "status": "running", "plugin": description, "responses": responses,
                "scope": "owned v1 group configuration, verified PID migration, and 1:4 CPU share contention check"}
    try:
        if not description["available"] or description["cgroup_version"] != 1:
            evidence.update(status="unavailable")
        else:
            registry = create_registry()
            p_low, p_high = plugin.group_path(g_low), plugin.group_path(g_high)
            workers = []
            with ResourceLock():
                try:
                    created_low = registry.call("resource.cgroup.create", {"group": g_low})
                    created_high = registry.call("resource.cgroup.create", {"group": g_high})
                    responses.extend([created_low, created_high])
                    if not created_low["ok"] or not created_high["ok"]:
                        raise RuntimeError("Group creation failed")
                    before = created_low["data"]["value"]
                    changed = registry.call("resource.cgroup.apply", {"group": g_high, "expected_value": before, "value": 4096})
                    responses.append(changed)
                    if not changed["ok"] or changed["data"]["after"]["value"] != 4096:
                        raise RuntimeError("Group value readback failed")
                    stale = registry.call("resource.cgroup.apply", {"group": g_high, "expected_value": before, "value": 3072})
                    responses.append(stale)
                    if before != 4096 and (stale["ok"] or stale["error"]["code"] != "STALE_RESOURCE_VALUE"):
                        raise RuntimeError("Stale resource precondition accepted")

                    w_low, s_low = _start_cpu_worker()
                    workers.append(w_low)
                    w_high, s_high = _start_cpu_worker()
                    workers.append(w_high)

                    bad_attach = registry.call("resource.cgroup.attach", {
                        "group": g_low, "pid": w_low.pid, "expected_start_ticks": s_low["start_ticks"] + 1})
                    responses.append(bad_attach)
                    if bad_attach["ok"] or bad_attach["error"]["code"] != "STALE_PROCESS_IDENTITY":
                        raise RuntimeError("Mismatched start_ticks accepted")

                    att_low = registry.call("resource.cgroup.attach", {
                        "group": g_low, "pid": w_low.pid, "expected_start_ticks": s_low["start_ticks"]})
                    att_high = registry.call("resource.cgroup.attach", {
                        "group": g_high, "pid": w_high.pid, "expected_start_ticks": s_high["start_ticks"]})
                    responses.extend([att_low, att_high])
                    if not att_low["ok"] or not att_high["ok"]:
                        raise RuntimeError("Process attach failed")

                    t0_low = process_snapshot(w_low.pid)["cpu_ticks"]
                    t0_high = process_snapshot(w_high.pid)["cpu_ticks"]
                    time.sleep(2.5)
                    t1_low = process_snapshot(w_low.pid)["cpu_ticks"]
                    t1_high = process_snapshot(w_high.pid)["cpu_ticks"]
                    d_low = max(1, t1_low - t0_low)
                    d_high = max(1, t1_high - t0_high)
                    ratio = d_high / d_low
                    evidence["contention_experiment"] = {
                        "low_shares": before, "high_shares": 4096,
                        "low_cpu_ticks": d_low, "high_cpu_ticks": d_high,
                        "measured_high_to_low_ratio": round(ratio, 3),
                        "expected_theoretical_ratio": round(4096 / before, 3)
                    }
                    if ratio < 2.0:
                        raise RuntimeError(f"Expected high-share group to receive >2x CPU ticks, got ratio={ratio:.2f}")

                    det_low = registry.call("resource.cgroup.detach", {"group": g_low, "pid": w_low.pid})
                    det_high = registry.call("resource.cgroup.detach", {"group": g_high, "pid": w_high.pid})
                    responses.extend([det_low, det_high])
                    if not det_low["ok"] or not det_high["ok"]:
                        raise RuntimeError("Process detach failed")

                    restored = registry.call("resource.cgroup.apply", {"group": g_high, "expected_value": 4096, "value": before})
                    responses.append(restored)
                    if not restored["ok"]:
                        raise RuntimeError("Group configuration rollback failed")
                    evidence.update(status="passed")
                finally:
                    for proc in workers:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    for path in (p_low, p_high):
                        if path.is_dir():
                            if (path / "cgroup.procs").read_text().strip():
                                raise RuntimeError(f"Validation group {path} contains unexpected tasks")
                            path.rmdir()
                    evidence["empty_groups_removed"] = True
    except BaseException as exc:
        evidence.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        (ROOT / "state/cgroup-plugin-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
