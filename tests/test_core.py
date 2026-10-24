"""Deterministic protocol/policy checks; no scheduler or resource mutations."""
import math
import unittest

from oeagent.evaluate import compare
from oeagent.observe import cpu_list
from oeagent.policy import PolicyConfig, Router
from oeagent.protocol import Registry, ToolError, object_schema, validate
from oeagent.histogram import merge, percentile, summarize


def task(role, cpu, voluntary):
    return {"cpu_percent": cpu, "roles": [role], "voluntary_per_sec": voluntary,
            "run_wait_fraction": None}


def observation(stamp, mixed=True):
    tasks = [task("latency", 40, 300)]
    if mixed:
        tasks.append(task("batch", 50, 0))
    return {"tasks": tasks, "ended_monotonic": stamp}


class ProtocolChecks(unittest.TestCase):
    def test_unknown_field_rejected(self):
        with self.assertRaises(ToolError):
            validate({"unexpected": 1}, object_schema())

    def test_bool_is_not_integer(self):
        with self.assertRaises(ToolError):
            validate(True, {"type": "integer"})

    def test_nonfinite_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ToolError):
                validate(value, {"type": "number"})

    def test_tool_errors_are_structured(self):
        result = Registry().call("unregistered", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_TOOL")

    def test_handler_runs_after_validation(self):
        seen = []
        registry = Registry()
        registry.register("sample", "sample", object_schema(), lambda args: seen.append(args) or {"done": True})
        self.assertFalse(registry.call("sample", {"x": 1})["ok"])
        self.assertEqual(seen, [])
        self.assertTrue(registry.call("sample", {})["ok"])

    def test_zero_request_id_preserved(self):
        self.assertEqual(Registry().call("unregistered", {}, 0)["request_id"], 0)

    def test_invalid_request_id_prevents_execution(self):
        seen = []
        registry = Registry()
        registry.register("sample", "sample", object_schema(), lambda args: seen.append(args))
        result = registry.call("sample", {}, math.nan)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_REQUEST_ID")
        self.assertEqual(seen, [])


class PolicyChecks(unittest.TestCase):
    def test_confirmation_precedes_apply(self):
        router = Router()
        self.assertEqual(router.decide(observation(1), None)["action"], "hold")
        self.assertEqual(router.decide(observation(2), None)["action"], "hold")
        third = router.decide(observation(3), None)
        self.assertEqual(third["action"], "apply")
        self.assertEqual(third["policy_id"], "scx.slice")
        self.assertEqual(third["slice_us"], 1000)

    def test_service_only_chooses_default(self):
        self.assertEqual(Router().propose(observation(1, False))["policy_id"], "kernel.default")

    def test_missing_observation_abstains(self):
        result = Router().decide({"tasks": [], "ended_monotonic": 1}, None)
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["reason"], "insufficient_observation")

    def test_residence_delays_return(self):
        router = Router(PolicyConfig(confirmations=1, residence_seconds=10))
        router.applied(10)
        self.assertEqual(router.decide(observation(15, False), 1000)["reason"], "minimum_residence")
        self.assertEqual(router.decide(observation(21, False), 1000)["action"], "apply")

    def test_already_applied_resets_pending(self):
        router = Router()
        router.decide(observation(1), None)
        self.assertEqual(router.decide(observation(2), 1000)["reason"], "already_applied")
        self.assertEqual(router.pending_count, 0)


