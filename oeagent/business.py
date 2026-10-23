"""Parse the one-thread wrk2 calibration/end export and continuous stream windows."""
import hashlib
import json
import math
from pathlib import Path


def parse_wrk2(path):
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    def marker(prefix):
        selected = [line for line in lines if line.startswith(prefix + " ")]
        if len(selected) != 1:
            raise ValueError(f"Expected one {prefix} record")
        return json.loads(selected[0].split(" ", 1)[1])
    whole = marker("EULER_BENCH_JSON")
    aligned = marker("OE_WRK2_ALIGNED")
    for key in ("start_ns", "end_ns", "requests", "bytes", "latency_samples", "rate", "terminal_exclusions", "connections"):
        if not isinstance(aligned[key], int) or isinstance(aligned[key], bool) or aligned[key] < 0:
            raise ValueError("Invalid aligned integer counter")
    duration = (aligned["end_ns"] - aligned["start_ns"]) / 1e9
    if (duration <= 0 or aligned["requests"] <= 0 or aligned["rate"] <= 0 or aligned["connections"] <= 0
            or aligned["latency_samples"] + aligned["terminal_exclusions"] != aligned["requests"]
            or aligned["terminal_exclusions"] > aligned["connections"]):
        raise ValueError("Aligned duration or HDR/request counts disagree")
    if not any("Thread calibration:" in line for line in lines):
        raise ValueError("No calibration record")
    for data in (whole, aligned):
        if any(not isinstance(data["errors"][key], int) or isinstance(data["errors"][key], bool) or data["errors"][key] < 0
               for key in ("connect", "read", "write", "status", "timeout")):
            raise ValueError("Invalid client error counter")
    for key in ("p50_us", "p95_us", "p99_us"):
        if not isinstance(whole[key], (int, float)) or not math.isfinite(whole[key]) or whole[key] < 0:
            raise ValueError("Invalid HDR latency percentile")
    return {"source": "wrk2.hdr.aligned", "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "start_monotonic": aligned["start_ns"] / 1e9, "end_monotonic": aligned["end_ns"] / 1e9,
            "duration_seconds": duration, "requests": aligned["requests"], "latency_samples": aligned["latency_samples"],
            "terminal_latency_exclusions": aligned["terminal_exclusions"],
            "rps": aligned["requests"] / duration, "offered_rps": aligned["rate"],
            "completion_fraction": aligned["requests"] / duration / aligned["rate"],
            "p50_ms": whole["p50_us"] / 1000, "p95_ms": whole["p50_us"] / 1000 if "p95_us" not in whole else whole["p95_us"] / 1000,
            "p99_ms": whole["p99_us"] / 1000,
            "errors": aligned["errors"], "whole_run": whole,
            "latency_scope": "HDR and counts after successful calibration until event-loop end; one client thread"}


def stream_window_to_frontend(window, offered_rps, raw_bytes):
    from .measurement import summarize_windows
    stats = summarize_windows([window], offered_rps)
    if stats["p99_ms"] is None:
        raise ValueError("Empty HDR histogram in stream window")
    return {"source": "wrk2.hdr.window", "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "start_monotonic": window["start_ns"] / 1e9, "end_monotonic": window["end_ns"] / 1e9,
            "duration_seconds": stats["duration_seconds"], "requests": stats["requests"],
            "terminal_latency_exclusions": window["terminal_exclusions"],
            "rps": stats["rps"], "offered_rps": offered_rps,
            "completion_fraction": stats["completion_fraction"],
            "p50_ms": stats["p50_ms"], "p95_ms": stats["p95_ms"], "p99_ms": stats["p99_ms"],
            "errors": window["errors"]}


def align_background_intervals(points, start, end):
    """Align 1s sysbench intervals to a [start, end] monotonic window with boundary clipping."""
    pts = list(points)
    duration = end - start
    if not pts or duration <= 0:
        return {"events_per_sec": None, "covered_seconds": 0.0, "whole_covered_seconds": 0.0,
                "measurement_seconds": max(0.0, duration), "reason": "no_background_points"}
    origin = min(p["received_monotonic"] - p["elapsed_seconds"] for p in pts)
    whole_weighted, whole_covered = 0.0, 0.0
    clip_weighted, clip_covered = 0.0, 0.0
    previous_elapsed = 0.0
    for point in pts:
        lo, hi = origin + previous_elapsed, origin + point["elapsed_seconds"]
        previous_elapsed = point["elapsed_seconds"]
        eps = point["events_per_sec"]
        if hi <= lo or not math.isfinite(eps):
            continue
        if start <= lo and hi <= end:
            whole_weighted += eps * (hi - lo)
            whole_covered += hi - lo
        ov_lo = max(start, lo)
        ov_hi = min(end, hi)
        if ov_hi > ov_lo:
            clip_weighted += eps * (ov_hi - ov_lo)
            clip_covered += ov_hi - ov_lo
    delay = max(p["received_monotonic"] - origin - p["elapsed_seconds"] for p in pts)
    good = clip_covered >= duration * 0.8 and delay < 2.0
    return {
        "events_per_sec": clip_weighted / clip_covered if clip_covered > 0 and good else None,
        "covered_seconds": min(duration, clip_covered),
        "whole_covered_seconds": whole_covered,
        "whole_events_per_sec": whole_weighted / whole_covered if whole_covered > 0 else None,
        "measurement_seconds": duration,
        "estimated_origin_monotonic": origin,
        "max_report_delivery_delay_seconds": delay,
        "alignment": "boundary-clipped overlap on background-intervals.jsonl",
        "reason": None if good else "insufficient_overlap_or_excessive_delivery_delay"
    }


def business_window(frontend, background, policy, workload_key, window_id):
    return {"schema_version": 1, "window_id": window_id, "profile_id": policy["profile_id"], "boot_id": policy["boot_id"],
            "policy_revision": policy["revision"], "workload_key": workload_key,
            "source": frontend["source"], "source_sha256": frontend["source_sha256"],
            "start_monotonic": frontend["start_monotonic"], "end_monotonic": frontend["end_monotonic"],
            "frontend_p99_ms": frontend["p99_ms"], "completed_requests": frontend["requests"],
            "request_errors": sum(frontend["errors"].values()), "offered_rps": frontend["offered_rps"],
            "background_eps": background["events_per_sec"], "background_covered_seconds": background["covered_seconds"]}
