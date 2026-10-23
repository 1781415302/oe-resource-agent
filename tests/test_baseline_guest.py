"""Guest baseline driver tests. Operating-system actions are mocked."""
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from oeagent.measurement import load_profile, validate_request
from oeagent.protocol import ToolError

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "experiments" / "baseline-profile.json"
BOOT = "boot-test"
SOURCE = {"oe-resource-agent/experiments/run_baseline.py": "c" * 64}
NATIVE = "a" * 64
WRK2 = "b" * 64


def profile():
    return load_profile(PROFILE_PATH)


def binaries():
    return {
        "native": NATIVE,
        "wrk2": WRK2,
        "native_build": {"binary": "/opt/oe/native", "binary_sha256": NATIVE},
        "wrk2_build": {
            "binary": "/opt/oe/wrk",
            "binary_sha256": WRK2,
            "measurement_protocol": "oe_wrk2_windows_v1",
            "window_ms": 5000,
        },
    }


def survey_request(run_key, **overrides):
    body = {
        "schema_version": 1,
        "operation": "survey",
        "run_key": run_key,
        "profile": profile(),
        "rates": None,
        "order": [],
        "expected_boot_id": BOOT,
        "expected_source_sha256": dict(SOURCE),
        "expected_binary_sha256": {"native": NATIVE, "wrk2": WRK2},
    }
    body.update(overrides)
    return validate_request(body)


def pilot_request(run_key):
    body = survey_request(run_key)
    body["operation"] = "pilot"
    body["rates"] = {"low": 12800, "mid": 22400, "high": 28800}
    body["order"] = ["default", "fixed-1000", "adaptive"]
    return validate_request(body)


def stream_line(kind, **fields):
    payload = {"kind": kind}
    payload.update(fields)
    return "OE_WRK2_STREAM " + json.dumps(payload) + "\n"


def client_lines(window_count):
    lines = [stream_line("ready")]
    lines.extend(stream_line("window", seq=index, final=False) for index in range(1, window_count + 1))
    lines.append(stream_line("footer"))
    return lines


class Feed:
    def __init__(self, lines):
        self.lines = list(lines)
        self.consumed = 0

    def baseline_read_chunk(self, timeout):
        if self.lines:
            self.consumed += 1
            return self.lines.pop(0)
        if timeout:
            time.sleep(timeout)
        return None


class Proc:
    def __init__(self, role, exit_when_drained):
        self.role = role
        self.exit_when_drained = exit_when_drained
        self.pid = None
        self.start_ticks = None
        self.stdout = None
        self.returncode = None
        self.stopped = False
        self.stop_recorded = False

    def poll(self):
        if self.stopped:
            if self.returncode is None:
                self.returncode = -15
            return self.returncode
        if self.role == "client" and self.exit_when_drained and self.stdout is not None and not self.stdout.lines:
            self.returncode = 0
            return 0
        return None


