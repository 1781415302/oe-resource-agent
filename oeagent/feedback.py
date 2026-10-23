"""Business windows and a bounded baseline/probe/retain-or-rollback controller."""
from dataclasses import dataclass
from collections import deque
import statistics

from .evaluate import compare
from .protocol import ToolError, object_schema, validate


WINDOW_SCHEMA = object_schema({
    "schema_version": {"type": "integer", "enum": [1]},
    "window_id": {"type": "string", "minLength": 1, "maxLength": 128},
    "profile_id": {"type": "string", "minLength": 1, "maxLength": 128},
    "boot_id": {"type": "string", "minLength": 1, "maxLength": 128},
    "policy_revision": {"type": "integer", "minimum": 0},
    "workload_key": {"type": "string", "minLength": 1, "maxLength": 128},
    "source": {"type": "string", "enum": ["wrk.reported", "wrk2.hdr.aligned", "wrk2.hdr.window"]},
    "source_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
    "start_monotonic": {"type": "number", "minimum": 0},
    "end_monotonic": {"type": "number", "minimum": 0},
    "frontend_p99_ms": {"type": "number", "minimum": 0},
    "completed_requests": {"type": "integer", "minimum": 1},
    "request_errors": {"type": "integer", "minimum": 0},
    "offered_rps": {"type": "number", "minimum": 1},
    "background_eps": {"type": "number", "minimum": 0},
    "background_covered_seconds": {"type": "number", "minimum": 0},
}, ["schema_version", "window_id", "profile_id", "boot_id", "policy_revision", "workload_key", "source",
    "source_sha256", "start_monotonic", "end_monotonic", "frontend_p99_ms", "completed_requests", "request_errors",
    "offered_rps", "background_eps", "background_covered_seconds"])


@dataclass
class FeedbackConfig:
    require_slo: bool = True
    candidate_wakeup_preempt: bool = False
    max_p99_ms: float = 3.0
    min_frontend_fraction: float = 0.9
    min_background_fraction: float = 0.9
    min_completion_fraction: float = 0.97
    max_completion_fraction: float = 1.05
    min_improvement_fraction: float = 0.05
    min_window_seconds: float = 5.0
    max_age_seconds: float = 10.0
    max_baseline_age_seconds: float = 120.0
    feedback_timeout_seconds: float = 90.0
    cooldown_seconds: float = 60.0
    min_background_coverage: float = 0.8
    candidate_slice_us: int = 1000
    baseline_confirmations: int = 1

    def __post_init__(self):
        properties = {name: {"type": "number", "minimum": 0.001, "maximum": 3600}
                      for name in self.__dataclass_fields__}
        for name in ("min_frontend_fraction", "min_background_fraction", "min_completion_fraction",
                     "min_improvement_fraction", "min_background_coverage"):
            properties[name] = {"type": "number", "minimum": 0, "maximum": 1}
        properties["max_completion_fraction"] = {"type": "number", "minimum": 1.0, "maximum": 2.0}
        properties["candidate_slice_us"] = {"type": "integer", "minimum": 100, "maximum": 20000}
        properties["baseline_confirmations"] = {"type": "integer", "minimum": 1, "maximum": 10}
        properties["require_slo"] = {"type": "boolean"}
        properties["candidate_wakeup_preempt"] = {"type": "boolean"}
        validate(self.__dict__, object_schema(properties, list(properties)))
        if self.feedback_timeout_seconds <= self.min_window_seconds + self.max_age_seconds:
            raise ToolError("INVALID_FEEDBACK_CONFIG", "反馈超时必须覆盖完整测量窗口和提交延迟。")


