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
                         "security.lsm.describe", "security.lsm.audit"):
            self.assertIn(expected, names)
        self.assertTrue(registry.call("network.ebpf.describe", {})["ok"])
        self.assertTrue(registry.call("security.lsm.describe", {})["ok"])
        self.assertFalse(registry.call("network.ebpf.attach", {"target_port": 70000})["ok"])
        self.assertFalse(registry.call("resource.cgroup.attach", {"group": "bad/name", "pid": 1})["ok"])


if __name__ == "__main__":
    unittest.main()
