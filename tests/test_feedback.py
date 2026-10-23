"""Synthetic deterministic inputs validate rejection and control semantics, not speed."""
import copy
import unittest
from collections import deque

from oeagent.feedback import FeedbackConfig, SloGuard, validate_window
from oeagent.protocol import ToolError
from oeagent.runtime import AgentRuntime


def window(start=10, end=20, revision=0, identity="w1", p99=4, requests=10000):
    return {"schema_version": 1, "window_id": identity, "profile_id": "demo", "boot_id": "boot",
            "policy_revision": revision, "workload_key": "same-rate-same-page", "source": "wrk2.hdr.aligned",
            "source_sha256": "a" * 64, "start_monotonic": start, "end_monotonic": end,
            "frontend_p99_ms": p99, "completed_requests": requests, "request_errors": 0,
            "offered_rps": 1000, "background_eps": 100, "background_covered_seconds": end - start}


def metrics(value):
    return {"frontend_p99_ms": value["frontend_p99_ms"], "frontend_rps": value["completed_requests"] / (value["end_monotonic"] - value["start_monotonic"]),
            "completion_fraction": value["completed_requests"] / (value["end_monotonic"] - value["start_monotonic"]) / value["offered_rps"],
            "background_eps": value["background_eps"]}


class WindowChecks(unittest.TestCase):
    def test_rps_is_derived(self):
        self.assertEqual(validate_window(window(), "demo", "boot", 0, 0, 21, FeedbackConfig())["frontend_rps"], 1000)

    def test_revision_and_boot_are_checked(self):
        for change, code in (({"policy_revision": 1}, "STALE_METRIC_REVISION"), ({"boot_id": "another"}, "METRIC_CONTEXT_MISMATCH")):
            with self.assertRaises(ToolError) as error:
                validate_window({**window(), **change}, "demo", "boot", 0, 0, 21, FeedbackConfig())
            self.assertEqual(error.exception.code, code)

    def test_cross_policy_window_rejected(self):
        with self.assertRaises(ToolError) as error:
            validate_window(window(), "demo", "boot", 0, 15, 21, FeedbackConfig())
        self.assertEqual(error.exception.code, "CROSS_POLICY_WINDOW")

    def test_stale_future_and_short_windows_rejected(self):
        for sample, now in ((window(), 40), (window(end=30), 21), (window(end=11), 21)):
            with self.assertRaises(ToolError):
                validate_window(sample, "demo", "boot", 0, 0, now, FeedbackConfig())

    def test_missing_or_nonfinite_is_rejected(self):
        for change in ({"frontend_p99_ms": float("nan")}, {"completed_requests": True}, {"source_sha256": "g" * 64}):
            with self.assertRaises(ToolError):
                validate_window({**window(), **change}, "demo", "boot", 0, 0, 21, FeedbackConfig())

    def test_background_coverage_rejected(self):
        with self.assertRaises(ToolError):
            validate_window({**window(), "background_covered_seconds": 3}, "demo", "boot", 0, 0, 21, FeedbackConfig())


