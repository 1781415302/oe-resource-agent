"""Host retry, download, collector, and audit contracts for the stage-1 driver.

SSH, SCP, the collector, and the study directory are isolated. No guest load is started.
"""
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

_PREFIX = os.environ.get("PYTHONPYCACHEPREFIX") or str(Path(tempfile.gettempdir()) / "oe-stage1-pyc-s5")
os.environ.setdefault("PYTHONPYCACHEPREFIX", _PREFIX)
sys.pycache_prefix = os.environ["PYTHONPYCACHEPREFIX"]

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT.parent / "research"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RESEARCH))

import analyze_baseline  # noqa: E402
import run_baseline as host  # noqa: E402

NS = 1_000_000_000
SOURCE_REL = "oe-resource-agent/marker.py"
SOURCE_BYTES = b"marker\n"
LOW_HOST_CPU_LIMITS = {"preflight_cpu_limit": 20, "during_cpu_limit": 30}


def errors():
    return {"connect": 0, "read": 0, "write": 0, "status": 0, "timeout": 0}


def ready(start_ns, offered_rps):
    return {"schema_version": 1, "kind": "ready", "start_ns": start_ns, "window_ms": 5000,
            "offered_rps": offered_rps, "connections": 32, "client_threads": 1, "pipeline": 1,
            "histogram": "wrk2_expected_latency_hdr_3sig"}


def window(seq, start_ns, requests, bins):
    return {"schema_version": 1, "kind": "window", "seq": seq, "start_ns": start_ns,
            "end_ns": start_ns + 5 * NS, "final": False, "requests": requests, "bytes": 0,
            "errors": errors(), "terminal_exclusions": 0, "histogram_overflows": 0, "bins_us": bins}


def footer(windows):
    bins = {}
    for item in windows:
        for value, count in item["bins_us"]:
            bins[value] = bins.get(value, 0) + count
    return {"schema_version": 1, "kind": "footer", "start_ns": windows[0]["start_ns"],
            "end_ns": windows[-1]["end_ns"], "windows": len(windows),
            "requests": sum(item["requests"] for item in windows), "bytes": 0, "errors": errors(),
            "terminal_exclusions": 0, "histogram_overflows": 0,
            "bins_us": [[value, bins[value]] for value in sorted(bins)], "export_duration_ns": 0}


def stream_text(records):
    return "".join("OE_WRK2_STREAM " + json.dumps(record) + "\n" for record in records)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def snapshot(source_hash):
    native = "ab" * 32
    wrk = "cd" * 32
    return {
        "schema_version": 1,
        "boot_id": "boot-1",
        "virtualization": "microsoft",
        "environment": {"memory_kib": 1},
        "native_build": {"binary_sha256": native},
        "wrk2_build": {"binary_sha256": wrk, "measurement_protocol": "oe_wrk2_windows_v1", "window_ms": 5000},
        "source_sha256": {SOURCE_REL: source_hash},
        "checks": {"linux": True, "root": True, "cpus": True, "sched_ext": True, "wrk2": True, "port": True},
        "limitations": ["optional psi unavailable"],
    }


def client_records(start, rate, requests):
    records = [ready(start * NS, rate)]
    windows = []
    for index in range(3):
        begin = (start + index * 5) * NS
        windows.append(window(index + 1, begin, requests, [[1000, requests]]))
    records.extend(windows)
    records.append(footer(windows))
    return records


def side_files(directory, start, end):
    write_jsonl(directory / "events.jsonl", [
        {"monotonic": start - 1, "event": "agent_started", "agent_version": "0.1.3", "mode": "default"},
        {"monotonic": end + 1, "event": "agent_stopped", "sched_ext": "disabled"},
    ])
    write_jsonl(directory / "driver-events.jsonl", [
        {"monotonic": start - 2, "event": "process_started", "pid": 11, "start_ticks": 200,
         "boot_id": "boot-1", "role": "server", "command": ["nginx"]},
        {"monotonic": start - 1.5, "event": "process_started", "pid": 10, "start_ticks": 100,
         "boot_id": "boot-1", "role": "client", "command": ["wrk2"]},
        {"monotonic": end + 0.5, "event": "process_stopped", "pid": 10, "role": "client", "returncode": 0},
        {"monotonic": end + 1, "event": "process_stopped", "pid": 11, "role": "server", "returncode": -15},
    ])
    rows = [{"monotonic": start - 1, "sched_ext": "disabled", "processes": {
        "client": {"pid": 10, "start_ticks": 100, "cpu_percent": None}}}]
    stamp = start
    while stamp < end:
        rows.append({"monotonic": stamp + 1, "sample_start_monotonic": stamp, "sample_interval_seconds": 1,
                     "sched_ext": "disabled", "processes": {
                         "client": {"pid": 10, "start_ticks": 100, "cpu_percent": 10}}})
        stamp += 1
    write_jsonl(directory / "samples.jsonl", rows)


