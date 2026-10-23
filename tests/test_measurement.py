"""Offline contracts for baseline windows, host time, and rate selection."""
import datetime
import json
import unittest
from pathlib import Path

from oeagent.histogram import percentile
from oeagent.measurement import (
    audit_trial,
    audit_windows,
    load_profile,
    parse_client_stream,
    select_rates,
    summarize_windows,
    validate_request,
)
from oeagent.quality import host_gate

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "experiments" / "baseline-profile.json"
NS = 1_000_000_000


def errors(**overrides):
    counts = {"connect": 0, "read": 0, "write": 0, "status": 0, "timeout": 0}
    counts.update(overrides)
    return counts


def ready(start_ns, offered_rps):
    return {"schema_version": 1, "kind": "ready", "start_ns": start_ns, "window_ms": 5000,
            "offered_rps": offered_rps, "connections": 32, "client_threads": 1, "pipeline": 1,
            "histogram": "wrk2_expected_latency_hdr_3sig"}


def window(seq, start_ns, end_ns, requests, bins, *, final=False, terminal=0, overflows=0, error_counts=None):
    return {"schema_version": 1, "kind": "window", "seq": seq, "start_ns": start_ns, "end_ns": end_ns,
            "final": final, "requests": requests, "bytes": 0, "errors": errors(**(error_counts or {})),
            "terminal_exclusions": terminal, "histogram_overflows": overflows, "bins_us": bins}


def footer(windows, export_duration_ns=0):
    bins = {}
    for item in windows:
        for value, count in item["bins_us"]:
            bins[value] = bins.get(value, 0) + count
    summed = errors()
    for item in windows:
        for key in summed:
            summed[key] += item["errors"][key]
    return {"schema_version": 1, "kind": "footer", "start_ns": windows[0]["start_ns"],
            "end_ns": windows[-1]["end_ns"], "windows": len(windows),
            "requests": sum(item["requests"] for item in windows),
            "bytes": sum(item["bytes"] for item in windows), "errors": summed,
            "terminal_exclusions": sum(item["terminal_exclusions"] for item in windows),
            "histogram_overflows": sum(item["histogram_overflows"] for item in windows),
            "bins_us": [[value, bins[value]] for value in sorted(bins)],
            "export_duration_ns": export_duration_ns}


def stream_text(records):
    return "".join("OE_WRK2_STREAM " + json.dumps(record) + "\n" for record in records)


def write_stream(directory, records):
    path = directory / "client.log"
    path.write_text(stream_text(records), encoding="utf-8")
    return path


def record(trial_id, scenario, rate, *, condition="default", workload=None, background=False):
    return {"trial_id": trial_id, "condition": condition, "scenario": scenario, "rate": rate,
            "client_log": "client.log", "samples_log": "samples.jsonl",
            "background_log": "background.log" if background else None,
            "background_intervals_log": "background-intervals.jsonl" if background else None,
            "agent_events_log": "events.jsonl", "driver_events_log": "driver-events.jsonl",
            "workload_events": workload or [], "process_returncodes": {"client:10:100": 0, "server:11:200": -15},
            "cleanup_errors": [], "error": None}


def audit(trial_id, sustained, *, cpu=False, measurement=True, execution=True):
    return {"trial_id": trial_id, "measurement_passed": measurement, "execution_passed": execution,
            "issues": [], "stats": None, "sustained": sustained, "client_cpu_limited": cpu,
            "transition_coverage": "not_applicable", "performance_flags": [], "windows": []}


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def side_logs(directory, *, mode="default", start=100, end=105):
    write_jsonl(directory / "events.jsonl", [
        {"monotonic": start - 1, "event": "agent_started", "agent_version": "0.1.3", "mode": mode},
        {"monotonic": end + 1, "event": "agent_stopped", "sched_ext": "disabled"},
    ])
    write_jsonl(directory / "driver-events.jsonl", [
        {"monotonic": start - 2, "event": "process_started", "pid": 11, "start_ticks": 200,
         "boot_id": "boot", "role": "server", "command": ["nginx"]},
        {"monotonic": start - 1.5, "event": "process_started", "pid": 10, "start_ticks": 100,
         "boot_id": "boot", "role": "client", "command": ["wrk2"]},
        {"monotonic": end + 0.5, "event": "process_stopped", "pid": 10, "role": "client", "returncode": 0},
        {"monotonic": end + 1, "event": "process_stopped", "pid": 11, "role": "server", "returncode": -15},
    ])


def write_samples(path, start, end, cpu):
    rows = [{"monotonic": start - 1, "sched_ext": "disabled", "processes": {
        "client": {"pid": 10, "start_ticks": 100, "cpu_percent": None}}}]
    stamp = start
    while stamp < end:
        rows.append({"monotonic": stamp + 1, "sample_start_monotonic": stamp, "sample_interval_seconds": 1,
                     "sched_ext": "disabled", "processes": {
                         "client": {"pid": 10, "start_ticks": 100, "cpu_percent": cpu}}})
        stamp += 1
    write_jsonl(path, rows)


