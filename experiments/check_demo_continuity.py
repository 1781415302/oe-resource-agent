#!/usr/bin/env python3
"""Short live API check: scene transitions retain history and isolate cards."""
import json
from pathlib import Path
import time
import urllib.request


def request(body=None):
    path = "/api/live-state" if body is None else "/api/control"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:8899" + path, data=data,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as response:
        value = json.load(response)
    if value.get("ok") is False:
        raise RuntimeError(value["error"])
    return value


def fresh():
    until = time.monotonic() + 8
    while time.monotonic() < until:
        state = request()
        if not state["sample_pending"] and state["latest"].get("workload_revision") == state["workload_revision"]:
            return state
        time.sleep(.15)
    raise TimeoutError("Current scene sample did not arrive")


if __name__ == "__main__":
    before = fresh()
    original = before["scenario_id"]
    alternate = "latency" if original != "latency" else "service"
    started = time.monotonic()
    try:
        request({"action": "set_scenario", "scenario": alternate})
        pending = request()
        if pending["sample_pending"]:
            assert pending["latest"]["http_p95_ms"] is None
            assert pending["latest"]["sysbench_eps"] is None
        after = fresh()
        assert not after["tuning"]["running"]
        assert any(p["seq"] == before["latest"]["seq"] for p in after["history"])
        assert any(p["workload_revision"] == after["workload_revision"] for p in after["history"])
        assert after["scene_events"][-1]["scenario_id"] == alternate
        assert after["latest"]["nginx_pid"] == after["process_ids"]["nginx"]
        result = {"status": "passed", "elapsed_seconds": round(time.monotonic()-started, 3),
                  "previous_scene": original, "next_scene": alternate,
                  "old_revision": before["workload_revision"], "new_revision": after["workload_revision"],
                  "retained_points": len(after["history"]), "scene_events": after["scene_events"],
                  "current_cards_isolated": True, "transport": "real HTTP API; no browser or full benchmark"}
        Path("state/demo-continuity-validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        request({"action": "set_scenario", "scenario": original})