def trial_record(run_key, scenario, rate, relative, *, workload=None, background=False):
    return {
        "trial_id": f"{run_key}-{scenario}",
        "condition": "default",
        "scenario": scenario,
        "rate": rate,
        "client_log": relative + "/client.log",
        "samples_log": relative + "/samples.jsonl",
        "background_log": relative + "/background.log" if background else None,
        "background_intervals_log": relative + "/background-intervals.jsonl" if background else None,
        "agent_events_log": relative + "/events.jsonl",
        "driver_events_log": relative + "/driver-events.jsonl",
        "workload_events": workload or [],
        "process_returncodes": {"client:10:100": 0, "server:11:200": -15},
        "cleanup_errors": [],
        "error": None,
    }


def build_archive(request_bytes, shot, *, poor):
    request = json.loads(request_bytes.decode("utf-8"))
    run_key = request["run_key"]
    requests = 1000 if poor else 10000
    rate = 2000
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        base = root / "results" / f"baseline-{run_key}"
        for scenario, background in (("service", False), ("mixed", True)):
            trial = base / "trials" / scenario
            trial.mkdir(parents=True)
            (trial / "client.log").write_text(stream_text(client_records(1000, rate, requests)),
                                              encoding="utf-8", newline="\n")
            side_files(trial, 1000, 1015)
            if background:
                (trial / "background.log").write_text("sysbench\n", encoding="utf-8", newline="\n")
                write_jsonl(trial / "background-intervals.jsonl", [
                    {"elapsed_seconds": index, "events_per_sec": 100, "received_monotonic": 1000 + index}
                    for index in range(1, 16)
                ])
        source = base / "source-files" / SOURCE_REL
        source.parent.mkdir(parents=True)
        source.write_bytes(SOURCE_BYTES)
        write_json(base / "request.json", request)
        write_json(base / "owner.json", {"run_key": run_key, "boot_id": shot["boot_id"], "pid": 4242, "start_ticks": 100})
        write_json(base / "python-cache-audit.json", {"all_cached_code_matches": True, "all_checked_hash": True})
        workload = [{"kind": "background_start", "started_ns": 990 * NS, "finished_ns": 1000 * NS,
                     "pid": 12, "start_ticks": 300}]
        write_json(base / "summary.json", {
            "schema_version": 1,
            "run_key": run_key,
            "operation": request["operation"],
            "status": "completed",
            "snapshot": shot,
            "trials": [
                trial_record(run_key, "service", rate, "trials/service"),
                trial_record(run_key, "mixed", rate, "trials/mixed", workload=workload, background=True),
            ],
            "final_sched_ext": "disabled",
            "cleanup_errors": [],
            "error": None,
        })
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    bundle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        return buffer.getvalue()


def positionals(argv):
    skip = {"-i", "-o"}
    found = []
    index = 1
    while index < len(argv):
        item = argv[index]
        if item in skip:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        found.append(item)
        index += 1
    return found


