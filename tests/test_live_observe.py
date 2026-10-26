"""Procfs feature conversion must not use load scenario metadata."""
import unittest
from unittest.mock import patch
from oeagent.live_observe import LiveSampler


def process(pid, ticks):
    return {"pid": pid, "start_ticks": pid + 1000, "allowed_cpus": [0], "cpu_ticks": ticks,
            "switches": {"voluntary": ticks, "involuntary": ticks * 2}}


class LiveObserverChecks(unittest.TestCase):
    def test_actual_cpu_pressure_and_proc_deltas_are_observed(self):
        sampler = LiveSampler()
        snapshots = {10: process(10, 100), 11: process(11, 100), 12: process(12, 100)}
        metrics = {"nginx_pid": 10, "nginx_wait_pct": 75, "nginx_avg_wait_us": 1000,
                   "http_p95_ms": 2, "probe_samples": 5, "probe_errors": 0, "sysbench_eps": 1000}
        cpus = [{"cpus": {"cpu0": {"total": 100, "busy": 20}}},
                {"cpus": {"cpu0": {"total": 200, "busy": 110}}}]
        with patch("oeagent.live_observe.process_snapshot", side_effect=lambda pid: snapshots.get(pid)), \
             patch("oeagent.live_observe.system_snapshot", side_effect=cpus), \
             patch("oeagent.live_observe.time.monotonic", side_effect=[1, 2]), \
             patch.object(sampler, "_runnable", return_value=4):
            first = sampler.sample(metrics, {"nginx": 10, "wrk2": 11, "sysbench": 12}, "default", 0, None)
            snapshots[10] = process(10, 102)
            snapshots[12] = process(12, 108)
            second = sampler.sample(metrics, {"nginx": 10, "wrk2": 11, "sysbench": 12}, "default", 0, None)
        self.assertIsNone(first.cpu_busy_pct)
        self.assertEqual(second.cpu_busy_pct, 90)
        self.assertEqual(second.wait_pct, 75)
        self.assertEqual(second.background_runnable, 4)
        self.assertEqual(first.identity, second.identity)

    def test_probe_from_another_pid_cannot_drive_decisions(self):
        sampler = LiveSampler()
        with patch("oeagent.live_observe.process_snapshot", return_value=process(10, 100)), \
             patch("oeagent.live_observe.system_snapshot", return_value={"cpus": {}}):
            value = sampler.sample({"nginx_pid": 99, "http_p95_ms": 100, "nginx_wait_pct": 90},
                                   {"nginx": 10}, "default", 0, None)
        self.assertFalse(value.valid)
        self.assertIsNone(value.probe_p95_ms)
        self.assertIsNone(value.wait_pct)


if __name__ == "__main__":
    unittest.main()