def service_records(start, rate, count, *, bins_for=None):
    records = [ready(start * NS, rate)]
    windows = []
    for index in range(count):
        begin = (start + index * 5) * NS
        requests = rate * 5
        bins = bins_for(index) if bins_for else [[1000, requests]]
        windows.append(window(index + 1, begin, begin + 5 * NS, requests, bins))
    records.extend(windows)
    records.append(footer(windows))
    return records


class HistogramContract(unittest.TestCase):
    def test_five_second_rank_is_4950_not_three_milliseconds(self):
        bins = [[1000, 4950], [3000, 50]]
        item = window(1, 0, 5 * NS, 5000, bins)
        stats = summarize_windows([item], 1000)
        self.assertEqual(stats["duration_seconds"], 5)
        self.assertEqual(stats["requests"], 5000)
        self.assertEqual(stats["latency_samples"], 5000)
        self.assertEqual(stats["rps"], 1000)
        self.assertEqual(stats["completion_fraction"], 1)
        self.assertEqual(stats["p99_ms"], 1)
        self.assertNotEqual(stats["p99_ms"], 3)
        self.assertEqual(percentile(bins, 99), 3000)

    def test_terminal_exclusion_requires_5001_requests(self):
        bins = [[1000, 4950], [3000, 50]]
        rejected = [ready(0, 1000), window(1, 0, 5 * NS, 5000, bins, terminal=1), footer([
            window(1, 0, 5 * NS, 5000, bins, terminal=1)])]
        with self.assertRaises(ValueError):
            parse_client_stream(self._write(rejected))
        accepted_window = window(1, 0, 5 * NS, 5001, bins, terminal=1)
        path = self._write([ready(0, 1000), accepted_window, footer([accepted_window])])
        parsed = parse_client_stream(path)
        self.assertTrue(parsed["complete"])
        stats = summarize_windows(parsed["windows"], 1000)
        self.assertEqual(stats["requests"], 5001)
        self.assertEqual(stats["latency_samples"], 5000)
        self.assertEqual(stats["p99_ms"], 1)

    def _write(self, records):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "client.log"
        path.write_text(stream_text(records), encoding="utf-8")
        return path


class StreamAudit(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.profile = load_profile(PROFILE_PATH)

    def test_main_histogram_mismatch_fails_measurement(self):
        item = window(1, 100 * NS, 105 * NS, 5000, [[1000, 4950], [3000, 50]])
        broken = footer([item])
        broken["bins_us"] = [[1000, 4950], [3000, 49]]
        write_stream(self.directory, [ready(100 * NS, 1000), item, broken])
        side_logs(self.directory)
        write_samples(self.directory / "samples.jsonl", 100, 105, 10)
        result = audit_trial(self.directory, record("run-a", "service", 1000), self.profile)
        self.assertFalse(result["measurement_passed"])
        self.assertIn("footer_mismatch", result["issues"])
        self.assertEqual([item["seq"] for item in result["windows"]], [1])

    def test_empty_tail_percentiles_are_null(self):
        body = window(1, 0, 5 * NS, 5000, [[1000, 4950], [3000, 50]])
        tail = window(2, 5 * NS, 6 * NS, 0, [], final=True)
        stats = summarize_windows([tail], 1000)
        self.assertIsNone(stats["p50_ms"])
        self.assertIsNone(stats["p95_ms"])
        self.assertIsNone(stats["p99_ms"])
        self.assertNotEqual(stats["p99_ms"], 0)
        write_stream(self.directory, [ready(0, 1000), body, tail, footer([body, tail])])
        side_logs(self.directory, start=0, end=6)
        audits = audit_windows(self.directory, record("run-tail", "service", 1000), self.profile)
        self.assertIsNone(audits[-1]["stats"]["p99_ms"])
        self.assertEqual(audits[-1]["stats"]["latency_samples"], 0)

    def test_missing_background_coverage_keeps_window(self):
        item = window(1, 100 * NS, 105 * NS, 10000, [[1000, 10000]])
        write_stream(self.directory, [ready(100 * NS, 2000), item, footer([item])])
        side_logs(self.directory)
        (self.directory / "background.log").write_text("sysbench\n", encoding="utf-8")
        write_jsonl(self.directory / "background-intervals.jsonl", [
            {"elapsed_seconds": 1, "events_per_sec": 100, "received_monotonic": 10},
        ])
        workload = [{"kind": "background_start", "started_ns": 99 * NS, "finished_ns": 100 * NS,
                     "pid": 12, "start_ticks": 300}]
        audits = audit_windows(self.directory, record("run-mixed", "mixed", 2000, workload=workload, background=True),
                               self.profile)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["workload_phase"], "mixed")
        self.assertIsNone(audits[0]["background_eps"])
        self.assertIn("background_coverage_insufficient", audits[0]["issues"])
        self.assertEqual(audits[0]["background_coverage"], 0)