def validate_window(window, profile_id, boot_id, revision, policy_since, now, config):
    validate(window, WINDOW_SCHEMA)
    if window["profile_id"] != profile_id or window["boot_id"] != boot_id:
        raise ToolError("METRIC_CONTEXT_MISMATCH", "业务配置或启动标识不同。")
    if window["policy_revision"] != revision:
        raise ToolError("STALE_METRIC_REVISION", "指标不是当前策略版本。")
    start, end = window["start_monotonic"], window["end_monotonic"]
    if start < policy_since:
        raise ToolError("CROSS_POLICY_WINDOW", "指标区间跨越策略动作。")
    if not config.min_window_seconds <= end - start <= 3600 or end > now:
        raise ToolError("INVALID_METRIC_WINDOW", "测量时长无效或时间来自未来。")
    if now - end > config.max_age_seconds:
        raise ToolError("STALE_METRICS", "业务指标已过期。")
    if any(c not in "0123456789abcdef" for c in window["source_sha256"]):
        raise ToolError("INVALID_METRIC_SOURCE", "来源摘要必须是小写SHA256。")
    duration = end - start
    coverage = window["background_covered_seconds"] / duration
    if not config.min_background_coverage <= coverage <= 1.001:
        raise ToolError("METRIC_COVERAGE_INSUFFICIENT", "后台指标未覆盖足够的同一测量区间。")
    return {"frontend_p99_ms": window["frontend_p99_ms"], "frontend_rps": window["completed_requests"] / duration,
            "background_eps": window["background_eps"], "completion_fraction": window["completed_requests"] / duration / window["offered_rps"]}