def tree_hash(root):
    digest = hashlib.sha256()
    root = Path(root)
    if root.is_file():
        digest.update(root.read_bytes())
        return digest.hexdigest()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class FakeProc:
    def __init__(self, world, kind):
        self.world = world
        self.kind = kind
        self.returncode = None
        self.stdout = None
        self.remaining = world.run_seconds
        self.done = False

    def poll(self):
        if self.kind == "collector":
            if self.world.collector_rc is not None:
                self.returncode = self.world.collector_rc
                return self.returncode
            if self.world.stop_path is not None and self.world.stop_path.exists():
                self.returncode = 0
                self.world.collector_rc = 0
                return 0
            if self.world.collector_deadline is not None and self.world.now >= self.world.collector_deadline:
                self.returncode = 1
                self.world.collector_rc = 1
                return 1
            return None
        return self.returncode

    def wait(self, timeout=None):
        code = self.poll()
        if code is None:
            raise subprocess.TimeoutExpired(self.kind, timeout)
        return code

    def terminate(self):
        if self.returncode is None:
            self.returncode = 1
            if self.kind == "collector":
                self.world.collector_rc = 1

    def kill(self):
        self.terminate()

    def finish(self):
        if self.done:
            return
        self.done = True
        text = self.world.complete_load()
        if self.stdout is not None:
            self.stdout.write(text.encode("utf-8"))
            self.stdout.flush()
        self.returncode = 0
        self.world.cpu = 5.0
        self.world.active_ssh = None


class FakeWorld:
    def __init__(self, *, run_cpus=None, poor=False, fail_download=False, truncate=False,
                 die_on_sleep=None, checks_ok=True, run_seconds=16, scp_seconds=0,
                 preflight_ssh_seconds=0):
        self.now = 10000.0
        self.origin = self.now
        self.cpu = 5.0
        self.run_cpus = list(run_cpus or [5.0])
        self.poor = poor
        self.fail_download = fail_download
        self.truncate = truncate
        self.die_on_sleep = die_on_sleep
        self.checks_ok = checks_ok
        self.run_seconds = run_seconds
        self.scp_seconds = scp_seconds
        self.preflight_ssh_seconds = preflight_ssh_seconds
        self.samples_path = None
        self.stop_path = None
        self.collector_rc = None
        self.collector_deadline = None
        self.last_sample_at = None
        self.sleeps = 0
        self.remote_files = {}
        self.popens = []
        self.runs = []
        self.active_ssh = None
        self.source_hash = hashlib.sha256(SOURCE_BYTES).hexdigest()
        self.shot = snapshot(self.source_hash)

    @contextmanager
    def applied(self):
        self.real_run = host.subprocess.run
        self.real_popen = host.subprocess.Popen
        with patch.object(host.time, "perf_counter", self.perf_counter), \
                patch.object(host.time, "sleep", self.sleep), \
                patch.object(host.subprocess, "Popen", self.popen), \
                patch.object(host.subprocess, "run", self.run):
            yield self

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)
        self.maybe_sample()
        self.sleeps += 1
        if self.active_ssh is not None and not self.active_ssh.done:
            self.active_ssh.remaining -= float(seconds)
            if self.active_ssh.remaining <= 0:
                self.active_ssh.finish()
        if self.die_on_sleep is not None and self.sleeps >= self.die_on_sleep:
            self.collector_rc = 1

    def advance(self, seconds):
        remaining = float(seconds)
        while remaining > 0:
            step = min(5.0, remaining)
            self.now += step
            self.maybe_sample()
            remaining -= step

    def maybe_sample(self):
        if self.samples_path is None:
            return
        if self.last_sample_at is not None and self.now - self.last_sample_at < 5:
            return
        self.write_sample()

    def write_sample(self):
        row = {
            "utc": "2026-10-22T00:00:00+00:00",
            "elapsed_seconds": self.now - self.origin,
            "collector_pid": 4242,
            "host_cpu_percent": float(self.cpu),
            "host_monotonic_seconds": self.now,
            "vmware": [],
            "hyperv": None,
            "error": None,
            "hyperv_error": None,
        }
        with self.samples_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row) + "\n")
            handle.flush()
        self.last_sample_at = self.now

    def popen(self, argv, **kwargs):
        self.popens.append(list(argv))
        if argv[0] == "pwsh":
            self.samples_path = Path(argv[argv.index("-OutputPath") + 1])
            self.stop_path = Path(argv[argv.index("-StopPath") + 1])
            self.collector_deadline = self.now + int(argv[argv.index("-DurationSeconds") + 1])
            self.samples_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_sample()
            return FakeProc(self, "collector")
        if argv[0] == "ssh":
            self.cpu = float(self.run_cpus.pop(0) if self.run_cpus else 5.0)
            proc = FakeProc(self, "ssh")
            proc.stdout = kwargs.get("stdout")
            self.active_ssh = proc
            return proc
        return self.real_popen(argv, **kwargs)

    def run(self, argv, **kwargs):
        self.runs.append(list(argv))
        if argv[0] == "powercfg":
            return subprocess.CompletedProcess(argv, 0, b"Power Scheme GUID: test\n", b"")
        if argv[0] == "scp":
            return self.scp(argv)
        if argv[0] == "ssh":
            return self.short_ssh(argv)
        return self.real_run(argv, **kwargs)

    def scp(self, argv):
        self.advance(self.scp_seconds)
        source, dest = positionals(argv)
        if dest.startswith("root@"):
            remote = dest.split(":", 1)[1]
            self.remote_files[remote] = Path(source).read_bytes()
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        remote = source.split(":", 1)[1]
        if self.fail_download or remote not in self.remote_files:
            return subprocess.CompletedProcess(argv, 1, b"", b"download failed")
        data = self.remote_files[remote]
        if self.truncate:
            data = b"not-a-gzip"
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(data)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def short_ssh(self, argv):
        command = positionals(argv)[-1]
        remote = shlex.split(command)
        if "--stop" in remote:
            payload = json.dumps({"stopped": True, "reason": "already_exited"}) + "\n"
            return subprocess.CompletedProcess(argv, 0, payload.encode("utf-8"), b"")
        run_key = Path(remote[-1]).name.removeprefix("oe-baseline-").removesuffix(".json")
        if "preflight" in run_key:
            self.advance(self.preflight_ssh_seconds)
        shot = json.loads(json.dumps(self.shot))
        if not self.checks_ok:
            shot["checks"] = dict(shot["checks"])
            shot["checks"]["linux"] = False
        body = "probing\n" + json.dumps({"note": "progress"}) + "\n" + json.dumps(shot) + "\n"
        if "preflight" not in run_key:
            raise AssertionError(command)
        return subprocess.CompletedProcess(argv, 0, body.encode("utf-8"), b"")

    def complete_load(self):
        uploads = [value for key, value in self.remote_files.items() if key.endswith(".json")]
        request_bytes = uploads[-1]
        payload = build_archive(request_bytes, self.shot, poor=self.poor)
        request = json.loads(request_bytes.decode("utf-8"))
        remote = f"/tmp/oe-baseline-{request['run_key']}.tar.gz"
        self.remote_files[remote] = payload
        digest = hashlib.sha256(payload).hexdigest()
        result = {"run_key": request["run_key"], "status": "completed", "archive": remote, "archive_sha256": digest}
        return json.dumps({"progress": True, "status": "running"}) + "\n" + json.dumps(result) + "\n"

    def load_commands(self):
        return [argv for argv in self.popens if argv and argv[0] == "ssh"]

    def ssh_runs(self):
        return [argv for argv in self.runs if argv and argv[0] == "ssh"]