class Manager:
    def __init__(self, trial_dir, allowed, client_factory, forced_errors):
        self.trial_dir = Path(trial_dir)
        self.allowed = allowed
        self.client_factory = client_factory
        self.forced_errors = forced_errors
        self.path = self.trial_dir / "driver-events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.procs = []
        self.trace = []
        self.client_starts = 0
        self.client_argv = None
        self.client_early_stop = False
        self.client_feed = None
        self.background_started_at = None
        self.background_stopped_at = None
        self._next_pid = 500 + len(allowed)

    def _write(self, row):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def start(self, role, argv, cpus, logfile, pipe=False):
        del logfile, pipe
        self._next_pid += 1
        proc = Proc(role, exit_when_drained=role == "client")
        proc.pid = self._next_pid
        proc.start_ticks = 1000 + proc.pid
        self.allowed[proc.pid] = list(cpus)
        if role == "client":
            lines, exit_when_drained = self.client_factory(list(argv))
            proc.exit_when_drained = exit_when_drained
            proc.stdout = Feed(lines)
            self.client_starts += 1
            self.client_argv = list(argv)
            self.client_feed = proc.stdout
        self.procs.append(proc)
        self.trace.append(("start", role))
        self._write({"monotonic": 1, "event": "process_started", "pid": proc.pid, "start_ticks": proc.start_ticks,
                     "boot_id": BOOT, "role": role, "command": list(argv)})
        if role == "background" and self.client_feed is not None and self.background_started_at is None:
            self.background_started_at = self.client_feed.consumed
        return proc

    def stop(self, process):
        if process.stop_recorded:
            return
        if process.role == "client" and process.stdout is not None and process.stdout.lines:
            self.client_early_stop = True
        if process.returncode is None:
            process.stopped = True
            process.returncode = -15
        process.stop_recorded = True
        self.trace.append(("stop", process.role))
        if process.role == "background" and self.client_feed is not None and self.background_stopped_at is None:
            self.background_stopped_at = self.client_feed.consumed
        self._write({"monotonic": 2, "event": "process_stopped", "pid": process.pid, "role": process.role,
                     "returncode": process.returncode})

    def stop_all(self):
        for process in reversed(self.procs):
            self.stop(process)
        return list(self.forced_errors)

    def close(self):
        return None


class Sampler:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("", encoding="utf-8")

    def sample(self, phase, pids):
        del phase, pids

    def close(self):
        return None


class Reader:
    def finish(self):
        self.done = True


