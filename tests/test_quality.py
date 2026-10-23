"""Host gate semantics and experimental preemption configuration checks."""
import datetime
import unittest

from oeagent.feedback import FeedbackConfig, SloGuard
from oeagent.protocol import ToolError
from oeagent.quality import host_gate
from oeagent.runtime import business_roles_present
from oeagent.tools import create_registry


def sample(stamp, cpu):
    return {"utc": datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat(), "host_cpu_percent": cpu}


class QualityChecks(unittest.TestCase):
    def test_complete_quiet_window(self):
        self.assertTrue(host_gate([sample(t, 10) for t in range(0, 31, 5)], 0, 30, 20)["passed"])

    def test_any_over_limit_sample_rejects(self):
        result = host_gate([sample(0, 10), sample(5, 80), sample(10, 10)], 0, 10, 30)
        self.assertIn("host_cpu_over_limit", result["issues"])

    def test_edge_and_internal_gaps_reject(self):
        for times in ((10, 15, 20), (0, 5, 20), (0, 5, 10)):
            self.assertIn("host_sampling_gap", host_gate([sample(t, 10) for t in times], 0, 20, 30)["issues"])

    def test_unavailable_is_not_zero(self):
        self.assertFalse(host_gate([sample(0, None), sample(5, float("nan"))], 0, 5, 30)["passed"])

    def test_outside_spike_does_not_affect_window(self):
        self.assertTrue(host_gate([sample(-5, 90), sample(0, 10), sample(5, 10)], 0, 5, 30)["passed"])


class WakeupConfigChecks(unittest.TestCase):
    def test_cpu_bound_service_keeps_business_role(self):
        self.assertTrue(business_roles_present({"tasks": [
            {"roles": ["latency"], "cpu_percent": 90, "voluntary_per_sec": 0},
            {"roles": ["batch"], "cpu_percent": 10, "voluntary_per_sec": 0}]}))

    def test_gone_service_or_batch_is_detected(self):
        for tasks in ([], [{"roles": ["latency"]}], [{"roles": ["batch"]}]):
            self.assertFalse(business_roles_present({"tasks": tasks}))

    def test_default_disabled(self):
        self.assertFalse(FeedbackConfig().candidate_wakeup_preempt)

    def test_non_boolean_rejected(self):
        with self.assertRaises(ToolError):
            FeedbackConfig(candidate_wakeup_preempt=1)

    def test_feedback_probe_preserves_experimental_flag(self):
        guard = SloGuard(FeedbackConfig(candidate_wakeup_preempt=True))
        value = {"window_id": "w1", "start_monotonic": 0, "end_monotonic": 10, "request_errors": 0,
            "workload_key": "mixed", "source": "wrk2.hdr.aligned", "offered_rps": 1000}
        result = guard.submit(value, {"frontend_p99_ms": 5, "frontend_rps": 1000,
            "background_eps": 100, "completion_fraction": 1}, "same", "kernel.default", 11)
        self.assertTrue(result["wakeup_preempt"])

    def test_public_tool_validates_preempt_flag(self):
        registry = create_registry()
        args = {"policy_id": "scx.slice", "slice_us": 1000, "wakeup_preempt": True}
        self.assertTrue(registry.call("policy.validate", args)["ok"])
        self.assertFalse(registry.call("policy.validate", {**args, "wakeup_preempt": 1})["ok"])
        self.assertFalse(registry.call("policy.validate", {"policy_id": "kernel.default", "wakeup_preempt": True})["ok"])


if __name__ == "__main__":
    unittest.main()
