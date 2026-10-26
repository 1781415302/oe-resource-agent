"""Contracts for independently checkable demo data and bounded readbacks."""
import hashlib
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from oeagent import demo_evidence as evidence
from experiments import web_dashboard as dashboard


def window():
    return {"schema_version": 1, "kind": "window", "seq": 3,
            "start_ns": 1000000000, "end_ns": 6000000000, "final": False,
            "requests": 10000, "bytes": 40960000,
            "errors": dict.fromkeys(("connect", "read", "write", "status", "timeout"), 0),
            "terminal_exclusions": 0, "histogram_overflows": 0, "bins_us": [[1000, 10000]]}


class EvidenceChecks(unittest.TestCase):
    def test_completed_rps_is_derived_from_raw_window(self):
        record = window()
        raw = json.dumps(record).encode()
        result = evidence.wrk_window(record, raw)
        self.assertEqual(result["rps"], 2000)
        self.assertEqual(result["offered_rps"], 25600)
        self.assertEqual(result["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(json.loads(result["raw_record"]), record)
        self.assertEqual(result["requests"], 10000)

    def test_inconsistent_hdr_is_not_displayed_as_valid_measurement(self):
        record = window()
        record["bins_us"] = [[1000, 9999]]
        with self.assertRaises(ValueError):
            evidence.wrk_window(record, json.dumps(record).encode())

    def test_partial_and_calibration_records_are_not_full_windows(self):
        self.assertIsNone(evidence.wrk_window({"kind": "ready"}, b"ready"))
        record = {**window(), "final": True}
        self.assertIsNone(evidence.wrk_window(record, b"partial"))

    def test_arbitrary_command_request_cannot_reach_runner(self):
        with patch.object(evidence, "run_readonly") as runner:
            with self.assertRaises(ValueError):
                evidence.readbacks("system; id", {"agent": 123})
        runner.assert_not_called()

    def test_inactive_scheduler_has_no_fabricated_map(self):
        with patch.object(evidence, "run_readonly", return_value={"exit_code": 0, "stdout": "disabled"}) as runner:
            result = evidence.readbacks("scheduler", {"agent": 123})
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(result["notes"])
        self.assertEqual(runner.call_args.args[0], ["cat", "/sys/kernel/sched_ext/state"])

    def test_read_failure_is_explicit_and_bounded(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["cat"], 2)) as runner:
            result = evidence.run_readonly(["cat", "/proc/uptime"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdout"], "")
        self.assertTrue(result["stderr"])
        self.assertEqual(runner.call_args.kwargs["timeout"], 2)
        self.assertNotIn("shell", runner.call_args.kwargs)

    def test_absent_workload_has_missing_latency_and_wait(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.nginx_proc = None
        sandbox.sysbench_proc = None
        sandbox.sysbench_enabled = False
        sandbox.sysbench_latest_eps = 0
        with patch.object(dashboard, "demo_evidence", evidence, create=True), \
                patch.object(dashboard.time, "sleep"):
            metrics = sandbox._probe_http_and_sched()
        self.assertIsNone(metrics["http_p99_ms"])
        self.assertIsNone(metrics["nginx_wait_pct"])
        self.assertEqual(metrics["probe_samples"], 0)
        self.assertFalse(metrics["schedstat_valid"])

    def test_pending_sysbench_report_is_not_zero_throughput(self):
        sandbox = dashboard.LiveSandbox.__new__(dashboard.LiveSandbox)
        sandbox.nginx_proc = None
        sandbox.sysbench_proc = None
        sandbox.sysbench_enabled = True
        sandbox.sysbench_latest_eps = None
        sandbox.sysbench_last_report = None
        with patch.object(dashboard, "demo_evidence", evidence, create=True), \
                patch.object(dashboard.time, "sleep"):
            metrics = sandbox._probe_http_and_sched()
        self.assertIsNone(metrics["sysbench_eps"])


if __name__ == "__main__":
    unittest.main()