class Runtime:
    def __init__(self, mode):
        self.mode = mode
        self.stop_requested = False
        self.backend = None
        self._event = threading.Event()

    def policy_snapshot(self):
        if self.mode == "fixed":
            return {"policy_id": "scx.slice", "slice_us": 1000, "revision": 1, "wakeup_preempt": False}
        return {"policy_id": "kernel.default", "slice_us": None, "revision": 0, "wakeup_preempt": False}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class GuestTests(unittest.TestCase):
    def test_compiler_context_flag_is_not_a_source_mismatch(self):
        import experiments.run_baseline as baseline

        expected = compile("value = 1\n", "sample.py", "exec")
        cached = expected.replace(co_flags=expected.co_flags ^ 0x1000000)
        self.assertNotEqual(cached, expected)
        self.assertTrue(baseline._same_code(cached, expected))
        other = compile("value = 1\n", "other.py", "exec")
        self.assertFalse(baseline._same_code(other, expected))

    def test_foreign_bytecode_is_replaced_before_the_audit(self):
        import importlib.util
        import marshal
        import experiments.run_baseline as baseline

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        source = directory / "sample.py"
        source.write_text("value = 1\n", encoding="utf-8", newline="\n")
        cache = directory / "sample.pyc"
        cache.write_bytes(b"\0" * 32)
        with patch.object(importlib.util, "cache_from_source", return_value=str(cache)):
            baseline._rewrite_foreign_bytecode(directory)
        expected = compile(source.read_bytes(), str(source), "exec")
        self.assertEqual(marshal.loads(cache.read_bytes()[16:]), expected)

    def test_import_and_help_do_not_touch_proc(self):
        import importlib
        import experiments.run_baseline as baseline

        def affinity(*_args, **_kwargs):
            raise AssertionError("affinity was called")

        real_open = Path.open

        def guarded_open(self, *args, **kwargs):
            name = str(self).replace("\\", "/")
            if name.startswith("/proc") or name.startswith("/sys") or "/proc/" in name or name.startswith("/sys/"):
                raise AssertionError(f"opened {self}")
            return real_open(self, *args, **kwargs)

        source = (ROOT / "experiments" / "run_baseline.py").read_text(encoding="utf-8")
        self.assertNotIn("stop_active", source)
        self.assertNotIn("run_controlled_cycle", source)
        self.assertNotIn("pkill", source)
        with patch("os.sched_getaffinity", affinity, create=True), \
                patch("os.sched_setaffinity", affinity, create=True), \
                patch.object(Path, "open", guarded_open):
            reloaded = importlib.reload(baseline)
            with self.assertRaises(SystemExit) as caught:
                reloaded.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_boot_change_rejects_before_workload(self):
        import experiments.run_baseline as baseline
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(baseline, "_boot_id", return_value="other-boot"), \
                    patch.object(baseline, "_make_manager", side_effect=AssertionError("workload started")), \
                    patch.object(baseline, "_acquire_lock", side_effect=AssertionError("lock acquired")):
                summary = baseline.execute_request(survey_request("boot-change"), Path(tmp))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("boot", summary["error"])
        self.assertEqual(summary["trials"], [])
        self.assertIsNone(summary["snapshot"])

    def test_source_change_rejects_before_workload(self):
        import experiments.run_baseline as baseline

        def wrong_source():
            return {"oe-resource-agent/experiments/run_baseline.py": "d" * 64}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(baseline, "_boot_id", return_value=BOOT), \
                    patch.object(baseline, "_source_hashes", wrong_source), \
                    patch.object(baseline, "_make_manager", side_effect=AssertionError("workload started")):
                summary = baseline.execute_request(survey_request("source-change"), Path(tmp))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("source", summary["error"])
        self.assertEqual(summary["trials"], [])

    def test_lock_conflict_does_not_start_or_archive(self):
        import experiments.run_baseline as baseline
        managers = []

        def busy():
            class Held:
                def __enter__(self):
                    raise ToolError("RESOURCE_BUSY", "已有基准或 Agent 控制该虚拟机。")

                def __exit__(self, *_args):
                    return False

            return Held()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "lock-conflict"
            path = root / "request.json"
            path.write_text(json.dumps(survey_request(key)), encoding="utf-8")
            stdout = io.StringIO()
            with self._patches(baseline, managers, client_mode="success", lock=busy), \
                    patch.object(baseline, "ROOT", root), redirect_stdout(stdout):
                code = baseline.main(["--request", str(path)])
            summary = json.loads((root / "results" / f"baseline-{key}" / "summary.json").read_text(encoding="utf-8"))
            payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertIsNone(payload["archive"])
        self.assertIsNone(payload["archive_sha256"])
        self.assertEqual(payload["run_key"], key)
        self.assertIn("lock", summary["error"])
        self.assertEqual(summary["trials"], [])
        self.assertEqual(managers, [])
        self.assertFalse((root / "results" / f"baseline-{key}" / "archive-eligible").exists())

    def test_ready_timeout_stops_one_client(self):
        import experiments.run_baseline as baseline
        managers = []
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            with self._patches(baseline, managers, client_mode="hang"), \
                    patch.object(baseline.time, "monotonic", clock.monotonic), \
                    patch.object(baseline.time, "sleep", clock.sleep):
                summary = baseline.execute_request(survey_request("ready-timeout"), Path(tmp))
        self.assertNotEqual(summary["status"], "completed")
        self.assertEqual(len(summary["trials"]), 1)
        self.assertIn("calibration", summary["trials"][0]["error"])
        self.assertEqual(len(managers), 1)
        self.assertEqual(managers[0].client_starts, 1)
        self.assertNotIn(("start", "background"), managers[0].trace)
        self.assertIn(("stop", "client"), managers[0].trace)
        self.assertIn("-R2000", managers[0].client_argv)
        self.assertFalse(managers[0].client_early_stop)

    def test_cleanup_failure_is_not_completed(self):
        import experiments.run_baseline as baseline
        managers = []
        with tempfile.TemporaryDirectory() as tmp:
            with self._patches(baseline, managers, client_mode="success", forced_errors=["nginx did not exit"]):
                summary = baseline.execute_request(survey_request("cleanup-fail"), Path(tmp))
        self.assertNotEqual(summary["status"], "completed")
        self.assertEqual(summary["status"], "failed")
        self.assertIn("nginx did not exit", summary["cleanup_errors"])
        self.assertEqual(len(summary["trials"]), 1)
        self.assertIn("nginx did not exit", summary["trials"][0]["cleanup_errors"])

    def test_dynamic_action_uses_one_client(self):
        import experiments.run_baseline as baseline
        managers = []
        loaded = profile()
        with tempfile.TemporaryDirectory() as tmp:
            with self._patches(baseline, managers, client_mode="success"):
                summary = baseline.execute_request(pilot_request("dynamic-client"), Path(tmp))
        dynamic = [trial for trial in summary["trials"] if trial["scenario"] == "dynamic"]
        self.assertEqual(len(dynamic), 3)
        phase = loaded["pilot_dynamic_phase_windows"]
        dynamic_managers = [manager for manager in managers if manager.client_argv
                            and any(item.startswith("-d100") for item in manager.client_argv)]
        self.assertEqual(len(dynamic_managers), 3)
        for trial, manager in zip(dynamic, dynamic_managers):
            self.assertEqual(manager.client_starts, 1)
            self.assertFalse(manager.client_early_stop)
            self.assertEqual([kind for kind, _role in manager.trace if _role == "client" and kind == "start"], ["start"])
            starts = [role for kind, role in manager.trace if kind == "start"]
            self.assertLess(starts.index("client"), starts.index("background"))
            self.assertEqual(manager.background_started_at, 1 + phase)
            self.assertEqual(manager.background_stopped_at, 1 + phase * 2)
            self.assertEqual([item["kind"] for item in trial["workload_events"]], ["background_start", "background_stop"])
            self.assertEqual({item["pid"] for item in trial["workload_events"]}, {trial["workload_events"][0]["pid"]})
            self.assertLessEqual(trial["workload_events"][0]["finished_ns"], trial["workload_events"][1]["started_ns"])
            clients = [key for key in trial["process_returncodes"] if key.startswith("client:")]
            self.assertEqual(len(clients), 1)
            argv = manager.client_argv
            self.assertIn("-t1", argv)
            self.assertIn("-c32", argv)
            self.assertIn("-T3s", argv)
            self.assertIn("-R28800", argv)
            self.assertIn("OE_WRK2_WINDOW_MS=5000", argv)
            self.assertTrue(any(item.startswith("-d100s") for item in argv))
        for manager in managers:
            if manager.client_argv and any(item.startswith("-d50s") for item in manager.client_argv):
                starts = [role for kind, role in manager.trace if kind == "start"]
                self.assertLess(starts.index("background"), starts.index("client"))
                self.assertEqual(starts.count("client"), 1)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual([call["mode"] for call in self.controller_calls], ["default", "default", "fixed", "fixed", "adaptive", "adaptive"])
        self.assertEqual([call["fixed_slice_us"] for call in self.controller_calls], [None, None, 1000, 1000, None, None])
        self.assertTrue(all(call["wakeup_preempt"] is False for call in self.controller_calls))

    def test_stop_identity_mismatch_keeps_evidence(self):
        import experiments.run_baseline as baseline
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "stop-mismatch"
            directory = root / "results" / f"baseline-{key}"
            directory.mkdir(parents=True)
            owner = {"run_key": key, "pid": 321, "start_ticks": 10, "boot_id": "boot-a"}
            (directory / "owner.json").write_text(json.dumps(owner), encoding="utf-8")

            def pidfd(*_args, **_kwargs):
                raise AssertionError("pidfd used")

            stdout = io.StringIO()
            with patch.object(baseline, "ROOT", root), patch.object(baseline, "_boot_id", return_value="boot-b"), \
                    patch("os.pidfd_open", pidfd, create=True), redirect_stdout(stdout):
                code = baseline.main(["--stop", key])
            kept = json.loads((directory / "owner.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["stopped"])
        self.assertEqual(payload["reason"], "owner_identity_mismatch")
        self.assertEqual(kept, owner)

    def _patches(self, baseline, managers, client_mode, lock=None, forced_errors=None):
        errors = list(forced_errors or [])
        allowed = {}
        mode_holder = {"mode": "default"}
        self.controller_calls = []

        def client_factory(argv):
            if client_mode == "hang":
                return [], False
            count = 12 if any(item.startswith("-d100") or item.startswith("-d90") for item in argv) else 4
            return client_lines(count), True

        def make_manager(trial_dir):
            manager = Manager(trial_dir, allowed, client_factory, errors)
            managers.append(manager)
            return manager

        def start_controller(_log_dir, mode, fixed_slice_us, _duration, wakeup_preempt):
            self.controller_calls.append({
                "mode": mode, "fixed_slice_us": fixed_slice_us, "wakeup_preempt": wakeup_preempt})
            mode_holder["mode"] = mode
            runtime = Runtime(mode)
            errors_out = []

            def run():
                while not runtime.stop_requested:
                    runtime._event.wait(0.01)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            path = Path(_log_dir) / "events.jsonl"
            path.write_text("", encoding="utf-8")
            return runtime, thread, errors_out

        def sched():
            return "enabled" if mode_holder["mode"] == "fixed" else "disabled"

        def review(_run_dir, _record, _profile):
            return {"measurement_passed": True, "execution_passed": True, "sustained": True,
                    "client_cpu_limited": False}

        stack = ExitStack()
        stack.enter_context(patch.object(baseline, "_boot_id", return_value=BOOT))
        stack.enter_context(patch.object(baseline, "_source_hashes", return_value=dict(SOURCE)))
        stack.enter_context(patch.object(baseline, "_binary_identities", binaries))
        stack.enter_context(patch.object(baseline, "inspect_guest", return_value={
            "schema_version": 1, "boot_id": BOOT, "virtualization": "microsoft", "environment": {"ok": False},
            "native_build": {"binary_sha256": NATIVE}, "wrk2_build": {"binary_sha256": WRK2, "window_ms": 5000},
            "source_sha256": dict(SOURCE), "checks": {"ready": True}, "limitations": []}))
        stack.enter_context(patch.object(baseline, "_audit_loaded_code", return_value={"all_cached_code_matches": True}))
        stack.enter_context(patch.object(baseline, "_snapshot_sources", return_value=None))
        stack.enter_context(patch.object(baseline, "_acquire_lock", lock or (lambda: _OpenLock())))
        stack.enter_context(patch.object(baseline, "_port_free", return_value=None))
        stack.enter_context(patch.object(baseline, "_wait_http_ready", return_value=None))
        stack.enter_context(patch.object(baseline, "_have_env", return_value=True))
        stack.enter_context(patch.object(baseline, "_which", side_effect=lambda name: f"/usr/bin/{name}"))
        stack.enter_context(patch.object(baseline, "_bind_controller", return_value=None))
        stack.enter_context(patch.object(baseline, "_cpu_allowed", side_effect=lambda pid: allowed[pid]))
        stack.enter_context(patch.object(baseline, "_process_stats",
                                         side_effect=lambda pid: {"pid": pid, "start_ticks": 1000 + pid, "state": "S"}))
        stack.enter_context(patch.object(baseline, "_sched_state", sched))
        stack.enter_context(patch.object(baseline, "_make_sampler", side_effect=lambda path: Sampler(path)))
        stack.enter_context(patch.object(baseline, "_make_background_reader", return_value=Reader()))
        stack.enter_context(patch.object(baseline, "_make_manager", side_effect=make_manager))
        stack.enter_context(patch.object(baseline, "_start_controller", side_effect=start_controller))
        stack.enter_context(patch.object(baseline, "_review_trial", side_effect=review))
        stack.enter_context(patch.object(baseline, "_publish_archive", return_value=None))
        return stack


class _OpenLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


if __name__ == "__main__":
    unittest.main()
