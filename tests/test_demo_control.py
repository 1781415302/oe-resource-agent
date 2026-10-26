"""Dashboard regression tests for kernel control and measured-search lifecycle."""
import threading
import unittest
from unittest.mock import Mock

from experiments import web_dashboard as dashboard
from oeagent.demo_tuning import SCENARIOS, StrategySearch


class DemoControlChecks(unittest.TestCase):
    def sandbox(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.lock = threading.RLock()
        sandbox.backend = Mock(process=None)
        sandbox.backend.snapshot.return_value = {"policy_id": "kernel.default", "sched_ext": "disabled"}
        sandbox.tuning_running = False
        sandbox.auto_agent = False
        sandbox.tuning = None
        sandbox.suite = {"running": False, "results": []}
        sandbox.cgroup_state = {"running": False}
        sandbox._log = Mock()
        return sandbox

    def test_busy_comparison_refuses_load_change(self):
        sandbox = self.sandbox()
        sandbox.tuning_running = True
        result = sandbox.handle_control({"action": "toggle_sysbench", "enable": False})
        self.assertFalse(result["ok"])
        sandbox.backend.stop.assert_not_called()

    def test_invalid_scenario_does_not_cancel_running_search(self):
        sandbox = self.sandbox()
        sandbox.tuning_running = True
        result = sandbox.handle_control({"action": "set_scenario", "scenario": "not-a-scene"})
        self.assertFalse(result["ok"])
        self.assertTrue(sandbox.tuning_running)

    def test_manual_hot_update_cancels_search_and_uses_requested_candidate(self):
        sandbox = self.sandbox()
        sandbox._cancel_search = Mock()
        sandbox._apply_strategy = Mock(return_value={"map_id": 5, "generation": 2})
        result = sandbox.handle_control({"action": "set_policy", "policy": "scx", "slice_us": 500, "wakeup_preempt": True})
        self.assertTrue(result["ok"])
        sandbox._cancel_search.assert_called_once()
        sandbox._apply_strategy.assert_called_once_with("scx-500us")

    def test_unsupported_parameters_do_not_mutate_kernel(self):
        sandbox = self.sandbox()
        sandbox._apply_strategy = Mock()
        result = sandbox.handle_control({"action": "set_policy", "policy": "scx", "slice_us": 5})
        self.assertFalse(result["ok"])
        sandbox._apply_strategy.assert_not_called()

    def test_network_recovers_after_high_traffic_at_cooldown_zero(self):
        sandbox = self.sandbox()
        sandbox.auto_agent = True
        sandbox.ebpf_port = 18095
        sandbox.ebpf_state = {"attached": True, "drop_target": True}
        sandbox._prev_ebpf_target_pkts = 0
        sandbox._ebpf_auto_cooldown = 1
        sandbox.net_plugin = Mock()
        sandbox.net_plugin.status.side_effect = [{"active": True, "stats": {"target_packets": 50}},
                                                 {"active": True, "stats": {"target_packets": 50}}]
        sandbox._step_network()
        self.assertTrue(sandbox.ebpf_state["drop_target"])
        self.assertEqual(sandbox._ebpf_auto_cooldown, 0)
        sandbox._step_network()
        self.assertFalse(sandbox.ebpf_state["drop_target"])
        sandbox.net_plugin.attach.assert_called_once_with({"interface": "lo", "target_port": 18095, "drop_target": False})

    def test_invalid_payload_and_concurrent_cgroup_control_are_explicit(self):
        sandbox = self.sandbox()
        self.assertFalse(sandbox.handle_control([])["ok"])
        sandbox.cgroup_state["running"] = True
        self.assertFalse(sandbox.handle_control({"action": "start_tuning"})["ok"])

    def test_changed_process_identity_aborts_attribution(self):
        sandbox = self.sandbox()
        sandbox.tuning_running = True
        sandbox.tuning = StrategySearch(SCENARIOS["latency"])
        sandbox.tuning.begin(0, {"pid": 1})
        sandbox._measurement_context = Mock(return_value={"pid": 2})
        with self.assertRaisesRegex(RuntimeError, "身份"):
            sandbox._step_tuning()

    def monitor(self):
        import time
        sandbox = self.sandbox()
        sandbox.auto_agent = True
        sandbox.next_search_after = None
        sandbox.monitor_since = 0
        sandbox.last_decision = {"reference": {"eps": 100}}
        sandbox.monitor_context = {"revision": 1}
        sandbox._measurement_context = Mock(return_value=sandbox.monitor_context)
        sandbox._effective_scenario = Mock(return_value={"traffic": False, "batch": True})
        sandbox.last_monitor_feedback = time.monotonic()
        sandbox.tuning_seen = set()
        sandbox.monitor_bad_streak = 0
        sandbox._apply_strategy = Mock()
        return sandbox

    def test_three_regressions_restore_default_and_schedule_cooldown(self):
        sandbox = self.monitor()
        sandbox._samples_after = Mock(return_value=[{"window_id": str(i), "eps": 50,
            "background_coverage": 1, "errors": 0} for i in range(3)])
        sandbox._step_tuning()
        sandbox._apply_strategy.assert_called_once_with("default")
        self.assertIsNone(sandbox.monitor_since)
        self.assertIsNotNone(sandbox.next_search_after)

    def test_missing_monitor_feedback_restores_default(self):
        sandbox = self.monitor()
        sandbox.last_monitor_feedback -= 31
        sandbox._samples_after = Mock(return_value=[])
        sandbox._step_tuning()
        sandbox._apply_strategy.assert_called_once_with("default")

    def test_partial_suite_does_not_overwrite_last_completed_report(self):
        sandbox = self.sandbox()
        sandbox.saved_suite = {"status": "complete", "marker": "previous"}
        sandbox.suite = {"status": "cancelled", "results": []}
        sandbox._save_json = Mock()
        sandbox._save_suite()
        self.assertEqual(sandbox.saved_suite["marker"], "previous")
        sandbox._save_json.assert_called_once_with("demo-suite-progress.json", sandbox.suite)

    def test_invalid_goal_is_rejected_before_current_test_is_cancelled(self):
        sandbox = self.sandbox()
        sandbox.tuning_running = True
        sandbox._cancel_search = Mock()
        for value in (float("nan"), float("inf"), -1, True):
            result = sandbox.handle_control({"action": "set_scenario", "scenario": "latency", "p99_limit_ms": value})
            self.assertFalse(result["ok"])
        sandbox._cancel_search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