class HostContract(unittest.TestCase):
    def test_help_does_not_start_work_or_create_output(self):
        out = Path(tempfile.gettempdir()) / "oe-stage1-help-should-not-exist"
        if out.exists():
            raise AssertionError(out)
        with patch.object(host.subprocess, "Popen", side_effect=AssertionError("popen")), \
                patch.object(host.subprocess, "run", side_effect=AssertionError("run")):
            with self.assertRaises(SystemExit) as caught:
                host.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertFalse(out.exists())

    def test_usage_errors_do_not_create_output(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        pilot = out / "pilot"
        with patch.object(host.subprocess, "Popen", side_effect=AssertionError("popen")), \
                patch.object(host.subprocess, "run", side_effect=AssertionError("run")):
            with self.assertRaises(SystemExit) as missing:
                host.main([])
            with self.assertRaises(SystemExit) as pilot_missing:
                host.main(["--host", "guest.example", "--guest-root", "/root/oe-resource-agent",
                           "--output-root", str(pilot), "--mode", "pilot"])
        self.assertEqual(missing.exception.code, 2)
        self.assertEqual(pilot_missing.exception.code, 2)
        self.assertFalse(pilot.exists())

    def test_source_does_not_embed_old_ssh_defaults(self):
        text = (RESEARCH / "run_baseline.py").read_text(encoding="utf-8")
        self.assertNotIn("openeuler_ed25519", text)
        self.assertNotIn("192.168.200.130", text)
        self.assertNotIn("ExecutionPolicy", text)
        self.assertNotIn("stop_active", text)
        self.assertNotIn("run_controlled_cycle", text)
        self.assertNotIn("pkill", text)
        self.assertIn("BatchMode=yes", text)
        self.assertIn("ConnectTimeout=8", text)
        self.assertIn("StrictHostKeyChecking=yes", text)
        self.assertIn("pwsh", text)

    def test_pretty_snapshot_and_compact_result_both_parse(self):
        shot = snapshot("ab" * 32)
        pretty = "probing\n" + json.dumps(shot, indent=2) + "\nAuthorized users only.\n"
        accepted = [item for item in (host._accept_snapshot(value) for value in host._json_objects(pretty))
                    if item is not None]
        self.assertEqual([item["virtualization"] for item in accepted], ["microsoft"])
        result = {"run_key": "survey-1", "status": "completed",
                  "archive": "/tmp/oe-baseline-survey-1.tar.gz", "archive_sha256": "cd" * 32}
        compact = json.dumps({"progress": True, "status": "running"}) + "\n" + json.dumps(result) + "\n"
        self.assertEqual(host._machine_result(compact)["run_key"], "survey-1")

    def test_invalid_host_and_guest_root_raise_before_ssh(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        with patch.object(host.subprocess, "Popen", side_effect=AssertionError("popen")), \
                patch.object(host.subprocess, "run", side_effect=AssertionError("run")):
            with self.assertRaises(ValueError):
                host.run_study(host="bad host", key=None, guest_root="/root/oe-resource-agent",
                               output_root=out, mode="survey", calibration_dir=None)
            with self.assertRaises(ValueError):
                host.run_study(host="guest.example", key=None, guest_root="/root/../etc",
                               output_root=out, mode="survey", calibration_dir=None)
        self.assertEqual(list(out.iterdir()), [])


class HostStudy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "out"
        self.output.mkdir()

    def run_main(self, world, *, key=None, collector_mutation=None, host_cpu_limits=None):
        argv = ["--host", "guest.example", "--guest-root", "/root/oe-resource-agent",
                "--output-root", str(self.output), "--mode", "survey"]
        if key is not None:
            argv.extend(["--key", str(key)])
        with ExitStack() as stack:
            if host_cpu_limits is not None:
                profile = json.loads(host._PROFILE_PATH.read_text(encoding="utf-8"))
                profile["host_quality"].update(host_cpu_limits)
                profile_path = Path(self.temp.name) / "test-baseline-profile.json"
                profile_path.write_text(json.dumps(profile), encoding="utf-8")
                profile_key = "oe-resource-agent/experiments/baseline-profile.json"
                sources = tuple((key, profile_path if key == profile_key else source)
                                for key, source in host._SOURCES)
                stack.enter_context(patch.object(host, "_PROFILE_PATH", profile_path))
                stack.enter_context(patch.object(host, "_SOURCES", sources))
            if collector_mutation is not None:
                original_open = host._Driver._open_collector

                def mutate_before_open(driver):
                    collector_mutation(driver)
                    return original_open(driver)

                stack.enter_context(patch.object(host._Driver, "_open_collector", mutate_before_open))
            stack.enter_context(world.applied())
            try:
                code = host.main(argv)
            except RuntimeError:
                if collector_mutation is None:
                    raise
                code = 1
        directories = [path for path in self.output.iterdir() if path.is_dir()]
        self.assertEqual(len(directories), 1, directories)
        return code, directories[0]

    def manifest(self, directory):
        return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    def test_clean_pass_completes_and_audit_recomputes(self):
        world = FakeWorld(run_cpus=[5.0])
        code, directory = self.run_main(world)
        manifest = self.manifest(directory)
        audit = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0, audit["issues"])
        self.assertEqual(manifest["status"], "completed", manifest["error"])
        self.assertEqual(manifest["collector_returncode"], 0)
        self.assertEqual(len(manifest["attempts"]), 1)
        self.assertEqual(manifest["attempts"][0]["status"], "completed")
        self.assertEqual(audit["status"], "passed", audit["issues"])
        self.assertEqual(audit["selected_run_key"], manifest["attempts"][0]["run_key"])
        self.assertTrue((directory / "calibration.json").is_file())
        self.assertEqual(len(world.load_commands()), 1)
        self.assert_ssh_contract(world.load_commands()[0], key=None)
        self.assertNotIn("schema_version", positionals(world.load_commands()[0])[-1])
        collector = next(argv for argv in world.popens if argv[0] == "pwsh")
        self.assertNotIn("-ExecutionPolicy", collector)
        self.assertNotIn("powershell.exe", collector)
        self.assertIn("-NoProfile", collector)
        collector_script = Path(collector[collector.index("-File") + 1])
        self.assertEqual(collector_script, directory / "source-files" / "research" / "collect_host_cpu.ps1")
        self.assertTrue(collector_script.is_file())
        self.assertEqual(host._sha256(collector_script),
                         manifest["host_source_sha256"]["research/collect_host_cpu.ps1"])
        self.assert_recompute(directory, audit, manifest["attempts"])

    def test_default_profile_accepts_host_cpu_at_100_percent(self):
        world = FakeWorld(run_cpus=[100.0])
        code, directory = self.run_main(world)
        manifest = self.manifest(directory)
        audit = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["profile"]["host_quality"]["preflight_cpu_limit"], 100)
        self.assertEqual(manifest["profile"]["host_quality"]["during_cpu_limit"], 100)
        self.assertEqual(audit["status"], "passed", audit["issues"])
        self.assertTrue(any(row["host_cpu_percent"] == 100.0 for row in
                            (json.loads(line) for line in (directory / "host-cpu.jsonl").read_text().splitlines())))
        self.assertEqual(code, 0)

    def test_first_quality_pass_is_selected_and_rejected_attempt_is_kept(self):
        world = FakeWorld(run_cpus=[90.0, 5.0, 5.0])
        code, directory = self.run_main(world, host_cpu_limits=LOW_HOST_CPU_LIMITS)
        manifest = self.manifest(directory)
        audit = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(len(world.load_commands()), 2)
        self.assertEqual(len(manifest["attempts"]), 2)
        self.assertEqual(manifest["attempts"][0]["status"], "host_rejected")
        self.assertEqual(manifest["attempts"][1]["status"], "completed")
        self.assertEqual(audit["selected_run_key"], manifest["attempts"][1]["run_key"], audit["issues"])
        self.assertTrue((directory / "attempts" / f"{manifest['attempts'][0]['run_key']}.tar.gz").is_file())
        self.assertTrue((directory / "attempts" / f"{manifest['attempts'][1]['run_key']}.tar.gz").is_file())
        self.assertEqual(audit["status"], "passed", audit["issues"])
        self.assertEqual(audit["issues"], [])
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(code, 0)
        self.assertTrue(any(item.startswith("retryable_host_quality_rejected:")
                            for item in audit["limitations"]))
        report = (directory / "analysis.html").read_text(encoding="utf-8")
        self.assertIn("host_rejected", report)
        self.assertIn("retryable_host_quality_rejected", report)
        calibration = json.loads((directory / "calibration.json").read_text(encoding="utf-8"))
        self.assertEqual(calibration["status"], "selected")
        self.assertTrue(all(item.startswith(manifest["attempts"][1]["run_key"])
                            for item in calibration["source_trial_ids"]))
        self.assert_recompute(directory, audit, manifest["attempts"])

    def test_audit_rejects_an_attempt_after_a_pass(self):
        world = FakeWorld()
        code, directory = self.run_main(world)
        self.assertEqual(code, 0)
        manifest = self.manifest(directory)
        extra = json.loads(json.dumps(manifest["attempts"][-1]))
        extra["index"] = 3
        manifest["attempts"].append(extra)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        audit = analyze_baseline.audit_study(directory)
        self.assertEqual(audit["status"], "failed")
        self.assertIn("extra_attempt_after_pass", audit["issues"])

    def test_collector_budget_covers_slow_retry_and_transfers(self):
        world = FakeWorld(run_cpus=[90.0, 5.0, 5.0], run_seconds=1288,
                          scp_seconds=119, preflight_ssh_seconds=119)
        code, directory = self.run_main(world, host_cpu_limits=LOW_HOST_CPU_LIMITS)
        manifest = self.manifest(directory)
        audit = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
        collector = next(argv for argv in world.popens if argv[0] == "pwsh")
        actual_budget = int(collector[collector.index("-DurationSeconds") + 1])
        quality = manifest["profile"]["host_quality"]
        q = quality["maximum_preflight_wait_seconds"]
        s = quality["sample_seconds"]
        a = quality["maximum_attempts"]
        g = host._host_wait("survey")
        expected_budget = 2 * q + 120 + 120 + a * (q + 120 + g + 120 + s + 1) + 180 + 3 * s
        old_budget = q + g + 45 + 3 * s
        self.assertEqual(actual_budget, expected_budget)
        self.assertGreater(world.now - world.origin, old_budget)
        self.assertEqual(manifest["attempts"][0]["status"], "host_rejected")
        self.assertEqual(manifest["attempts"][1]["status"], "completed")
        self.assertEqual(manifest["collector_returncode"], 0)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(audit["status"], "passed", audit["issues"])
        self.assertEqual(code, 0)

    def test_host_quality_retries_at_most_twice(self):
        world = FakeWorld(run_cpus=[90.0, 90.0, 5.0])
        code, directory = self.run_main(world, host_cpu_limits=LOW_HOST_CPU_LIMITS)
        manifest = self.manifest(directory)
        audit = analyze_baseline.audit_study(directory)
        self.assertEqual(code, 2, manifest)
        self.assertEqual(manifest["status"], "quality_incomplete")
        self.assertEqual(len(manifest["attempts"]), 2)
        self.assertEqual([item["status"] for item in manifest["attempts"]], ["host_rejected", "host_rejected"])
        self.assertEqual(len(world.load_commands()), 2)
        self.assertIsNone(audit["selected_run_key"])
        self.assertEqual(audit["status"], "quality_incomplete", audit["issues"])

    def test_poor_performance_does_not_retry(self):
        world = FakeWorld(poor=True)
        code, directory = self.run_main(world)
        manifest = self.manifest(directory)
        audit = analyze_baseline.audit_study(directory)
        self.assertEqual(len(world.load_commands()), 1)
        self.assertEqual(len(manifest["attempts"]), 1)
        self.assertEqual(manifest["attempts"][0]["status"], "completed")
        self.assertEqual(manifest["status"], "quality_incomplete", manifest["error"])
        self.assertEqual(code, 2)
        self.assertEqual(audit["status"], "quality_incomplete", audit["issues"])
        self.assertEqual(audit["selected_run_key"], manifest["attempts"][0]["run_key"])
        self.assertTrue(any(trial["performance_flags"] for trial in audit["trials"]))
        self.assertFalse((directory / "calibration.json").exists())

    def test_download_failure_is_not_retried(self):
        world = FakeWorld(fail_download=True)
        code, directory = self.run_main(world)
        manifest = self.manifest(directory)
        self.assertEqual(len(world.load_commands()), 1)
        self.assertEqual(len(manifest["attempts"]), 1)
        attempt = manifest["attempts"][0]
        self.assertEqual(attempt["status"], "execution_failed")
        self.assertNotEqual(attempt["fetch_returncode"], 0)
        self.assertEqual(attempt["guest_returncode"], 0)
        self.assertEqual(manifest["status"], "failed", manifest["error"])
        self.assertEqual(code, 1)
        self.assertIn("download failed", attempt["error"])

    def test_missing_frozen_collector_copy_does_not_start_collector(self):
        world = FakeWorld()

        def remove_copy(driver):
            (driver.directory / "source-files" / "research" / "collect_host_cpu.ps1").unlink()

        code, directory = self.run_main(world, collector_mutation=remove_copy)
        manifest = self.manifest(directory)
        self.assertEqual(code, 1)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("frozen collector source is missing", manifest["error"])
        self.assertEqual(world.popens, [])
        self.assertEqual(world.load_commands(), [])

    def test_mismatched_frozen_collector_copy_does_not_start_collector(self):
        world = FakeWorld()

        def corrupt_copy(driver):
            path = driver.directory / "source-files" / "research" / "collect_host_cpu.ps1"
            path.write_bytes(path.read_bytes() + b"tampered")

        code, directory = self.run_main(world, collector_mutation=corrupt_copy)
        manifest = self.manifest(directory)
        self.assertEqual(code, 1)
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("frozen collector digest mismatch", manifest["error"])
        self.assertEqual(world.popens, [])
        self.assertEqual(world.load_commands(), [])
    def test_collector_failure_does_not_leave_completed_or_start_load(self):
        world = FakeWorld(die_on_sleep=3)
        code, directory = self.run_main(world)
        manifest = self.manifest(directory)
        audit = analyze_baseline.audit_study(directory)
        self.assertEqual(world.load_commands(), [])
        self.assertEqual(world.ssh_runs(), [])
        self.assertNotEqual(manifest["collector_returncode"], 0)
        self.assertNotEqual(manifest["status"], "completed")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(code, 1)
        self.assertTrue(any("collector_exit" in item for item in audit["issues"]))

    def test_truncated_archive_is_rejected_without_retry(self):
        world = FakeWorld(truncate=True)
        code, directory = self.run_main(world)
        manifest = self.manifest(directory)
        audit = analyze_baseline.audit_study(directory)
        self.assertEqual(len(world.load_commands()), 1)
        self.assertEqual(len(manifest["attempts"]), 1)
        attempt = manifest["attempts"][0]
        self.assertEqual(attempt["status"], "execution_failed")
        self.assertIn("archive_truncated", attempt["error"])
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(code, 1)
        self.assertTrue(any("archive_truncated" in item for item in audit["issues"]), audit["issues"])
        self.assertIsNone(audit["selected_run_key"])

    def test_explicit_key_is_passed_and_failed_checks_do_not_start_load(self):
        key = Path(self.temp.name) / "id_ed25519"
        world = FakeWorld(checks_ok=False)
        code, directory = self.run_main(world, key=key)
        manifest = self.manifest(directory)
        self.assertEqual(world.load_commands(), [])
        self.assertEqual(len(world.ssh_runs()), 1)
        self.assert_ssh_contract(world.ssh_runs()[0], key=key)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(code, 1)
        self.assertIn("checks failed", manifest["error"])
        self.assertFalse(manifest["guest_preflight"]["checks"]["linux"])

    def assert_recompute(self, directory, audit, attempts):
        before = {"raw": tree_hash(directory / "raw"), "samples": tree_hash(directory / "host-cpu.jsonl"),
                  "manifest": tree_hash(directory / "manifest.json")}
        archives = []
        for attempt in attempts:
            path = directory / "attempts" / f"{attempt['run_key']}.tar.gz"
            archives.append(tree_hash(path))
        recomputed = analyze_baseline.audit_study(directory)
        self.assertEqual(recomputed, audit)
        self.assertEqual(tree_hash(directory / "raw"), before["raw"])
        self.assertEqual(tree_hash(directory / "host-cpu.jsonl"), before["samples"])
        self.assertEqual(tree_hash(directory / "manifest.json"), before["manifest"])
        for attempt, digest in zip(attempts, archives):
            self.assertEqual(tree_hash(directory / "attempts" / f"{attempt['run_key']}.tar.gz"), digest)

    def assert_ssh_contract(self, argv, key):
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=8", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertNotIn("StrictHostKeyChecking=no", argv)
        command = positionals(argv)[-1]
        remote = shlex.split(command)
        self.assertEqual(remote[0], "python3")
        self.assertEqual(remote[1], "/root/oe-resource-agent/experiments/run_baseline.py")
        self.assertNotIn("{", command)
        if key is None:
            self.assertNotIn("-i", argv)
            self.assertNotIn("IdentitiesOnly=yes", argv)
        else:
            self.assertEqual(argv[argv.index("-i") + 1], str(key))
            self.assertIn("IdentitiesOnly=yes", argv)
        self.assertNotIn("openeuler_ed25519", " ".join(argv))


if __name__ == "__main__":
    unittest.main()
