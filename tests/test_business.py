"""Parser invariants use synthetic logs; live correctness is checked separately."""
import json
import unittest
from unittest.mock import patch

from oeagent.business import align_background_intervals, parse_wrk2


class BusinessParserChecks(unittest.TestCase):
    def parse(self, change=None):
        errors = dict.fromkeys(("connect", "read", "write", "status", "timeout"), 0)
        whole = {"errors": errors, "p50_us": 1000, "p95_us": 2000, "p99_us": 3000}
        aligned = {"start_ns": 1000000000, "end_ns": 11000000000, "requests": 10000,
                   "bytes": 40960000, "latency_samples": 10000, "rate": 1000, "errors": errors,
                   "terminal_exclusions": 0, "connections": 32}
        aligned.update(change or {})
        text = "Thread calibration: sample\nEULER_BENCH_JSON " + json.dumps(whole) + "\nOE_WRK2_ALIGNED " + json.dumps(aligned) + "\n"
        with patch("oeagent.business.Path") as mocked:
            mocked.return_value.read_text.return_value = text
            mocked.return_value.read_bytes.return_value = text.encode()
            return parse_wrk2("synthetic-client.log")

    def test_aligned_rps_and_hdr(self):
        result = self.parse()
        self.assertEqual(result["rps"], 1000)
        self.assertEqual(result["p99_ms"], 3)
        self.assertEqual(result["start_monotonic"], 1)

    def test_histogram_count_must_match_interval_requests(self):
        with self.assertRaises(ValueError):
            self.parse({"latency_samples": 9999})

    def test_only_counted_terminal_exclusions_explain_difference(self):
        result = self.parse({"latency_samples": 9999, "terminal_exclusions": 1})
        self.assertEqual(result["requests"], 10000)
        self.assertEqual(result["terminal_latency_exclusions"], 1)

    def test_invalid_timing_or_counter_rejected(self):
        for change in ({"end_ns": 0}, {"requests": True}, {"rate": 0}):
            with self.assertRaises(ValueError):
                self.parse(change)

    def test_background_boundary_straddle_alignment(self):
        points = [{"elapsed_seconds": float(i), "events_per_sec": 3200.0, "received_monotonic": 100.4 + i}
                  for i in range(1, 8)]
        aligned = align_background_intervals(points, 100.5, 105.35)
        self.assertEqual(aligned["whole_covered_seconds"], 3.0)
        self.assertAlmostEqual(aligned["covered_seconds"], 4.85, places=2)
        self.assertAlmostEqual(aligned["events_per_sec"], 3200.0, places=2)


if __name__ == "__main__":
    unittest.main()