class SurveySelection(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.profile = load_profile(PROFILE_PATH)

    def test_profile_matches_frozen_values(self):
        profile = self.profile
        self.assertEqual(profile["schema_version"], 1)
        self.assertEqual(profile["seed"], 20261021)
        self.assertEqual(profile["conditions"], ["default", "fixed-1000", "adaptive"])
        self.assertEqual(profile["service_cpus"], [0])
        self.assertEqual(profile["client_cpus"], [1])
        self.assertEqual(profile["controller_cpus"], [2])
        self.assertEqual(profile["port"], 18088)
        self.assertEqual(profile["page_bytes"], 4096)
        self.assertEqual(profile["connections"], 32)
        self.assertEqual(profile["window_ms"], 5000)
        self.assertEqual(profile["calibration_timeout_seconds"], 25)
        self.assertEqual(profile["client_timeout_seconds"], 3)
        self.assertEqual(profile["episode_timeout_seconds"], 130)
        self.assertEqual(profile["sample_seconds"], 1)
        self.assertEqual(profile["survey_rates"], [2000, 4000, 8000, 16000, 32000, 64000, 128000])
        self.assertEqual(profile["survey_duration_seconds"], 50)
        self.assertEqual(profile["survey_required_windows"], 3)
        self.assertEqual(profile["completion_min"], 0.97)
        self.assertEqual(profile["completion_max"], 1.03)
        self.assertEqual(profile["client_cpu_limit_percent"], 85)
        self.assertEqual(profile["min_background_coverage"], 0.8)
        self.assertEqual(profile["max_background_delivery_delay_seconds"], 2)
        self.assertEqual(profile["pilot_service_windows"], 4)
        self.assertEqual(profile["pilot_mixed_windows"], 4)
        self.assertEqual(profile["pilot_dynamic_phase_windows"], 4)
        self.assertEqual(profile["host_quality"], {
            "sample_seconds": 5, "preflight_seconds": 30, "preflight_cpu_limit": 100,
            "during_cpu_limit": 100, "maximum_sample_gap_seconds": 8,
            "maximum_preflight_wait_seconds": 120, "maximum_attempts": 2})

    def test_example_rates_floor_to_100(self):
        trials = [
            (record("svc-64000", "service", 64000), audit("svc-64000", True)),
            (record("mix-32000", "mixed", 32000), audit("mix-32000", True)),
        ]
        selected = select_rates(trials, self.profile)
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["service_reference_rps"], 64000)
        self.assertEqual(selected["mixed_reference_rps"], 32000)
        self.assertEqual(selected["rates"], {"low": 12800, "mid": 22400, "high": 28800})
        self.assertFalse(selected["capacity_bracketed"]["service"])
        self.assertFalse(selected["capacity_bracketed"]["mixed"])

    def test_completion_failure_brackets_when_client_is_not_saturated(self):
        trials = [
            (record("svc-32000", "service", 32000), audit("svc-32000", True)),
            (record("svc-64000", "service", 64000), audit("svc-64000", False)),
            (record("mix-16000", "mixed", 16000), audit("mix-16000", True)),
            (record("mix-32000", "mixed", 32000), audit("mix-32000", False)),
        ]
        selected = select_rates(trials, self.profile)
        self.assertEqual(selected["service_reference_rps"], 32000)
        self.assertEqual(selected["mixed_reference_rps"], 16000)
        self.assertTrue(selected["capacity_bracketed"]["service"])
        self.assertTrue(selected["capacity_bracketed"]["mixed"])
        self.assertEqual(selected["rates"], {"low": 6400, "mid": 11200, "high": 14400})

    def test_client_saturation_is_not_a_capacity_bracket(self):
        trials = [
            (record("svc-32000", "service", 32000), audit("svc-32000", True)),
            (record("svc-64000", "service", 64000), audit("svc-64000", False, cpu=True)),
            (record("mix-32000", "mixed", 32000), audit("mix-32000", True)),
        ]
        selected = select_rates(trials, self.profile)
        self.assertFalse(selected["capacity_bracketed"]["service"])
        self.assertFalse(selected["capacity_bracketed"]["mixed"])
        self.assertEqual(selected["service_reference_rps"], 32000)
        self.assertIn("service: client saturated", selected["limitations"])

    def test_insufficient_windows_leave_sustained_null(self):
        write_stream(self.directory, service_records(100, 2000, 1))
        side_logs(self.directory, end=105)
        write_samples(self.directory / "samples.jsonl", 100, 105, 10)
        result = audit_trial(self.directory, record("run-short", "service", 2000), self.profile)
        self.assertTrue(result["measurement_passed"])
        self.assertTrue(result["execution_passed"])
        self.assertIsNone(result["sustained"])
        self.assertIn("survey_windows_insufficient", result["issues"])
        selected = select_rates([(record("run-short", "service", 2000), result)], self.profile)
        self.assertEqual(selected["status"], "insufficient")
        self.assertIsNone(selected["service_reference_rps"])
        self.assertFalse(selected["capacity_bracketed"]["service"])

    def test_observed_client_saturation_does_not_bracket(self):
        write_stream(self.directory, service_records(100, 4000, 3))
        side_logs(self.directory, end=115)
        write_samples(self.directory / "samples.jsonl", 100, 115, 90)
        hot = audit_trial(self.directory, record("run-hot", "service", 4000), self.profile)
        self.assertTrue(hot["measurement_passed"])
        self.assertTrue(hot["client_cpu_limited"])
        self.assertFalse(hot["sustained"])
        self.assertIn("client_cpu_limited", hot["performance_flags"])
        selected = select_rates([
            (record("run-low", "service", 2000), audit("run-low", True)),
            (record("run-hot", "service", 4000), hot),
            (record("mix-2000", "mixed", 2000), audit("mix-2000", True)),
        ], self.profile)
        self.assertFalse(selected["capacity_bracketed"]["service"])
        self.assertEqual(selected["service_reference_rps"], 2000)


