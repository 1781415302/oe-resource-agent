"""Predeclared host-only gates; never retry because a scheduler performed badly."""
import datetime
import math


def utc_seconds(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def host_gate(points, start, end, limit, maximum_gap=8, minimum_points=2, *, time_key="utc"):
    if time_key not in ("utc", "host_monotonic_seconds"):
        raise ValueError("unsupported time_key")
    valid, issues = [], []
    for row in points:
        try:
            if time_key == "utc":
                stamp = utc_seconds(row["utc"])
            else:
                stamp = row["host_monotonic_seconds"]
                if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                    continue
                stamp = float(stamp)
        except (ValueError, TypeError, KeyError):
            continue
        if not start <= stamp <= end:
            continue
        cpu = row.get("host_cpu_percent")
        if (row.get("error") or not isinstance(cpu, (int, float)) or isinstance(cpu, bool)
                or not math.isfinite(cpu) or not 0 <= cpu <= 100):
            issues.append("host_sample_error")
        else:
            valid.append((stamp, cpu))
    valid.sort()
    if len(valid) < minimum_points:
        issues.append("host_samples_insufficient")
    gaps = [valid[i + 1][0] - valid[i][0] for i in range(len(valid) - 1)]
    if valid:
        gaps += [valid[0][0] - start, end - valid[-1][0]]
        if max(gaps) > maximum_gap:
            issues.append("host_sampling_gap")
        if max(cpu for _, cpu in valid) > limit:
            issues.append("host_cpu_over_limit")
    return {"passed": not issues, "issues": sorted(set(issues)), "sample_count": len(valid),
            "maximum_host_cpu_percent": max((cpu for _, cpu in valid), default=None),
            "limit_percent": limit, "maximum_sample_gap_seconds": max(gaps, default=None),
            "scope": "sampled Windows total CPU only; does not prove absence of all interference"}
