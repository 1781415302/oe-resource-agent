"""Evaluate supplied business metrics without substituting CPU usage for SLOs."""


def compare(before, after, goals):
    fields = ("frontend_p99_ms", "frontend_rps", "background_eps")
    missing = [key for key in fields if before.get(key) is None or after.get(key) is None]
    if missing:
        return {"status": "inconclusive", "missing_metrics": missing, "acceptable": None}
    if any(before[key] <= 0 or after[key] < 0 for key in fields):
        return {"status": "inconclusive", "reason": "invalid baseline or candidate metric", "acceptable": None}
    changes = {key: (after[key] / before[key] - 1) * 100 for key in fields}
    checks = {
        "p99_target": after["frontend_p99_ms"] <= goals["max_p99_ms"],
        "frontend_floor": after["frontend_rps"] / before["frontend_rps"] >= goals["min_frontend_fraction"],
        "background_floor": after["background_eps"] / before["background_eps"] >= goals["min_background_fraction"],
    }
    return {"status": "evaluated", "acceptable": all(checks.values()), "checks": checks,
            "percent_changes": changes, "scope": "provided metric windows; caller must establish comparable workload"}
