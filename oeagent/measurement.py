"""Pure baseline measurement contracts: parse, check, and calculate.

Reads only paths it is given. Does not start processes, read procfs, open SSH
sessions, change resources, or write reports.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Literal, Sequence, TypedDict, Union

Json = Union[None, bool, int, float, str, list["Json"], dict[str, "Json"]]
Condition = Literal["default", "fixed-1000", "adaptive"]
Scenario = Literal["service", "mixed", "dynamic"]
Operation = Literal["preflight", "survey", "pilot"]
Bins = list[tuple[int, int]]

_CONDITIONS = ("default", "fixed-1000", "adaptive")
_SCENARIOS = ("service", "mixed", "dynamic")
_OPERATIONS = ("preflight", "survey", "pilot")
_ERROR_KEYS = ("connect", "read", "write", "status", "timeout")
_STREAM_PREFIX = "OE_WRK2_STREAM "
_RUN_KEY = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_NATIVE_NAME = re.compile(r"native-(\d+)\.log$")
_MODES = {"default": "default", "fixed-1000": "fixed", "adaptive": "adaptive"}
_FIXED_SLICE_US = 1000
# Guest resource premise from section 4.5. Not the host gate and not a tunable.
_RESOURCE_COVERAGE_MIN = 0.8
_RESOURCE_GAP_MAX_SECONDS = 2.5
_CLOCK_TOLERANCE_NS = 1000
_CLOCK_TOLERANCE_SECONDS = 1e-6
_SIGNAL_RETURN_MIN = -64
_MEASUREMENT_WINDOW_ISSUES = {
    "histogram_overflow",
    "background_log_missing",
    "background_intervals_invalid",
    "agent_events_missing",
    "agent_events_malformed",
    "policy_event_invalid",
}


class ErrorCounts(TypedDict):
    connect: int
    read: int
    write: int
    status: int
    timeout: int


class HostQualityConfig(TypedDict):
    sample_seconds: int
    preflight_seconds: int
    preflight_cpu_limit: float
    during_cpu_limit: float
    maximum_sample_gap_seconds: float
    maximum_preflight_wait_seconds: int
    maximum_attempts: int


class BaselineProfile(TypedDict):
    schema_version: Literal[1]
    seed: int
    conditions: list[Condition]
    service_cpus: list[int]
    client_cpus: list[int]
    controller_cpus: list[int]
    port: int
    page_bytes: int
    connections: int
    window_ms: int
    calibration_timeout_seconds: int
    client_timeout_seconds: int
    episode_timeout_seconds: int
    sample_seconds: int
    survey_rates: list[int]
    survey_duration_seconds: int
    survey_required_windows: int
    completion_min: float
    completion_max: float
    client_cpu_limit_percent: float
    min_background_coverage: float
    max_background_delivery_delay_seconds: float
    pilot_service_windows: int
    pilot_mixed_windows: int
    pilot_dynamic_phase_windows: int
    host_quality: HostQualityConfig


class Ready(TypedDict):
    schema_version: Literal[1]
    kind: Literal["ready"]
    start_ns: int
    window_ms: int
    offered_rps: int
    connections: int
    client_threads: Literal[1]
    pipeline: Literal[1]
    histogram: Literal["wrk2_expected_latency_hdr_3sig"]


class Window(TypedDict):
    schema_version: Literal[1]
    kind: Literal["window"]
    seq: int
    start_ns: int
    end_ns: int
    final: bool
    requests: int
    bytes: int
    errors: ErrorCounts
    terminal_exclusions: int
    histogram_overflows: int
    bins_us: Bins


class Footer(TypedDict):
    schema_version: Literal[1]
    kind: Literal["footer"]
    start_ns: int
    end_ns: int
    windows: int
    requests: int
    bytes: int
    errors: ErrorCounts
    terminal_exclusions: int
    histogram_overflows: int
    bins_us: Bins
    export_duration_ns: int


class ClientStream(TypedDict):
    ready: Ready | None
    windows: list[Window]
    footer: Footer | None
    complete: bool
    issues: list[str]


class WindowStats(TypedDict):
    duration_seconds: float
    requests: int
    latency_samples: int
    rps: float
    completion_fraction: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    bins_us: Bins


class HostSample(TypedDict):
    utc: str
    elapsed_seconds: float
    collector_pid: int
    host_cpu_percent: float | None
    host_monotonic_seconds: float
    vmware: list[dict[str, Json]]
    hyperv: dict[str, Json] | None
    error: str | None
    hyperv_error: str | None


class Gate(TypedDict):
    passed: bool
    issues: list[str]
    sample_count: int
    maximum_host_cpu_percent: float | None
    limit_percent: float
    maximum_sample_gap_seconds: float | None
    scope: str


class GuestSnapshot(TypedDict):
    schema_version: Literal[1]
    boot_id: str
    virtualization: str
    environment: dict[str, Json]
    native_build: dict[str, Json]
    wrk2_build: dict[str, Json]
    source_sha256: dict[str, str]
    checks: dict[str, bool]
    limitations: list[str]


class Request(TypedDict):
    schema_version: Literal[1]
    operation: Operation
    run_key: str
    profile: BaselineProfile
    rates: dict[str, int] | None
    order: list[Condition]
    expected_boot_id: str | None
    expected_source_sha256: dict[str, str]
    expected_binary_sha256: dict[str, str]


class WorkloadEvent(TypedDict):
    kind: Literal["background_start", "background_stop"]
    started_ns: int
    finished_ns: int
    pid: int
    start_ticks: int


class TrialRecord(TypedDict):
    trial_id: str
    condition: Condition
    scenario: Scenario
    rate: int
    client_log: str
    samples_log: str
    background_log: str | None
    background_intervals_log: str | None
    agent_events_log: str
    driver_events_log: str
    workload_events: list[WorkloadEvent]
    process_returncodes: dict[str, int | None]
    cleanup_errors: list[str]
    error: str | None


class WindowAudit(TypedDict):
    seq: int
    stats: WindowStats
    background_eps: float | None
    background_coverage: float | None
    background_delivery_delay_seconds: float | None
    workload_phase: Literal["service", "mixed", "transition"]
    workload_transition: bool
    policy_transition: bool
    issues: list[str]


class TrialAudit(TypedDict):
    trial_id: str
    measurement_passed: bool
    execution_passed: bool
    issues: list[str]
    stats: WindowStats | None
    sustained: bool | None
    client_cpu_limited: bool
    transition_coverage: Literal["observed", "not_triggered", "not_applicable"]
    performance_flags: list[str]
    windows: list[WindowAudit]


class RateSelection(TypedDict):
    schema_version: Literal[1]
    status: Literal["selected", "insufficient"]
    service_reference_rps: int | None
    mixed_reference_rps: int | None
    capacity_bracketed: dict[str, bool]
    rates: dict[str, int]
    source_trial_ids: list[str]
    limitations: list[str]


class GuestSummary(TypedDict):
    schema_version: Literal[1]
    run_key: str
    operation: Operation
    status: Literal["running", "completed", "failed", "interrupted"]
    snapshot: GuestSnapshot | None
    trials: list[TrialRecord]
    final_sched_ext: str
    cleanup_errors: list[str]
    error: str | None


class Attempt(TypedDict):
    run_key: str
    operation: Literal["survey", "pilot"]
    index: int
    order: list[Condition]
    preflight_start: float
    preflight_end: float
    host_start: float
    host_end: float | None
    guest_returncode: int | None
    fetch_returncode: int | None
    archive: str | None
    archive_sha256: str | None
    preflight_gate: Gate
    run_gate: Gate | None
    status: Literal["running", "completed", "host_rejected", "execution_failed", "interrupted"]
    error: str | None


class StudyManifest(TypedDict):
    schema_version: Literal[1]
    study_id: str
    mode: Literal["survey", "pilot"]
    host: str
    guest_root: str
    profile: BaselineProfile
    calibration: RateSelection | None
    calibration_source: str | None
    guest_preflight: GuestSnapshot | None
    host_environment: dict[str, Json]
    host_source_sha256: dict[str, str]
    attempts: list[Attempt]
    status: Literal["running", "completed", "quality_incomplete", "failed", "interrupted"]
    collector_returncode: int | None
    error: str | None


class StudyAudit(TypedDict):
    schema_version: Literal[1]
    status: Literal["passed", "quality_incomplete", "failed"]
    selected_run_key: str | None
    issues: list[str]
    trials: list[TrialAudit]
    calibration: RateSelection | None
    limitations: list[str]


def _append(items, code):
    if code not in items:
        items.append(code)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _require_int(value, label, minimum=None, maximum=None):
    if not _is_int(value):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} is below {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} is above {maximum}")
    return value


def _require_number(value, label, minimum=None, maximum=None):
    if not _is_number(value):
        raise ValueError(f"{label} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} is below {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} is above {maximum}")
    return value


def _object(value, keys, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) != set(keys):
        raise ValueError(f"{label} fields mismatch")
    return value


def _require_list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _ints(value, label, minimum=0):
    rows = _require_list(value, label)
    if not rows:
        raise ValueError(f"{label} must not be empty")
    return [_require_int(item, label, minimum=minimum) for item in rows]


def _relative_path(value, label, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")) or ":" in value:
        raise ValueError(f"{label} must be a relative path")
    parts = Path(value).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{label} must stay inside the trial directory")
    return value


def _sha_map(value, label, *, empty=False, keys=None):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if keys is not None and set(value) != set(keys):
        raise ValueError(f"{label} keys mismatch")
    if empty and value:
        raise ValueError(f"{label} must be empty")
    if keys is None and not empty and not value:
        raise ValueError(f"{label} must not be empty")
    for key, digest in value.items():
        if not isinstance(key, str) or not key or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError(f"{label} entries must be sha256 hex")
    return value


def _validate_bins(value):
    rows = _require_list(value, "bins_us")
    if len(rows) > 100000:
        raise ValueError("bins_us is oversized")
    previous = -1
    bins = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("bins_us entries must be pairs")
        bucket, count = row
        if not _is_int(bucket) or not _is_int(count) or bucket < 0 or bucket <= previous or count <= 0:
            raise ValueError("bins_us requires ascending unique values and positive counts")
        previous = bucket
        bins.append([bucket, count])
    return bins


def _validate_errors(value):
    _object(value, _ERROR_KEYS, "errors")
    return {key: _require_int(value[key], f"errors.{key}", minimum=0) for key in _ERROR_KEYS}


def _error_total(errors):
    return sum(errors[key] for key in _ERROR_KEYS)


def _validate_quality(value):
    _object(value, (
        "sample_seconds", "preflight_seconds", "preflight_cpu_limit", "during_cpu_limit",
        "maximum_sample_gap_seconds", "maximum_preflight_wait_seconds", "maximum_attempts"), "host_quality")
    _require_int(value["sample_seconds"], "host_quality.sample_seconds", minimum=1)
    _require_int(value["preflight_seconds"], "host_quality.preflight_seconds", minimum=1)
    _require_number(value["preflight_cpu_limit"], "host_quality.preflight_cpu_limit", minimum=0, maximum=100)
    _require_number(value["during_cpu_limit"], "host_quality.during_cpu_limit", minimum=0, maximum=100)
    _require_number(value["maximum_sample_gap_seconds"], "host_quality.maximum_sample_gap_seconds", minimum=0)
    if value["maximum_sample_gap_seconds"] <= 0:
        raise ValueError("host_quality.maximum_sample_gap_seconds must be positive")
    _require_int(value["maximum_preflight_wait_seconds"], "host_quality.maximum_preflight_wait_seconds", minimum=1)
    _require_int(value["maximum_attempts"], "host_quality.maximum_attempts", minimum=1)
    return value


def _validate_profile(value):
    _object(value, (
        "schema_version", "seed", "conditions", "service_cpus", "client_cpus", "controller_cpus", "port",
        "page_bytes", "connections", "window_ms", "calibration_timeout_seconds", "client_timeout_seconds",
        "episode_timeout_seconds", "sample_seconds", "survey_rates", "survey_duration_seconds",
        "survey_required_windows", "completion_min", "completion_max", "client_cpu_limit_percent",
        "min_background_coverage", "max_background_delivery_delay_seconds", "pilot_service_windows",
        "pilot_mixed_windows", "pilot_dynamic_phase_windows", "host_quality"), "profile")
    if value["schema_version"] != 1:
        raise ValueError("profile schema_version must be 1")
    _require_int(value["seed"], "seed")
    conditions = _require_list(value["conditions"], "conditions")
    if not conditions or len(set(conditions)) != len(conditions) or any(item not in _CONDITIONS for item in conditions):
        raise ValueError("conditions must be unique known conditions")
    _ints(value["service_cpus"], "service_cpus")
    _ints(value["client_cpus"], "client_cpus")
    _ints(value["controller_cpus"], "controller_cpus")
    _require_int(value["port"], "port", minimum=1, maximum=65535)
    _require_int(value["page_bytes"], "page_bytes", minimum=1)
    _require_int(value["connections"], "connections", minimum=1)
    _require_int(value["window_ms"], "window_ms", minimum=1)
    for label in ("calibration_timeout_seconds", "client_timeout_seconds", "episode_timeout_seconds",
                  "sample_seconds", "survey_duration_seconds", "survey_required_windows",
                  "pilot_service_windows", "pilot_mixed_windows", "pilot_dynamic_phase_windows"):
        _require_int(value[label], label, minimum=1)
    previous = 0
    rates = _require_list(value["survey_rates"], "survey_rates")
    if not rates:
        raise ValueError("survey_rates must not be empty")
    for rate in rates:
        _require_int(rate, "survey_rates", minimum=1)
        if rate <= previous:
            raise ValueError("survey_rates must be strictly increasing")
        previous = rate
    _require_number(value["completion_min"], "completion_min", minimum=0)
    _require_number(value["completion_max"], "completion_max", minimum=0)
    if value["completion_min"] > value["completion_max"]:
        raise ValueError("completion range is reversed")
    _require_number(value["client_cpu_limit_percent"], "client_cpu_limit_percent", minimum=0, maximum=100)
    _require_number(value["min_background_coverage"], "min_background_coverage", minimum=0, maximum=1)
    _require_number(value["max_background_delivery_delay_seconds"], "max_background_delivery_delay_seconds", minimum=0)
    _validate_quality(value["host_quality"])
    return value


def _validate_workload_event(value):
    _object(value, ("kind", "started_ns", "finished_ns", "pid", "start_ticks"), "workload_events")
    if value["kind"] not in ("background_start", "background_stop"):
        raise ValueError("workload event kind is invalid")
    _require_int(value["started_ns"], "workload started_ns", minimum=0)
    _require_int(value["finished_ns"], "workload finished_ns", minimum=0)
    if value["finished_ns"] < value["started_ns"]:
        raise ValueError("workload event ends before it starts")
    _require_int(value["pid"], "workload pid", minimum=1)
    _require_int(value["start_ticks"], "workload start_ticks", minimum=0)
    return value


def _validate_trial_record(value):
    _object(value, (
        "trial_id", "condition", "scenario", "rate", "client_log", "samples_log", "background_log",
        "background_intervals_log", "agent_events_log", "driver_events_log", "workload_events",
        "process_returncodes", "cleanup_errors", "error"), "trial")
    if not isinstance(value["trial_id"], str) or not value["trial_id"] or "/" in value["trial_id"] or "\\" in value["trial_id"]:
        raise ValueError("trial_id is invalid")
    if value["condition"] not in _CONDITIONS or value["scenario"] not in _SCENARIOS:
        raise ValueError("trial condition or scenario is invalid")
    _require_int(value["rate"], "rate", minimum=1)
    _relative_path(value["client_log"], "client_log")
    _relative_path(value["samples_log"], "samples_log")
    _relative_path(value["background_log"], "background_log", nullable=True)
    _relative_path(value["background_intervals_log"], "background_intervals_log", nullable=True)
    _relative_path(value["agent_events_log"], "agent_events_log")
    _relative_path(value["driver_events_log"], "driver_events_log")
    events = _require_list(value["workload_events"], "workload_events")
    for event in events:
        _validate_workload_event(event)
    codes = value["process_returncodes"]
    if not isinstance(codes, dict):
        raise ValueError("process_returncodes must be an object")
    for key, code in codes.items():
        if not isinstance(key, str) or not key:
            raise ValueError("process_returncodes keys must be strings")
        if code is not None and not _is_int(code):
            raise ValueError("process_returncodes values must be integers or null")
    errors = _require_list(value["cleanup_errors"], "cleanup_errors")
    if any(not isinstance(item, str) for item in errors):
        raise ValueError("cleanup_errors must be strings")
    if value["error"] is not None and not isinstance(value["error"], str):
        raise ValueError("trial error must be a string or null")
    return value


def _validate_window_stats(value):
    _object(value, (
        "duration_seconds", "requests", "latency_samples", "rps", "completion_fraction",
        "p50_ms", "p95_ms", "p99_ms", "bins_us"), "stats")
    _require_number(value["duration_seconds"], "duration_seconds", minimum=0)
    _require_int(value["requests"], "requests", minimum=0)
    _require_int(value["latency_samples"], "latency_samples", minimum=0)
    _require_number(value["rps"], "rps", minimum=0)
    _require_number(value["completion_fraction"], "completion_fraction", minimum=0)
    for label in ("p50_ms", "p95_ms", "p99_ms"):
        if value[label] is not None:
            _require_number(value[label], label, minimum=0)
    _validate_bins(value["bins_us"])
    return value


def _validate_window_audit(value):
    _object(value, (
        "seq", "stats", "background_eps", "background_coverage", "background_delivery_delay_seconds",
        "workload_phase", "workload_transition", "policy_transition", "issues"), "window audit")
    _require_int(value["seq"], "seq", minimum=1)
    _validate_window_stats(value["stats"])
    for label in ("background_eps", "background_coverage", "background_delivery_delay_seconds"):
        if value[label] is not None:
            _require_number(value[label], label, minimum=0)
    if value["workload_phase"] not in ("service", "mixed", "transition"):
        raise ValueError("workload_phase is invalid")
    if not isinstance(value["workload_transition"], bool) or not isinstance(value["policy_transition"], bool):
        raise ValueError("transition flags must be boolean")
    if not isinstance(value["issues"], list) or any(not isinstance(item, str) for item in value["issues"]):
        raise ValueError("window issues must be strings")
    return value


def _validate_trial_audit(value):
    _object(value, (
        "trial_id", "measurement_passed", "execution_passed", "issues", "stats", "sustained",
        "client_cpu_limited", "transition_coverage", "performance_flags", "windows"), "trial audit")
    if not isinstance(value["trial_id"], str) or not value["trial_id"]:
        raise ValueError("trial audit id is invalid")
    if not isinstance(value["measurement_passed"], bool) or not isinstance(value["execution_passed"], bool):
        raise ValueError("trial audit pass flags must be boolean")
    if not isinstance(value["issues"], list) or any(not isinstance(item, str) for item in value["issues"]):
        raise ValueError("trial audit issues must be strings")
    if value["stats"] is not None:
        _validate_window_stats(value["stats"])
    if value["sustained"] is not None and not isinstance(value["sustained"], bool):
        raise ValueError("sustained must be boolean or null")
    if not isinstance(value["client_cpu_limited"], bool):
        raise ValueError("client_cpu_limited must be boolean")
    if value["transition_coverage"] not in ("observed", "not_triggered", "not_applicable"):
        raise ValueError("transition_coverage is invalid")
    if not isinstance(value["performance_flags"], list) or any(not isinstance(item, str) for item in value["performance_flags"]):
        raise ValueError("performance_flags must be strings")
    windows = _require_list(value["windows"], "windows")
    for window in windows:
        _validate_window_audit(window)
    return value


def load_profile(path: Path) -> BaselineProfile:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load profile: {exc}") from exc
    return _validate_profile(value)


def validate_request(value: object) -> Request:
    _object(value, (
        "schema_version", "operation", "run_key", "profile", "rates", "order", "expected_boot_id",
        "expected_source_sha256", "expected_binary_sha256"), "request")
    if value["schema_version"] != 1:
        raise ValueError("request schema_version must be 1")
    if value["operation"] not in _OPERATIONS:
        raise ValueError("operation is invalid")
    if not isinstance(value["run_key"], str) or not _RUN_KEY.fullmatch(value["run_key"]):
        raise ValueError("run_key is invalid")
    profile = _validate_profile(value["profile"])
    order = _require_list(value["order"], "order")
    operation = value["operation"]
    if operation == "pilot":
        if value["rates"] is None or not isinstance(value["rates"], dict):
            raise ValueError("pilot rates must be low, mid, and high")
        if set(value["rates"]) != {"low", "mid", "high"}:
            raise ValueError("pilot rates must be low, mid, and high")
        for key in ("low", "mid", "high"):
            _require_int(value["rates"][key], f"rates.{key}", minimum=1)
        if not value["rates"]["low"] < value["rates"]["mid"] < value["rates"]["high"]:
            raise ValueError("pilot rates must be strictly increasing")
        if sorted(order) != sorted(profile["conditions"]) or set(order) != set(_CONDITIONS):
            raise ValueError("pilot order must be a permutation of the three conditions")
        if not isinstance(value["expected_boot_id"], str) or not value["expected_boot_id"]:
            raise ValueError("expected_boot_id is required after preflight")
        _sha_map(value["expected_source_sha256"], "expected_source_sha256")
        _sha_map(value["expected_binary_sha256"], "expected_binary_sha256", keys=("native", "wrk2"))
    else:
        if value["rates"] is not None or order:
            raise ValueError("only pilot sets rates and condition order")
        if operation == "preflight":
            if value["expected_boot_id"] is not None:
                raise ValueError("preflight expected_boot_id must be null")
            _sha_map(value["expected_source_sha256"], "expected_source_sha256", empty=True)
            _sha_map(value["expected_binary_sha256"], "expected_binary_sha256", empty=True)
        else:
            if not isinstance(value["expected_boot_id"], str) or not value["expected_boot_id"]:
                raise ValueError("expected_boot_id is required after preflight")
            _sha_map(value["expected_source_sha256"], "expected_source_sha256")
            _sha_map(value["expected_binary_sha256"], "expected_binary_sha256", keys=("native", "wrk2"))
    return value


def _counts_hold(record):
    counted = sum(count for _, count in record["bins_us"])
    return record["requests"] == counted + record["terminal_exclusions"] + record["histogram_overflows"]


def _validate_ready(value):
    _object(value, (
        "schema_version", "kind", "start_ns", "window_ms", "offered_rps", "connections",
        "client_threads", "pipeline", "histogram"), "ready")
    if value["schema_version"] != 1 or value["kind"] != "ready":
        raise ValueError("invalid_stream_record")
    _require_int(value["start_ns"], "start_ns", minimum=0)
    _require_int(value["window_ms"], "window_ms", minimum=1)
    _require_int(value["offered_rps"], "offered_rps", minimum=1)
    _require_int(value["connections"], "connections", minimum=1)
    if value["client_threads"] != 1 or value["pipeline"] != 1:
        raise ValueError("invalid_stream_record")
    if value["histogram"] != "wrk2_expected_latency_hdr_3sig":
        raise ValueError("invalid_stream_record")
    return value


def _validate_window_record(value):
    _object(value, (
        "schema_version", "kind", "seq", "start_ns", "end_ns", "final", "requests", "bytes", "errors",
        "terminal_exclusions", "histogram_overflows", "bins_us"), "window")
    if value["schema_version"] != 1 or value["kind"] != "window":
        raise ValueError("invalid_stream_record")
    _require_int(value["seq"], "seq", minimum=1)
    _require_int(value["start_ns"], "start_ns", minimum=0)
    _require_int(value["end_ns"], "end_ns", minimum=0)
    if not isinstance(value["final"], bool):
        raise ValueError("invalid_stream_record")
    _require_int(value["requests"], "requests", minimum=0)
    _require_int(value["bytes"], "bytes", minimum=0)
    _validate_errors(value["errors"])
    _require_int(value["terminal_exclusions"], "terminal_exclusions", minimum=0)
    _require_int(value["histogram_overflows"], "histogram_overflows", minimum=0)
    value["bins_us"] = _validate_bins(value["bins_us"])
    return value


def _validate_footer(value):
    _object(value, (
        "schema_version", "kind", "start_ns", "end_ns", "windows", "requests", "bytes", "errors",
        "terminal_exclusions", "histogram_overflows", "bins_us", "export_duration_ns"), "footer")
    if value["schema_version"] != 1 or value["kind"] != "footer":
        raise ValueError("invalid_stream_record")
    _require_int(value["start_ns"], "start_ns", minimum=0)
    _require_int(value["end_ns"], "end_ns", minimum=0)
    _require_int(value["windows"], "windows", minimum=0)
    _require_int(value["requests"], "requests", minimum=0)
    _require_int(value["bytes"], "bytes", minimum=0)
    _validate_errors(value["errors"])
    _require_int(value["terminal_exclusions"], "terminal_exclusions", minimum=0)
    _require_int(value["histogram_overflows"], "histogram_overflows", minimum=0)
    value["bins_us"] = _validate_bins(value["bins_us"])
    _require_int(value["export_duration_ns"], "export_duration_ns", minimum=0)
    return value


def _validate_stream_record(value):
    if not isinstance(value, dict) or value.get("kind") not in ("ready", "window", "footer"):
        raise ValueError("invalid_stream_record")
    if value["kind"] == "ready":
        return _validate_ready(value)
    if value["kind"] == "window":
        return _validate_window_record(value)
    return _validate_footer(value)


def _merged_bins(windows):
    counts = {}
    for window in windows:
        for bucket, count in window["bins_us"]:
            counts[bucket] = counts.get(bucket, 0) + count
    return [[bucket, counts[bucket]] for bucket in sorted(counts)]


def _same_errors(left, right):
    return all(left[key] == right[key] for key in _ERROR_KEYS)


def _footer_matches(ready, windows, footer):
    if not _counts_hold(footer):
        return False
    if footer["windows"] != len(windows):
        return False
    if footer["start_ns"] != ready["start_ns"]:
        return False
    if windows:
        if footer["start_ns"] != windows[0]["start_ns"] or footer["end_ns"] != windows[-1]["end_ns"]:
            return False
    elif footer["end_ns"] != footer["start_ns"]:
        return False
    if footer["requests"] != sum(window["requests"] for window in windows):
        return False
    if footer["bytes"] != sum(window["bytes"] for window in windows):
        return False
    if footer["terminal_exclusions"] != sum(window["terminal_exclusions"] for window in windows):
        return False
    if footer["histogram_overflows"] != sum(window["histogram_overflows"] for window in windows):
        return False
    summed = {key: sum(window["errors"][key] for window in windows) for key in _ERROR_KEYS}
    if not _same_errors(summed, footer["errors"]):
        return False
    return footer["bins_us"] == _merged_bins(windows)


def _assemble(records):
    ready, windows, footer = None, [], None
    issues = []
    for record in records:
        kind = record["kind"]
        if kind == "ready":
            if ready is not None or windows or footer is not None:
                _append(issues, "unexpected_ready")
                break
            ready = record
            continue
        if kind == "window":
            if ready is None:
                _append(issues, "missing_ready")
                break
            if footer is not None:
                _append(issues, "record_after_footer")
                break
            if any(window["final"] for window in windows):
                _append(issues, "final_not_last")
                break
            if record["seq"] != len(windows) + 1:
                _append(issues, "sequence_gap")
                break
            expected_start = windows[-1]["end_ns"] if windows else ready["start_ns"]
            if record["start_ns"] != expected_start:
                _append(issues, "timeline_discontinuity")
                break
            if record["end_ns"] <= record["start_ns"]:
                _append(issues, "non_positive_window")
                break
            if not _counts_hold(record):
                _append(issues, "window_count_invariant")
                break
            windows.append(record)
            continue
        if footer is not None:
            _append(issues, "unexpected_footer")
            break
        if ready is None:
            _append(issues, "missing_ready")
            break
        footer = record
    if ready is None:
        _append(issues, "missing_ready")
    if footer is None:
        _append(issues, "missing_footer")
    elif ready is not None and not _footer_matches(ready, windows, footer):
        _append(issues, "footer_mismatch")
    return ready, windows, footer, issues


def parse_client_stream(path: Path, *, allow_incomplete: bool = False) -> ClientStream:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("client_log_unreadable") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("client_log_not_utf8") from exc
    issues = []
    if raw and not raw.endswith(b"\n"):
        _append(issues, "truncated_line")
        newline = text.rfind("\n")
        text = text[:newline + 1] if newline >= 0 else ""
    records = []
    for line in text.splitlines():
        if not line.startswith(_STREAM_PREFIX):
            continue
        try:
            item = json.loads(line[len(_STREAM_PREFIX):])
        except json.JSONDecodeError:
            _append(issues, "malformed_stream_json")
            break
        try:
            records.append(_validate_stream_record(item))
        except ValueError:
            _append(issues, "invalid_stream_record")
            break
    ready, windows, footer, assemble_issues = _assemble(records)
    for issue in assemble_issues:
        _append(issues, issue)
    complete = not issues
    if issues and not allow_incomplete:
        raise ValueError(issues[0])
    return {"ready": ready, "windows": windows, "footer": footer, "complete": complete, "issues": issues}


def _hdr_value_us(bins, percentile):
    """Match hdr_value_at_percentile: max(1, trunc(N * p / 100 + 0.5))."""
    total = sum(count for _, count in bins)
    if total <= 0:
        return None
    requested = percentile if percentile < 100 else 100.0
    rank = int((requested / 100.0) * total + 0.5)
    if rank < 1:
        rank = 1
    cumulative = 0
    for value, count in bins:
        cumulative += count
        if cumulative >= rank:
            return value
    return 0


def summarize_windows(windows: Sequence[Window], offered_rps: int) -> WindowStats:
    offered_rps = _require_int(offered_rps, "offered_rps", minimum=1)
    if not isinstance(windows, Sequence) or isinstance(windows, (str, bytes)) or not windows:
        raise ValueError("windows must be a non-empty sequence")
    requests = 0
    duration_ns = 0
    counts = {}
    for window in windows:
        _require_int(window["requests"], "requests", minimum=0)
        _require_int(window["start_ns"], "start_ns", minimum=0)
        _require_int(window["end_ns"], "end_ns", minimum=0)
        if window["end_ns"] <= window["start_ns"]:
            raise ValueError("non_positive_window")
        requests += window["requests"]
        duration_ns += window["end_ns"] - window["start_ns"]
        for bucket, count in _validate_bins(window["bins_us"]):
            counts[bucket] = counts.get(bucket, 0) + count
    if duration_ns <= 0:
        raise ValueError("non_positive_window")
    bins = [[bucket, counts[bucket]] for bucket in sorted(counts)]
    samples = sum(count for _, count in bins)
    duration = duration_ns / 1_000_000_000
    rps = requests / duration
    percentiles = {}
    for percent in (50, 95, 99):
        percentiles[f"p{percent}_ms"] = None if samples == 0 else _hdr_value_us(bins, percent) / 1000
    return {"duration_seconds": duration, "requests": requests, "latency_samples": samples, "rps": rps,
            "completion_fraction": rps / offered_rps, "bins_us": bins, **percentiles}


def _log_path(directory, relative):
    return Path(directory).joinpath(relative)


def _read_jsonl(path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("unreadable") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("not_utf8") from exc
    if raw and not raw.endswith(b"\n"):
        newline = text.rfind("\n")
        text = text[:newline + 1] if newline >= 0 else ""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed_jsonl") from exc
        if not isinstance(value, dict):
            raise ValueError("malformed_jsonl")
        rows.append(value)
    return rows


def _seconds_to_ns(value):
    number = _require_number(value, "monotonic")
    return int(round(float(number) * 1_000_000_000))


def _snap(value, edges):
    for edge in edges:
        if abs(value - edge) <= _CLOCK_TOLERANCE_NS:
            return edge
    return value


def _load_intervals(path):
    try:
        rows = _read_jsonl(path)
    except ValueError as exc:
        code = "background_log_missing" if str(exc) == "unreadable" else "background_intervals_invalid"
        return None, [code]
    points = []
    previous = None
    for row in rows:
        elapsed, eps, received = row.get("elapsed_seconds"), row.get("events_per_sec"), row.get("received_monotonic")
        if (not _is_number(elapsed) or not _is_number(eps) or not _is_number(received) or eps < 0
                or (previous is not None and not elapsed > previous)):
            return None, ["background_intervals_invalid"]
        previous = elapsed
        points.append({"elapsed_seconds": float(elapsed), "events_per_sec": float(eps),
                       "received_monotonic": float(received)})
    return points, []


def _interval_stats(points, start_s, end_s):
    if not points:
        return None, 0.0, None
    origin = min(point["received_monotonic"] - point["elapsed_seconds"] for point in points)
    delay = max(point["received_monotonic"] - origin - point["elapsed_seconds"] for point in points)
    previous = 0.0
    weighted = 0.0
    covered = 0.0
    for point in points:
        lo = origin + previous
        hi = origin + point["elapsed_seconds"]
        if start_s <= lo and hi <= end_s and hi > lo:
            weighted += point["events_per_sec"] * (hi - lo)
            covered += hi - lo
        previous = point["elapsed_seconds"]
    duration = end_s - start_s
    coverage = covered / duration if duration > 0 else 0.0
    eps = weighted / covered if covered else None
    return eps, coverage, delay


def _state_at(instant, events):
    phase = "service"
    ordered = sorted(events, key=lambda event: (event["started_ns"], 0 if event["kind"] == "background_start" else 1))
    for event in ordered:
        start, end = event["started_ns"], event["finished_ns"]
        if instant < start:
            return phase
        if start <= instant < end:
            return "transition"
        phase = "mixed" if event["kind"] == "background_start" else "service"
    return phase


def _workload_phase(window, events):
    start, end = window["start_ns"], window["end_ns"]
    boundaries = [value for event in events for value in (event["started_ns"], event["finished_ns"])]
    if any(start < boundary < end for boundary in boundaries):
        return "transition"
    return _state_at(start, events)


def _policy_actions(events):
    actions = []
    invalid = False
    for event in events:
        if event.get("event") != "policy_applied":
            continue
        try:
            start = _seconds_to_ns(event["action_started_monotonic"])
            end = _seconds_to_ns(event["action_finished_monotonic"])
            if "before" not in event or "after" not in event or "revision" not in event:
                raise ValueError("policy_event_invalid")
            if not isinstance(event["before"], dict) or not isinstance(event["after"], dict) or not _is_int(event["revision"]):
                raise ValueError("policy_event_invalid")
        except (KeyError, TypeError, ValueError):
            invalid = True
            continue
        actions.append({"start_ns": start, "end_ns": end, "before": event["before"], "after": event["after"],
                        "revision": event["revision"]})
    return actions, invalid


def _policy_transition(window, actions):
    start, end = window["start_ns"], window["end_ns"]
    for action in actions:
        action_start = _snap(action["start_ns"], (start, end))
        action_end = _snap(action["end_ns"], (start, end))
        overlaps = action_start < action_end and action_start < end and start < action_end
        contains_revision = start < action_end < end
        if overlaps or contains_revision:
            return True
    return False


def _background_eps(phase, points, point_issues, available, window, profile):
    if phase == "service":
        return None, None, None, []
    issues = []
    if not available or "background_log_missing" in point_issues:
        return None, None, None, ["background_log_missing"]
    if points is None:
        return None, None, None, list(point_issues) or ["background_intervals_invalid"]
    start_s = window["start_ns"] / 1_000_000_000
    end_s = window["end_ns"] / 1_000_000_000
    raw_eps, coverage, delay = _interval_stats(points, start_s, end_s)
    insufficient = coverage < profile["min_background_coverage"]
    late = delay is None or delay >= profile["max_background_delivery_delay_seconds"]
    if insufficient:
        issues.append("background_coverage_insufficient")
    if late:
        issues.append("background_delivery_delay")
    return (None if insufficient or late else raw_eps), coverage, delay, issues


def _audit_stream_windows(directory, record, profile, stream, events, event_issue, points, point_issues, available):
    offered = stream["ready"]["offered_rps"] if stream["ready"] else record["rate"]
    actions, policy_invalid = _policy_actions(events) if event_issue is None else ([], False)
    audits = []
    for window in stream["windows"]:
        phase = _workload_phase(window, record["workload_events"])
        issues = []
        if window["histogram_overflows"]:
            _append(issues, "histogram_overflow")
        if event_issue:
            _append(issues, event_issue)
        if policy_invalid:
            _append(issues, "policy_event_invalid")
        eps, coverage, delay, background_issues = _background_eps(
            phase, points, point_issues, available, window, profile)
        for issue in background_issues:
            _append(issues, issue)
        audits.append({
            "seq": window["seq"],
            "stats": summarize_windows([window], offered),
            "background_eps": eps,
            "background_coverage": coverage,
            "background_delivery_delay_seconds": delay,
            "workload_phase": phase,
            "workload_transition": phase == "transition",
            "policy_transition": _policy_transition(window, actions),
            "issues": issues,
        })
    return audits


def _load_agent_events(directory, record):
    path = _log_path(directory, record["agent_events_log"])
    if not path.is_file():
        return [], "agent_events_missing"
    try:
        return _read_jsonl(path), None
    except ValueError:
        return [], "agent_events_malformed"


def _background_inputs(directory, record):
    log, intervals = record["background_log"], record["background_intervals_log"]
    if log is None or intervals is None:
        return None, ["background_log_missing"], False
    log_path, interval_path = _log_path(directory, log), _log_path(directory, intervals)
    if not log_path.is_file() or not interval_path.is_file():
        return None, ["background_log_missing"], False
    points, issues = _load_intervals(interval_path)
    return points, issues, True


def audit_windows(directory: Path, record: TrialRecord, profile: BaselineProfile) -> list[WindowAudit]:
    directory = Path(directory)
    record = _validate_trial_record(record)
    profile = _validate_profile(profile)
    stream = parse_client_stream(_log_path(directory, record["client_log"]), allow_incomplete=True)
    events, event_issue = _load_agent_events(directory, record)
    points, point_issues, available = _background_inputs(directory, record)
    return _audit_stream_windows(directory, record, profile, stream, events, event_issue, points, point_issues, available)


def _is_survey_trial(record, profile):
    # Pilot rates are the 40/70/90 grid, not the frozen survey ladder.
    return (record["condition"] == "default" and record["scenario"] in ("service", "mixed")
            and record["rate"] in set(profile["survey_rates"]))


def _regular_windows(windows):
    regular = [window for window in windows if not window["final"]]
    return regular or list(windows)


def _evaluation_windows(windows, profile, survey):
    regular = [window for window in windows if not window["final"]]
    chosen = regular or list(windows)
    if not survey:
        return chosen
    needed = profile["survey_required_windows"]
    if len(regular) < needed:
        return regular
    return regular[-needed:]


def _sample_rows(path):
    if not path.is_file():
        return None, ["samples_missing"]
    try:
        rows = _read_jsonl(path)
    except ValueError:
        return None, ["samples_malformed"]
    issues = []
    for index, row in enumerate(rows):
        if not _is_number(row.get("monotonic")) or "processes" not in row or "sched_ext" not in row:
            _append(issues, "samples_invalid")
            continue
        if not isinstance(row["processes"], dict):
            _append(issues, "samples_invalid")
            continue
        if index == 0 and "sample_start_monotonic" not in row:
            continue
        if not _is_number(row.get("sample_start_monotonic")) or not _is_number(row.get("sample_interval_seconds")):
            _append(issues, "samples_invalid")
    if any(issue == "samples_invalid" for issue in issues):
        return None, issues
    return rows, issues


def _contained_samples(rows, start_s, end_s):
    contained = []
    if not rows:
        return contained
    for index, row in enumerate(rows):
        if index == 0 and "sample_start_monotonic" not in row:
            continue
        start = row.get("sample_start_monotonic")
        end = row.get("monotonic")
        if not _is_number(start) or not _is_number(end):
            continue
        if start >= start_s - _CLOCK_TOLERANCE_SECONDS and end <= end_s + _CLOCK_TOLERANCE_SECONDS and end > start:
            contained.append(row)
    return contained


def _client_sample(row):
    processes = row.get("processes")
    if not isinstance(processes, dict):
        return None
    client = processes.get("client")
    if not isinstance(client, dict):
        return None
    pid, ticks, cpu = client.get("pid"), client.get("start_ticks"), client.get("cpu_percent")
    if not _is_int(pid) or not _is_int(ticks) or not _is_number(cpu) or cpu < 0:
        return None
    return pid, ticks, float(cpu)


def _resource_premise(rows, windows, limit):
    if not windows:
        return ["resource_coverage_insufficient"], False, False
    start_s = windows[0]["start_ns"] / 1_000_000_000
    end_s = windows[-1]["end_ns"] / 1_000_000_000
    duration = end_s - start_s
    contained = _contained_samples(rows or [], start_s, end_s)
    identities = set()
    covered = 0.0
    limited = False
    for row in contained:
        sample = _client_sample(row)
        if sample is None:
            continue
        pid, ticks, cpu = sample
        identities.add((pid, ticks))
        covered += row["monotonic"] - row["sample_start_monotonic"]
        if cpu >= limit:
            limited = True
    issues = []
    if len(identities) != 1:
        _append(issues, "resource_identity_mismatch")
    coverage = covered / duration if duration > 0 else 0.0
    if coverage < _RESOURCE_COVERAGE_MIN:
        _append(issues, "resource_coverage_insufficient")
    stamps = [start_s]
    for row in rows or []:
        stamp = row.get("monotonic")
        if _is_number(stamp) and start_s - _CLOCK_TOLERANCE_SECONDS <= stamp <= end_s + _CLOCK_TOLERANCE_SECONDS:
            stamps.append(float(stamp))
    stamps.append(end_s)
    stamps.sort()
    if any(stamps[index + 1] - stamps[index] > _RESOURCE_GAP_MAX_SECONDS for index in range(len(stamps) - 1)):
        _append(issues, "resource_gap")
    return issues, limited, not issues


def _pair_processes(events):
    pending = {}
    paired = []
    issues = []
    for event in events:
        name = event.get("event")
        if name not in ("process_started", "process_stopped"):
            continue
        role, pid = event.get("role"), event.get("pid")
        if not isinstance(role, str) or not role or not _is_int(pid) or not _is_number(event.get("monotonic")):
            _append(issues, "driver_event_invalid")
            continue
        if name == "process_started":
            if not _is_int(event.get("start_ticks")) or "boot_id" not in event or "command" not in event:
                _append(issues, "driver_event_invalid")
                continue
            pending.setdefault((role, pid), []).append(event)
            continue
        if not _is_int(event.get("returncode")):
            _append(issues, "driver_event_invalid")
            continue
        queue = pending.get((role, pid)) or []
        if not queue:
            _append(issues, "unmatched_process_stop")
            continue
        paired.append((queue.pop(0), event))
    if any(queue for queue in pending.values()):
        _append(issues, "process_stop_missing")
    return paired, issues


def _return_allowed(role, code):
    if role == "client":
        return code == 0
    if role == "native":
        return code == 0
    if role in ("server", "background"):
        return code == 0 or _SIGNAL_RETURN_MIN <= code < 0
    return code == 0


def _check_processes(record, events):
    issues = []
    paired, pair_issues = _pair_processes(events)
    for issue in pair_issues:
        _append(issues, issue)
    expected = {}
    for start, stop in paired:
        key = f"{start['role']}:{start['pid']}:{start['start_ticks']}"
        if key in expected:
            _append(issues, "process_returncode_mismatch")
        expected[key] = stop["returncode"]
        if not _return_allowed(start["role"], stop["returncode"]):
            if start["role"] == "client":
                _append(issues, "client_returncode")
            elif start["role"] == "native":
                _append(issues, "native_returncode")
            else:
                _append(issues, "workload_returncode")
    recorded = record["process_returncodes"]
    if set(recorded) != set(expected) or any(recorded[key] != code for key, code in expected.items()):
        _append(issues, "process_returncode_mismatch")
    if not any(start["role"] == "client" for start, _stop in paired):
        _append(issues, "client_returncode")
    return issues


def _trial_directory(directory, record):
    relative = record.get("client_log") if isinstance(record, dict) else None
    if not isinstance(relative, str) or not relative:
        return Path(directory)
    return _log_path(directory, relative).parent


def _native_reports(directory):
    reports = []
    issues = []
    for path in sorted(Path(directory).glob("native-*.log")):
        match = _NATIVE_NAME.fullmatch(path.name)
        if not match:
            _append(issues, "native_log_invalid")
            continue
        configs, stats, exit_ok, broken = [], [], False, False
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            _append(issues, "native_log_invalid")
            continue
        for line in lines:
            try:
                if line.startswith("OE_ADAPTIVE_CONFIG "):
                    configs.append(json.loads(line.split(" ", 1)[1]))
                elif line.startswith("OE_ADAPTIVE_STATS "):
                    stats.append(json.loads(line.split(" ", 1)[1]))
            except (json.JSONDecodeError, IndexError):
                broken = True
            if "EXIT: unregistered from user space" in line:
                exit_ok = True
        if broken:
            _append(issues, "native_log_invalid")
        reports.append({"pid": int(match.group(1)), "configs": configs, "stats": stats, "exit_ok": exit_ok})
    return reports, issues


def _scx_after(after):
    if not isinstance(after, dict):
        return False
    required = ("policy_id", "slice_us", "pid", "map_id", "generation", "wakeup_preempt")
    if any(key not in after for key in required):
        return False
    if after["policy_id"] != "scx.slice" or not _is_int(after["slice_us"]) or not _is_int(after["pid"]):
        return False
    if not isinstance(after["wakeup_preempt"], bool):
        return False
    if isinstance(after["map_id"], bool) or isinstance(after["generation"], bool):
        return False
    if not isinstance(after["map_id"], (int, str)) or not isinstance(after["generation"], (int, str)):
        return False
    return True


def _default_after(after):
    return (isinstance(after, dict) and after.get("policy_id") == "kernel.default"
            and "slice_us" in after and "sched_ext" in after)


def _native_matches(report, slice_us, wakeup):
    if not report["configs"] or not report["stats"]:
        return False
    expected = slice_us * 1000
    for row in report["configs"] + report["stats"]:
        if not isinstance(row, dict) or row.get("slice_ns") != expected or row.get("wakeup_preempt") is not wakeup:
            return False
    return all(isinstance(row, dict) and row.get("accounting_errors") == 0 for row in report["stats"])


def _confirm_control(record, events, reports, native_issues):
    measurement, execution = [], []
    for issue in native_issues:
        _append(measurement, issue)
    started = [event for event in events if event.get("event") == "agent_started"]
    stopped = [event for event in events if event.get("event") == "agent_stopped"]
    if len(started) != 1 or started[0].get("mode") != _MODES[record["condition"]] or "agent_version" not in started[0]:
        _append(measurement, "agent_started_missing" if not started else "agent_mode_mismatch")
    if not stopped:
        _append(execution, "agent_stopped_missing")
    elif stopped[-1].get("sched_ext") != "disabled":
        _append(execution, "sched_ext_not_disabled")
    if any(event.get("event") == "agent_error" for event in events):
        _append(execution, "controller_error")
    applied = [event for event in events if event.get("event") == "policy_applied"]
    stops = [event for event in events if event.get("event") == "backend_stopped"]
    for event in applied:
        after = event.get("after")
        if isinstance(after, dict) and after.get("policy_id") == "kernel.default" and not _default_after(after):
            _append(measurement, "policy_event_invalid")
        if isinstance(after, dict) and after.get("policy_id") == "scx.slice" and not _scx_after(after):
            _append(measurement, "policy_readback_mismatch")
    scx = [event for event in applied if isinstance(event.get("after"), dict) and event["after"].get("policy_id") == "scx.slice"]
    coverage = "not_applicable"
    if record["condition"] == "default":
        if applied or stops or reports:
            _append(measurement, "unexpected_policy" if applied or stops else "unexpected_native")
    elif record["condition"] == "fixed-1000":
        if not scx or scx[0]["after"].get("slice_us") != _FIXED_SLICE_US or scx[0]["after"].get("wakeup_preempt") is not False:
            _append(measurement, "fixed_policy_missing" if not scx else "policy_readback_mismatch")
        else:
            _match_backends(scx, reports, stops, measurement, execution)
    else:
        entered = next((index for index, event in enumerate(events) if event in scx), None)
        returned = False
        if entered is not None:
            for event in events[entered + 1:]:
                after = event.get("after") if event.get("event") == "policy_applied" else None
                if isinstance(after, dict) and after.get("policy_id") == "kernel.default":
                    returned = True
                if event.get("event") == "backend_stopped" and event.get("returncode") == 0:
                    returned = True
        coverage = "observed" if entered is not None and returned else "not_triggered"
        if scx:
            if any(event["after"].get("wakeup_preempt") is not False for event in scx):
                _append(measurement, "policy_readback_mismatch")
            _match_backends(scx, reports, stops, measurement, execution)
        elif reports or stops:
            _append(measurement, "unexpected_native")
    return measurement, execution, coverage


def _match_backends(scx_events, reports, stops, measurement, execution):
    by_pid = {report["pid"]: report for report in reports}
    expected = {}
    for event in scx_events:
        after = event["after"]
        if not _scx_after(after):
            _append(measurement, "policy_readback_mismatch")
            continue
        expected[after["pid"]] = after
    if set(by_pid) != set(expected):
        _append(measurement, "native_missing" if set(expected) - set(by_pid) else "unexpected_native")
    for pid, after in expected.items():
        report = by_pid.get(pid)
        if report is None:
            continue
        if not _native_matches(report, after["slice_us"], after["wakeup_preempt"]):
            _append(measurement, "native_config_mismatch")
        if not report["exit_ok"]:
            _append(execution, "native_exit_unconfirmed")
        matched = [event for event in stops if event.get("pid") == pid]
        if len(matched) != 1 or matched[0].get("returncode") != 0:
            _append(execution, "backend_stop_missing" if not matched else "native_returncode")


def _empty_audit(record, issue):
    coverage = "not_triggered" if record["condition"] == "adaptive" else "not_applicable"
    return {"trial_id": record["trial_id"], "measurement_passed": False, "execution_passed": False,
            "issues": [issue], "stats": None, "sustained": None, "client_cpu_limited": False,
            "transition_coverage": coverage, "performance_flags": [], "windows": []}


def _performance_flags(stream, profile):
    flags = []
    if stream["footer"] and (_error_total(stream["footer"]["errors"]) or any(_error_total(window["errors"]) for window in stream["windows"])):
        _append(flags, "request_errors")
    chosen = _regular_windows(stream["windows"])
    offered = stream["ready"]["offered_rps"] if stream["ready"] else None
    if offered:
        for window in chosen:
            fraction = summarize_windows([window], offered)["completion_fraction"]
            if not profile["completion_min"] <= fraction <= profile["completion_max"]:
                _append(flags, "completion_out_of_range")
                break
    return flags


def _phases_match(record, audits, chosen_seqs):
    chosen = [audit for audit in audits if audit["seq"] in chosen_seqs]
    if record["scenario"] == "service":
        return all(audit["workload_phase"] == "service" for audit in chosen)
    if record["scenario"] == "mixed":
        return bool(chosen) and all(audit["workload_phase"] == "mixed" for audit in chosen)
    return True


def _aggregate_background(record, profile, points, point_issues, available, windows):
    if record["scenario"] != "mixed":
        return True
    if not available or points is None or point_issues:
        return False
    if not windows:
        return False
    start_s = windows[0]["start_ns"] / 1_000_000_000
    end_s = windows[-1]["end_ns"] / 1_000_000_000
    _eps, coverage, delay = _interval_stats(points, start_s, end_s)
    return (coverage >= profile["min_background_coverage"] and delay is not None
            and delay < profile["max_background_delivery_delay_seconds"])


def audit_trial(directory: Path, record: TrialRecord, profile: BaselineProfile) -> TrialAudit:
    directory = Path(directory)
    record = _validate_trial_record(record)
    profile = _validate_profile(profile)
    measurement, execution, premise = [], [], []
    try:
        windows = audit_windows(directory, record, profile)
    except ValueError as exc:
        return _empty_audit(record, str(exc))
    for audit in windows:
        for issue in audit["issues"]:
            if issue in _MEASUREMENT_WINDOW_ISSUES:
                _append(measurement, issue)
    try:
        stream = parse_client_stream(_log_path(directory, record["client_log"]), allow_incomplete=False)
    except ValueError as exc:
        _append(measurement, str(exc))
        stream = parse_client_stream(_log_path(directory, record["client_log"]), allow_incomplete=True)
    for issue in stream["issues"]:
        _append(measurement, issue)
    if stream["footer"] and stream["footer"]["histogram_overflows"]:
        _append(measurement, "histogram_overflow")
    if stream["ready"] and stream["ready"]["offered_rps"] != record["rate"]:
        _append(measurement, "offered_rps_mismatch")
    if stream["ready"] and (stream["ready"]["window_ms"] != profile["window_ms"]
                            or stream["ready"]["connections"] != profile["connections"]):
        _append(measurement, "client_configuration_mismatch")
    stats = None
    if stream["windows"] and stream["ready"]:
        try:
            stats = summarize_windows(stream["windows"], stream["ready"]["offered_rps"])
        except ValueError:
            _append(measurement, "window_stats_invalid")
    events, event_issue = _load_agent_events(directory, record)
    if event_issue:
        _append(measurement, event_issue)
    reports, native_issues = _native_reports(_trial_directory(directory, record))
    control_measurement, control_execution, coverage = _confirm_control(record, events, reports, native_issues)
    for issue in control_measurement:
        _append(measurement, issue)
    for issue in control_execution:
        _append(execution, issue)
    driver_path = _log_path(directory, record["driver_events_log"])
    if not driver_path.is_file():
        _append(execution, "driver_events_missing")
        driver_events = []
    else:
        try:
            driver_events = _read_jsonl(driver_path)
        except ValueError:
            _append(execution, "driver_events_malformed")
            driver_events = []
    if driver_path.is_file() and "driver_events_malformed" not in execution:
        for issue in _check_processes(record, driver_events):
            _append(execution, issue)
    if record["error"] is not None:
        _append(execution, "trial_error")
    if record["cleanup_errors"]:
        _append(execution, "cleanup_errors")
    points, point_issues, available = _background_inputs(directory, record)
    for issue in point_issues:
        if issue == "background_intervals_invalid":
            _append(measurement, issue)
    performance = _performance_flags(stream, profile) if stream["complete"] else []
    survey = _is_survey_trial(record, profile)
    evaluated = _evaluation_windows(stream["windows"], profile, survey) if stream["windows"] else []
    sample_rows, sample_issues = _sample_rows(_log_path(directory, record["samples_log"]))
    client_cpu_limited = False
    resource_ok = False
    for issue in sample_issues:
        if issue in ("samples_malformed", "samples_invalid"):
            _append(measurement, issue)
        else:
            _append(premise, issue)
    if sample_rows is not None and "samples_invalid" not in measurement and "samples_malformed" not in measurement:
        resource_issues, client_cpu_limited, resource_ok = _resource_premise(
            sample_rows, evaluated, profile["client_cpu_limit_percent"])
        for issue in resource_issues:
            _append(premise, issue)
    elif sample_rows is None and "samples_missing" not in premise:
        resource_ok = False
    if client_cpu_limited:
        _append(performance, "client_cpu_limited")
    sustained = None
    measurement_passed = not measurement
    execution_passed = not execution
    if survey and measurement_passed and execution_passed:
        enough = len([window for window in stream["windows"] if not window["final"]]) >= profile["survey_required_windows"]
        if not enough:
            _append(premise, "survey_windows_insufficient")
        phases_ok = _phases_match(record, windows, {window["seq"] for window in evaluated})
        if not phases_ok:
            _append(premise, "survey_phase_mismatch")
        background_ok = _aggregate_background(record, profile, points, point_issues, available, evaluated)
        if record["scenario"] == "mixed" and not background_ok:
            _append(premise, "background_coverage_insufficient")
        comparable = enough and resource_ok and phases_ok and (record["scenario"] != "mixed" or background_ok)
        if comparable and ("request_errors" in performance or "completion_out_of_range" in performance or client_cpu_limited):
            sustained = False
        elif comparable:
            sustained = True
    return {"trial_id": record["trial_id"], "measurement_passed": measurement_passed,
            "execution_passed": execution_passed, "issues": measurement + execution + premise, "stats": stats,
            "sustained": sustained, "client_cpu_limited": client_cpu_limited, "transition_coverage": coverage,
            "performance_flags": performance, "windows": windows}


def _walk_scenario(scenario, rows, cap):
    reference = None
    reference_id = None
    witness_id = None
    bracketed = False
    limitations = []
    if not rows:
        return None, False, [], [f"{scenario}: no survey trial"]
    stopped = False
    for record, audit in rows:
        if not audit["measurement_passed"] or not audit["execution_passed"]:
            limitations.append(f"{scenario}: measurement or execution failed")
            stopped = True
            break
        if audit["sustained"] is None:
            limitations.append(f"{scenario}: insufficient premise")
            stopped = True
            break
        if audit["client_cpu_limited"]:
            limitations.append(f"{scenario}: client saturated")
            stopped = True
            break
        if audit["sustained"] is False:
            if reference is not None:
                bracketed = True
                witness_id = record["trial_id"]
                limitations.append(f"{scenario}: unsustainable at comparable rate")
            else:
                limitations.append(f"{scenario}: unsustainable without a lower bound")
            stopped = True
            break
        reference = record["rate"]
        reference_id = record["trial_id"]
    if reference is not None and not bracketed:
        if not stopped and reference == cap:
            limitations.append(f"{scenario}: sustained at survey cap; conservative rate, not a measured maximum")
        elif not stopped:
            limitations.append(f"{scenario}: no unsustainable comparison; conservative rate, not a measured maximum")
        else:
            limitations.append(f"{scenario}: conservative lower bound, not a measured maximum")
    if reference is None and not any(item.startswith(f"{scenario}:") for item in limitations):
        limitations.append(f"{scenario}: no sustainable rate")
    sources = [item for item in (reference_id, witness_id) if item]
    return reference, bracketed, sources, limitations


def _floor_hundred(value):
    return value // 100 * 100


def select_rates(trials: Sequence[tuple[TrialRecord, TrialAudit]], profile: BaselineProfile) -> RateSelection:
    profile = _validate_profile(profile)
    if not isinstance(trials, Sequence) or isinstance(trials, (str, bytes)):
        raise ValueError("trials must be a sequence")
    parsed = []
    for item in trials:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("trial entry must be a record and audit pair")
        record = _validate_trial_record(item[0])
        audit = _validate_trial_audit(item[1])
        if audit["trial_id"] != record["trial_id"]:
            raise ValueError("trial audit identity mismatch")
        parsed.append((record, audit))
    allowed = set(profile["survey_rates"])
    cap = max(profile["survey_rates"])
    limitations = []
    references = {}
    bracketed = {}
    sources = []
    for scenario in ("service", "mixed"):
        rows = [(record, audit) for record, audit in parsed
                if record["scenario"] == scenario and record["condition"] == "default" and record["rate"] in allowed]
        rows.sort(key=lambda pair: pair[0]["rate"])
        reference, scenario_bracket, scenario_sources, scenario_limits = _walk_scenario(scenario, rows, cap)
        references[scenario] = reference
        bracketed[scenario] = scenario_bracket
        sources.extend(scenario_sources)
        limitations.extend(scenario_limits)
    rates = {}
    status = "insufficient"
    if references["service"] is not None and references["mixed"] is not None:
        common = min(references["service"], references["mixed"])
        low = _floor_hundred(common * 40 // 100)
        mid = _floor_hundred(common * 70 // 100)
        high = _floor_hundred(common * 90 // 100)
        if low > 0 and low < mid < high:
            rates = {"low": low, "mid": mid, "high": high}
            status = "selected"
        else:
            limitations.append("selected rates are not positive and strictly increasing")
    return {"schema_version": 1, "status": status, "service_reference_rps": references["service"],
            "mixed_reference_rps": references["mixed"], "capacity_bracketed": bracketed, "rates": rates,
            "source_trial_ids": sources, "limitations": limitations}
