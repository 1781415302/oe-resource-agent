"""Live Agent separation and continuous display regression checks."""
from collections import deque
from copy import deepcopy
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, MagicMock, patch

from experiments import web_dashboard as dashboard
from oeagent import demo_evidence
from oeagent.demo_tuning import SCENARIOS
from oeagent.live_agent import LiveAgent
from oeagent.weighted_agent import WeightedAgent


class QuickChecks(unittest.TestCase):
    def test_toggle_auto_enables_independent_agent_without_long_search(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.lock = threading.RLock()
        sandbox.auto_agent = False
        sandbox.tuning_running = False
        sandbox.cgroup_state = {"running": False}
        sandbox._enable_autonomous = Mock()
        sandbox._start_search = Mock()
        self.assertTrue(sandbox.handle_control({"action": "toggle_auto_agent", "enable": True})["ok"])
        sandbox._enable_autonomous.assert_called_once()
        sandbox._start_search.assert_not_called()

    def test_scene_change_only_changes_load_without_agent_or_policy_calls(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.lock = threading.RLock()
        sandbox.tuning_running = False
        sandbox.cgroup_state = {"running": False}
        sandbox._cancel_search = Mock()
        sandbox._apply_scenario = Mock()
        sandbox.scenario = deepcopy(SCENARIOS["latency"])
        sandbox._enable_autonomous = Mock()
        sandbox._start_search = Mock()
        self.assertTrue(sandbox.handle_control({"action": "set_scenario", "scenario": "latency"})["ok"])
        sandbox._enable_autonomous.assert_not_called()
        sandbox._start_search.assert_not_called()
        sandbox._cancel_search.assert_not_called()

    def test_scene_api_rejects_policy_and_goal_injection(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.lock = threading.RLock()
        sandbox.tuning_running = False
        sandbox.cgroup_state = {"running": False}
        sandbox._apply_scenario = Mock()
        for field in ("strategy_id", "p99_limit_ms", "background_floor"):
            result = sandbox.handle_control({"action": "set_scenario", "scenario": "latency", field: 1})
            self.assertFalse(result["ok"])
        sandbox._apply_scenario.assert_not_called()

    def test_global_weight_update_does_not_change_load_or_policy(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.lock = threading.RLock()
        sandbox.tuning_running = False
        sandbox.cgroup_state = {"running": False}
        sandbox.live_agent = WeightedAgent()
        sandbox._log = Mock()
        sandbox._apply_scenario = Mock()
        sandbox._apply_strategy = Mock()
        result = sandbox.handle_control({"action": "set_agent_weights", "latency": .8, "throughput": .2})
        self.assertTrue(result["ok"])
        self.assertEqual(sandbox.live_agent.weights.latency, .8)
        sandbox._apply_scenario.assert_not_called()
        sandbox._apply_strategy.assert_not_called()

    def test_uniform_experiment_sampling_does_not_change_agent_or_policy(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.lock = threading.RLock()
        sandbox.tuning_running = False
        sandbox.cgroup_state = {"running": False}
        sandbox.auto_agent = True
        sandbox.control_mode = "autonomous"
        sandbox._log = Mock()
        sandbox._apply_strategy = Mock()
        result = sandbox.handle_control({"action": "set_experiment_sampling", "enable": True})
        self.assertTrue(result["ok"])
        self.assertTrue(sandbox.experiment_sampling)
        self.assertTrue(sandbox.auto_agent)
        self.assertEqual(sandbox.control_mode, "autonomous")
        self.assertFalse(sandbox.handle_control({"action": "set_experiment_sampling", "enable": "true"})["ok"])
        sandbox._apply_strategy.assert_not_called()

    def test_old_scene_window_is_not_shown_as_current_telemetry(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        record = {"schema_version": 1, "kind": "window", "seq": 1, "start_ns": 1000000000,
                  "end_ns": 6000000000, "final": False, "requests": 10000,
                  "bytes": 40960000, "terminal_exclusions": 0, "histogram_overflows": 0,
                  "bins_us": [[1000, 10000]], "errors": dict.fromkeys(("connect", "read", "write", "status", "timeout"), 0)}
        proc = Mock(pid=123)
        proc.stdout = io.StringIO('OE_WRK2_STREAM '+json.dumps(record)+'\n')
        sandbox.wrk_proc = proc
        sandbox.lock = threading.RLock()
        sandbox.work_dir = Path("fixture-only")
        sandbox.scenario_since = 2
        sandbox.offered_rps = 25600
        sandbox.front_windows = deque()
        sandbox.wrk_window = None
        sandbox._log = Mock()
        file = MagicMock()
        with patch.object(Path, "open", return_value=file), patch.object(dashboard, "demo_evidence", demo_evidence, create=True):
            sandbox._read_wrk_stdout(proc)
        self.assertIsNone(sandbox.wrk_window)
        self.assertFalse(sandbox.front_windows)

    def test_scene_switch_retains_chart_history_but_discards_old_live_window(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        previous = {"workload_revision": 1, "http_p95_ms": 1.2}
        sandbox.history = deque([previous])
        sandbox.workload_revision = 1
        sandbox.scene_events = deque()
        sandbox.front_windows = deque([{"old": True}])
        sandbox.auxiliary_logs = {}
        sandbox.wrk_window = {"old": True}
        for method in ("_apply_strategy", "_stop_traffic_locked", "_ensure_sysbench", "_ensure_traffic", "_log"):
            setattr(sandbox, method, Mock())
        sandbox._apply_scenario("latency")
        sandbox._apply_strategy.assert_not_called()
        self.assertEqual(list(sandbox.history), [previous])
        self.assertIsNone(sandbox.wrk_window)
        self.assertFalse(sandbox.front_windows)
        self.assertEqual(sandbox.scene_events[-1]["scenario_id"], "latency")
        self.assertEqual(sandbox.scene_events[-1]["workload_revision"], 2)

    def test_previous_scene_history_is_not_used_for_current_cards(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.workload_revision = 2
        sandbox.history = deque([{"workload_revision": 1, "sysbench_eps": 5000}])
        self.assertIsNone(sandbox._current_scene_sample())
        self.assertEqual(len(sandbox.history), 1)
        current = {"workload_revision": 2, "sysbench_eps": 2000}
        sandbox.history.append(current)
        self.assertEqual(sandbox._current_scene_sample(), current)


if __name__ == "__main__":
    unittest.main()