class SloGuard:
    """Metrics are local collector claims; time, revision and observed identity are checked."""
    def __init__(self, config=None):
        self.config = config or FeedbackConfig()
        self.baseline = None
        self.baseline_candidates = []
        self.stage = "awaiting_baseline"
        self.deadline = None
        self.blocked_key = None
        self.retry_after = 0.0
        self.seen = deque(maxlen=256)
        self.last_end = None
        self.last_evaluation = None

    def status(self):
        return {"stage": self.stage, "deadline_monotonic": self.deadline, "retry_after_monotonic": self.retry_after,
                "baseline_window_id": self.baseline["window"]["window_id"] if self.baseline else None,
                "baseline_confirmations": len(self.baseline_candidates),
                "last_evaluation": self.last_evaluation}

    def reset(self):
        self.baseline, self.deadline, self.last_end = None, None, None
        self.baseline_candidates = []
        self.stage = "awaiting_baseline"

    def rollback(self, reason):
        return {"action": "apply", "policy_id": "kernel.default", "slice_us": None, "reason": reason}

    def committed(self, decision, now):
        if decision["policy_id"] == "scx.slice":
            self.stage, self.deadline = "probing", now + self.config.feedback_timeout_seconds
        else:
            self.blocked_key = self.baseline["key"] if self.baseline else None
            self.retry_after = now + self.config.cooldown_seconds
            self.reset()
            self.stage = "cooldown"

    def tick(self, now, current_policy_id):
        if current_policy_id == "scx.slice" and self.deadline is not None and now >= self.deadline:
            return self.rollback("business_feedback_timeout")
        return {"action": "hold", "reason": "awaiting_business_window"}

    def submit(self, window, metrics, observed_identity, current_policy_id, now):
        if window["window_id"] in self.seen:
            raise ToolError("DUPLICATE_METRIC_WINDOW", "业务窗口已处理。")
        if self.last_end is not None and window["start_monotonic"] < self.last_end:
            raise ToolError("OVERLAPPING_METRIC_WINDOW", "业务窗口重叠或顺序倒退。")
        self.seen.append(window["window_id"])
        self.last_end = window["end_monotonic"]
        key = (window["workload_key"], observed_identity, window["source"], window["offered_rps"])
        cfg = self.config
        if current_policy_id == "kernel.default":
            if window["request_errors"]:
                self.baseline_candidates = []
                return {"action": "hold", "reason": "baseline_request_errors"}
            if any(metrics[k] <= 0 for k in ("frontend_p99_ms", "frontend_rps", "background_eps")):
                self.baseline_candidates = []
                return {"action": "hold", "reason": "baseline_metric_inconclusive"}
            if key == self.blocked_key and now < self.retry_after:
                self.baseline_candidates = []
                return {"action": "hold", "reason": "probe_cooldown"}
            if metrics["completion_fraction"] > cfg.max_completion_fraction:
                self.baseline_candidates = []
                return {"action": "hold", "reason": "baseline_backlog_draining"}
            if metrics["frontend_p99_ms"] <= cfg.max_p99_ms and metrics["completion_fraction"] >= cfg.min_completion_fraction:
                self.baseline_candidates = []
                self.baseline = {"window": window, "metrics": metrics, "key": key}
                self.stage = "baseline_recorded"
                return {"action": "hold", "reason": "baseline_meets_business_goals"}
            if self.baseline_candidates and self.baseline_candidates[-1]["key"] != key:
                self.baseline_candidates = []
            self.baseline_candidates.append({"window": window, "metrics": metrics, "key": key})
            if len(self.baseline_candidates) < cfg.baseline_confirmations:
                self.stage = "confirming_baseline"
                return {"action": "hold", "reason": "confirming_baseline_stability",
                        "confirmation_count": len(self.baseline_candidates)}
            if len(self.baseline_candidates) == 1:
                chosen_metrics = metrics
            else:
                chosen_metrics = {
                    field: statistics.median(item["metrics"][field] for item in self.baseline_candidates)
                    for field in ("frontend_p99_ms", "frontend_rps", "background_eps", "completion_fraction")
                }
            self.baseline = {"window": window, "metrics": chosen_metrics, "key": key,
                             "window_ids": [item["window"]["window_id"] for item in self.baseline_candidates]}
            self.stage = "baseline_recorded"
            return {"action": "apply", "policy_id": "scx.slice", "slice_us": cfg.candidate_slice_us,
                    "wakeup_preempt": cfg.candidate_wakeup_preempt,
                    "reason": "baseline_business_goal_violated"}
        if self.baseline is None:
            return self.rollback("no_comparable_baseline")
        if key != self.baseline["key"]:
            return self.rollback("workload_or_rate_changed")
        if now - self.baseline["window"]["end_monotonic"] > cfg.max_baseline_age_seconds:
            return self.rollback("baseline_expired")
        evaluation = compare(self.baseline["metrics"], metrics, {
            "max_p99_ms": cfg.max_p99_ms, "min_frontend_fraction": cfg.min_frontend_fraction,
            "min_background_fraction": cfg.min_background_fraction})
        if evaluation["status"] != "evaluated":
            return {**self.rollback("business_comparison_inconclusive"), "evaluation": evaluation}
        checks = evaluation.setdefault("checks", {})
        checks["completion_floor"] = cfg.min_completion_fraction <= metrics["completion_fraction"] <= cfg.max_completion_fraction
        checks["no_request_errors"] = window["request_errors"] == 0
        checks["latency_improvement"] = metrics["frontend_p99_ms"] <= self.baseline["metrics"]["frontend_p99_ms"] * (1 - cfg.min_improvement_fraction)
        evaluation["slo_met"] = checks["p99_target"]
        evaluation["require_slo"] = cfg.require_slo
        evaluation["hard_constraints"] = {k: v for k, v in checks.items() if cfg.require_slo or k != "p99_target"}
        evaluation["acceptable"] = evaluation.get("status") == "evaluated" and all(evaluation["hard_constraints"].values())
        evaluation.update(baseline_window_id=self.baseline["window"]["window_id"], candidate_window_id=window["window_id"])
        self.last_evaluation = evaluation
        if not evaluation["acceptable"]:
            return {**self.rollback("business_guard_rejected_candidate"), "evaluation": evaluation}
        self.stage = "accepted" if evaluation["slo_met"] else "mitigating_slo_unmet"
        self.deadline = now + cfg.feedback_timeout_seconds
        return {"action": "hold", "reason": "business_guard_accepted_candidate" if evaluation["slo_met"] else "latency_improved_slo_still_unmet", "evaluation": evaluation}