class MetricsChecks(unittest.TestCase):
    def test_histogram_merge_preserves_counts(self):
        self.assertEqual(merge([[[1, 2], [3, 4]], [[1, 5], [2, 1]]]), [[1, 7], [2, 1], [3, 4]])

    def test_percentiles_are_not_averaged(self):
        result = summarize([[[1, 1000]], [[100, 1]]])
        self.assertEqual(result["p99_ms"], 0.001)
        self.assertEqual(result["sample_count"], 1001)

    def test_histogram_rejects_duplicate_or_negative(self):
        for histogram in ([[1, 1], [1, 2]], [[-1, 4]], [[1, 0]]):
            with self.assertRaises(ValueError):
                percentile(histogram, 99)

    def test_missing_metrics_do_not_prove_benefit(self):
        self.assertIsNone(compare({}, {}, {})["acceptable"])

    def test_background_floor_prevents_false_win(self):
        result = compare({"frontend_p99_ms": 4, "frontend_rps": 100, "background_eps": 100},
                         {"frontend_p99_ms": 2, "frontend_rps": 99, "background_eps": 20},
                         {"max_p99_ms": 3, "min_frontend_fraction": 0.9, "min_background_fraction": 0.9})
        self.assertFalse(result["acceptable"])
        self.assertFalse(result["checks"]["background_floor"])

    def test_cpu_ranges(self):
        self.assertEqual(cpu_list("0-2,5,2"), [0, 1, 2, 5])
        with self.assertRaises(ValueError):
            cpu_list("3-1")

    def test_plugin_tools_registered_and_validated(self):
        from oeagent.tools import create_registry
        registry = create_registry()
        names = {item["name"] for item in registry.list()}
        for expected in ("resource.cgroup.attach", "resource.cgroup.detach",
                         "network.ebpf.describe", "network.ebpf.status", "network.ebpf.attach", "network.ebpf.detach",
                         "security.lsm.describe", "security.lsm.audit", "security.lsm.enforce"):
            self.assertIn(expected, names)
        self.assertTrue(registry.call("network.ebpf.describe", {})["ok"])
        self.assertTrue(registry.call("security.lsm.describe", {})["ok"])
        self.assertFalse(registry.call("network.ebpf.attach", {"target_port": 70000})["ok"])
        self.assertFalse(registry.call("resource.cgroup.attach", {"group": "bad/name", "pid": 1})["ok"])
        self.assertFalse(registry.call("security.lsm.enforce", {"pid": 1, "nice": 30})["ok"])

    def test_plugin_auto_config_validation(self):
        from oeagent.plugins import PluginAutoConfig
        cfg = PluginAutoConfig(latency_shares=4096, batch_shares=1024, monitor_port=18095, drop_threshold_pps=20.0)
        self.assertTrue(cfg.enabled)
        with self.assertRaises(ToolError):
            PluginAutoConfig(latency_group="same", batch_group="same")
        with self.assertRaises(ToolError):
            PluginAutoConfig(monitor_port=70000)
        with self.assertRaises(ToolError):
            PluginAutoConfig(monitor_port=80)
        with self.assertRaises(ToolError):
            PluginAutoConfig(drop_threshold_pps=0)

    def test_auto_plugin_coordinator_lifecycle(self):
        from oeagent.plugins import AutoPluginCoordinator, PluginAutoConfig
        events = []
        cfg = PluginAutoConfig(drop_threshold_pps=20.0, drop_hold_windows=2)
        coord = AutoPluginCoordinator(cfg, events.append, dry_run=True)

        # Window 1: Service only -> LSM audit on nginx + eBPF attach in observe mode; Cgroup remains inactive
        w1 = {"ended_monotonic": 10.0, "ebpf_stats": {"target_packets": 0, "dropped_packets": 0},
              "tasks": [{"pid": 101, "comm": "nginx", "roles": ["latency"], "identity": {"pid": 101, "start_ticks": 500}}]}
        coord.step(w1, "kernel.default")
        self.assertEqual(coord.status()["lsm_audited_count"], 1)
        self.assertFalse(coord.status()["cgroup"]["active"])
        self.assertTrue(coord.status()["ebpf"]["attached"])
        self.assertFalse(coord.status()["ebpf"]["drop_target"])

        # Window 2: Mixed contention arrives -> LSM audit on sysbench + Cgroup auto-isolates 4096:1024
        w2 = {"ended_monotonic": 11.0, "ebpf_stats": {"target_packets": 5, "dropped_packets": 0},
              "tasks": [{"pid": 101, "comm": "nginx", "roles": ["latency"], "identity": {"pid": 101, "start_ticks": 500}},
                        {"pid": 202, "comm": "sysbench", "roles": ["batch"], "identity": {"pid": 202, "start_ticks": 600}}]}
        coord.step(w2, "kernel.default")
        self.assertEqual(coord.status()["lsm_audited_count"], 2)
        self.assertEqual(coord.status()["lsm_enforced_count"], 1)
        self.assertTrue(coord.status()["cgroup"]["active"])
        self.assertEqual(coord.status()["cgroup"]["latency_pids"], [101])
        self.assertEqual(coord.status()["cgroup"]["batch_pids"], [202])

        # Window 3: High-rate burst on monitored port (50 pkt/s >= 20 pkt/s) -> eBPF auto-isolates (drop_target=True)
        w3 = {"ended_monotonic": 12.0, "ebpf_stats": {"target_packets": 55, "dropped_packets": 0},
              "tasks": w2["tasks"]}
        coord.step(w3, "scx.slice")
        self.assertTrue(coord.status()["ebpf"]["drop_target"])

        # Window 4 & 5: Burst subsides -> after hold_windows=2 cooldown, eBPF auto-recovers (drop_target=False)
        w4 = {"ended_monotonic": 13.0, "ebpf_stats": {"target_packets": 56, "dropped_packets": 10}, "tasks": w2["tasks"]}
        coord.step(w4, "scx.slice")
        self.assertTrue(coord.status()["ebpf"]["drop_target"])
        w5 = {"ended_monotonic": 14.0, "ebpf_stats": {"target_packets": 57, "dropped_packets": 10}, "tasks": w2["tasks"]}
        coord.step(w5, "scx.slice")
        self.assertFalse(coord.status()["ebpf"]["drop_target"])

        # Window 6: Batch exits and policy returns to kernel.default -> Cgroup auto-releases
        w6 = {"ended_monotonic": 15.0, "ebpf_stats": {"target_packets": 58, "dropped_packets": 10},
              "tasks": [w1["tasks"][0]]}
        coord.step(w6, "kernel.default")
        self.assertFalse(coord.status()["cgroup"]["active"])
        coord.close()
        event_names = [e["event"] for e in events]
        for expected_event in ("plugin_security_audited", "plugin_security_enforced", "plugin_ebpf_attached",
                               "plugin_cgroup_isolated", "plugin_ebpf_isolated", "plugin_ebpf_recovered",
                               "plugin_cgroup_released", "plugin_cleanup_completed"):
            self.assertIn(expected_event, event_names)



if __name__ == "__main__":
    unittest.main()
