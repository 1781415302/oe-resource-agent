"""Explainable workload router with confirmation and minimum residence."""
from dataclasses import dataclass


@dataclass
class PolicyConfig:
    latency_slice_us: int = 1000
    batch_slice_us: int = 20000
    balanced_slice_us: int = 4000
    confirmations: int = 3
    residence_seconds: float = 10
    batch_active_cpu_percent: float = 10
    latency_switches_per_sec: float = 20
    latency_wait_fraction: float = 0.05


class Router:
    def __init__(self, config=None):
        self.config = config or PolicyConfig()
        self.pending = None
        self.pending_count = 0
        self.last_change = None

    def propose(self, observation):
        cfg = self.config
        tasks = [p for p in observation["tasks"] if p["cpu_percent"] is not None]
        latency = [p for p in tasks if "latency" in p["roles"]]
        batch = [p for p in tasks if "batch" in p["roles"] and p["cpu_percent"] >= cfg.batch_active_cpu_percent]
        reasons = []
        if not tasks:
            return {"mode": "unknown", "policy_id": None, "slice_us": None, "reasons": ["No complete workload window"], "quality": "insufficient"}
        interactive = [p for p in latency if (p["voluntary_per_sec"] or 0) >= cfg.latency_switches_per_sec
                       or (p["run_wait_fraction"] or 0) >= cfg.latency_wait_fraction]
        if interactive and batch:
            mode, policy_id, value = "mixed_latency", "scx.slice", cfg.latency_slice_us
            reasons = ["Observed declared latency workload with sleep/wakeup activity or scheduler wait", "Observed active CPU batch workload"]
        elif latency and not batch:
            mode, policy_id, value = "service_only", "kernel.default", None
            reasons = ["Latency workload observed; active batch competitor absent", "Initial quick found a service-only throughput cost for fixed 1ms SCX"]
        elif batch and not latency:
            mode, policy_id, value = "batch_only", "kernel.default", None
            reasons = ["Only active CPU batch workloads observed", "No validated batch-only SCX benefit yet"]
        else:
            mode, policy_id, value = "balanced", "kernel.default", None
            reasons = ["Observed workloads do not support an unambiguous specialized mode"]
        return {"mode": mode, "policy_id": policy_id, "slice_us": value, "reasons": reasons, "quality": "observed",
                "latency_tasks": len(latency), "batch_tasks": len(batch), "interactive_tasks": len(interactive)}

    def decide(self, observation, current_slice_us, current_policy_id=None):
        proposal = self.propose(observation)
        desired = (proposal["policy_id"], proposal["slice_us"])
        current = (current_policy_id or ("scx.slice" if current_slice_us is not None else "kernel.default"), current_slice_us)
        now = observation["ended_monotonic"]
        if proposal["policy_id"] is None:
            self.pending, self.pending_count = None, 0
            return {**proposal, "action": "hold", "reason": "insufficient_observation"}
        if desired == current:
            self.pending, self.pending_count = None, 0
            return {**proposal, "action": "hold", "reason": "already_applied"}
        if self.pending == desired:
            self.pending_count += 1
        else:
            self.pending, self.pending_count = desired, 1
        if self.pending_count < self.config.confirmations:
            return {**proposal, "action": "hold", "reason": "confirming", "confirmation_count": self.pending_count}
        if self.last_change is not None and now - self.last_change < self.config.residence_seconds:
            return {**proposal, "action": "hold", "reason": "minimum_residence"}
        return {**proposal, "action": "apply", "reason": "confirmed_workload_change", "confirmation_count": self.pending_count}

    def applied(self, monotonic):
        self.last_change = monotonic
        self.pending, self.pending_count = None, 0