class GuardChecks(unittest.TestCase):
    def probe(self):
        guard = SloGuard()
        sample = window()
        decision = guard.submit(sample, metrics(sample), "identity", "kernel.default", 21)
        self.assertEqual(decision["policy_id"], "scx.slice")
        guard.committed(decision, 21)
        return guard

    def test_baseline_meeting_goal_keeps_default(self):
        guard = SloGuard()
        sample = window(p99=2)
        self.assertEqual(guard.submit(sample, metrics(sample), "identity", "kernel.default", 21)["reason"], "baseline_meets_business_goals")

    def test_zero_baseline_metric_abstains(self):
        guard = SloGuard()
        sample = {**window(), "background_eps": 0}
        result = guard.submit(sample, metrics(sample), "identity", "kernel.default", 21)
        self.assertEqual(result["reason"], "baseline_metric_inconclusive")
        self.assertIsNone(guard.baseline)

    def test_inconclusive_internal_baseline_rolls_back(self):
        guard = self.probe()
        guard.baseline["metrics"]["background_eps"] = 0
        sample = window(22, 32, 1, "w2", 2)
        self.assertEqual(guard.submit(sample, metrics(sample), "identity", "scx.slice", 33)["reason"], "business_comparison_inconclusive")

    def test_good_candidate_retained(self):
        guard = self.probe()
        sample = window(22, 32, 1, "w2", 2)
        result = guard.submit(sample, metrics(sample), "identity", "scx.slice", 33)
        self.assertEqual(result["reason"], "business_guard_accepted_candidate")
        self.assertEqual(guard.stage, "accepted")

    def test_background_cost_causes_rollback(self):
        guard = self.probe()
        sample = {**window(22, 32, 1, "w2", 2), "background_eps": 50}
        result = guard.submit(sample, metrics(sample), "identity", "scx.slice", 33)
        self.assertEqual(result["policy_id"], "kernel.default")
        self.assertFalse(result["evaluation"]["checks"]["background_floor"])

    def test_mitigation_keeps_goal_unmet_visible(self):
        guard = self.probe()
        guard.config.require_slo = False
        sample = window(22, 32, 1, "w2", 3.5)
        result = guard.submit(sample, metrics(sample), "identity", "scx.slice", 33)
        self.assertEqual(result["reason"], "latency_improved_slo_still_unmet")
        self.assertFalse(result["evaluation"]["slo_met"])
        self.assertTrue(result["evaluation"]["acceptable"])

    def test_errors_or_missing_rate_fail_guard(self):
        for change in ({"request_errors": 1}, {"completed_requests": 8000}):
            guard = self.probe()
            sample = {**window(22, 32, 1, "w2", 2), **change}
            self.assertEqual(guard.submit(sample, metrics(sample), "identity", "scx.slice", 33)["policy_id"], "kernel.default")

    def test_new_identity_or_rate_cannot_use_old_baseline(self):
        for identity, change in (("changed", {}), ("identity", {"offered_rps": 2000})):
            guard = self.probe()
            sample = {**window(22, 32, 1, "w2", 2), **change}
            self.assertEqual(guard.submit(sample, metrics(sample), identity, "scx.slice", 33)["reason"], "workload_or_rate_changed")

    def test_duplicate_and_overlap_rejected(self):
        for sample in (window(), window(19, 29, 1, "new", 2)):
            guard = self.probe()
            with self.assertRaises(ToolError):
                guard.submit(sample, metrics(sample), "identity", "scx.slice", 33)

    def test_missing_feedback_expires(self):
        guard = self.probe()
        self.assertEqual(guard.tick(112, "scx.slice")["reason"], "business_feedback_timeout")

    def test_rollback_blocks_immediate_same_workload_probe(self):
        guard = self.probe()
        guard.committed(guard.rollback("failed"), 30)
        sample = window(31, 41, 2, "w3")
        self.assertEqual(guard.submit(sample, metrics(sample), "identity", "kernel.default", 42)["reason"], "probe_cooldown")

    def test_multi_window_baseline_confirmation_and_median(self):
        guard = SloGuard(FeedbackConfig(baseline_confirmations=2))
        w1 = window(10, 15, 0, "w1", p99=6.0, requests=5000)
        first = guard.submit(w1, metrics(w1), "identity", "kernel.default", 16)
        self.assertEqual(first["action"], "hold")
        self.assertEqual(first["reason"], "confirming_baseline_stability")
        self.assertEqual(guard.stage, "confirming_baseline")
        w2 = window(15, 20, 0, "w2", p99=8.0, requests=5000)
        second = guard.submit(w2, metrics(w2), "identity", "kernel.default", 21)
        self.assertEqual(second["action"], "apply")
        self.assertEqual(second["reason"], "baseline_business_goal_violated")
        self.assertEqual(guard.baseline["metrics"]["frontend_p99_ms"], 7.0)
        self.assertEqual(guard.baseline["window_ids"], ["w1", "w2"])

    def test_backlog_catchup_spike_is_not_used_as_baseline(self):
        guard = SloGuard()
        catchup = window(10, 20, 0, "w-backlog", p99=12.0, requests=11500)
        decision = guard.submit(catchup, metrics(catchup), "identity", "kernel.default", 21)
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "baseline_backlog_draining")
        self.assertIsNone(guard.baseline)

    def test_continuous_window_source_accepted(self):
        sample = {**window(), "source": "wrk2.hdr.window"}
        derived = validate_window(sample, "demo", "boot", 0, 0, 21, FeedbackConfig())
        self.assertEqual(derived["frontend_rps"], 1000)


class IdentityChecks(unittest.TestCase):
    def runtime(self):
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.profile = {"sample_seconds": 1}
        tasks = [{"pid": i, "identity": {"pid": i, "start_ticks": 10}, "roles": [role],
                  "allowed_cpus": [0], "threads": 1, "cgroups": "root", "cpu_percent": 40}
                 for i, role in ((1, "latency"), (2, "batch"))]
        runtime.history = deque({"ended_monotonic": t, "window_seconds": 1,
                                 "tasks": copy.deepcopy(tasks), "quality": {"missing": []}} for t in (10, 11, 12))
        return runtime

    def test_stable_identity_covered(self):
        self.assertEqual(len(self.runtime()._business_identity(9.5, 11.5)), 64)

    def test_window_not_yet_observed_is_rejected(self):
        with self.assertRaises(ToolError):
            self.runtime()._business_identity(9.5, 12.5)

    def test_identity_or_thread_count_change_rejected(self):
        runtime = self.runtime()
        runtime.history[-1]["tasks"][0]["threads"] = 2
        with self.assertRaises(ToolError):
            runtime._business_identity(9.5, 11.5)


if __name__ == "__main__":
    unittest.main()