class RequestAndHostTime(unittest.TestCase):
    def test_request_operation_rules(self):
        profile = load_profile(PROFILE_PATH)
        digest = "a" * 64
        preflight = {"schema_version": 1, "operation": "preflight", "run_key": "run_1", "profile": profile,
                     "rates": None, "order": [], "expected_boot_id": None, "expected_source_sha256": {},
                     "expected_binary_sha256": {}}
        self.assertEqual(validate_request(preflight)["operation"], "preflight")
        pilot = {"schema_version": 1, "operation": "pilot", "run_key": "run_1", "profile": profile,
                 "rates": {"low": 12800, "mid": 22400, "high": 28800},
                 "order": ["adaptive", "default", "fixed-1000"], "expected_boot_id": "boot",
                 "expected_source_sha256": {"oe-resource-agent/oeagent/measurement.py": digest},
                 "expected_binary_sha256": {"native": digest, "wrk2": digest}}
        self.assertEqual(validate_request(pilot)["order"][0], "adaptive")
        with self.assertRaises(ValueError):
            validate_request({**preflight, "run_key": "../escape"})

    def test_utc_gate_still_rejects_limit_and_gap(self):
        def sample(stamp, cpu):
            return {"utc": datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat(),
                    "host_cpu_percent": cpu}
        self.assertTrue(host_gate([sample(t, 10) for t in range(0, 31, 5)], 0, 30, 20)["passed"])
        self.assertIn("host_cpu_over_limit", host_gate([sample(0, 10), sample(5, 80)], 0, 5, 30)["issues"])
        self.assertIn("host_sampling_gap", host_gate([sample(0, 10), sample(5, 10)], 0, 20, 30)["issues"])

    def test_host_monotonic_gate_ignores_utc(self):
        points = [
            {"utc": "not-a-time", "host_monotonic_seconds": 0, "host_cpu_percent": 10},
            {"utc": "not-a-time", "host_monotonic_seconds": 5, "host_cpu_percent": 12},
            {"utc": "not-a-time", "host_monotonic_seconds": 10, "host_cpu_percent": 11},
        ]
        result = host_gate(points, 0, 10, 30, time_key="host_monotonic_seconds")
        self.assertTrue(result["passed"])
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["maximum_host_cpu_percent"], 12)
        hot = [dict(points[0]), dict(points[1], host_cpu_percent=90), dict(points[2])]
        self.assertIn("host_cpu_over_limit", host_gate(hot, 0, 10, 30, time_key="host_monotonic_seconds")["issues"])
        gapped = [points[0], dict(points[2], host_monotonic_seconds=20)]
        self.assertIn("host_sampling_gap", host_gate(gapped, 0, 20, 30, time_key="host_monotonic_seconds")["issues"])
        outside = [{"host_monotonic_seconds": -5, "host_cpu_percent": 99}, *points]
        self.assertTrue(host_gate(outside, 0, 10, 30, time_key="host_monotonic_seconds")["passed"])
        ignored = [{"host_monotonic_seconds": True, "host_cpu_percent": 1},
                   {"host_monotonic_seconds": "2026-01-01T00:00:00+00:00", "host_cpu_percent": 1}]
        self.assertFalse(host_gate(ignored, 0, 5, 30, time_key="host_monotonic_seconds")["passed"])
        self.assertFalse(host_gate(points, 0, 10, 30)["passed"])


if __name__ == "__main__":
    unittest.main()
