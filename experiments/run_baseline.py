"""Guest survey and pilot driver for one owned baseline attempt.

Linux process, procfs, and scheduler actions stay inside the called functions.
Importing this module or running ``--help`` does not read ``/proc`` or change
affinity. ``ROOT.parent / "euler-bench"`` is the sibling bench tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rewrite_foreign_bytecode(directory):
    """Replace cached code objects that were not produced for this source path.

    A Windows interpreter can leave a checked-hash pyc on the shared tree.
    The hash still matches, so the existing startup check keeps it, and the
    guest then rejects the foreign code object. Rebuild that cache here.
    """
    import importlib.util
    import marshal
    import py_compile

    for source in sorted(Path(directory).glob("*.py")):
        cache = Path(importlib.util.cache_from_source(str(source)))
        expected = compile(source.read_bytes(), str(source), "exec", dont_inherit=True)
        current = None
        if cache.is_file():
            try:
                data = cache.read_bytes()
                if len(data) >= 16:
                    current = marshal.loads(data[16:])
            except (OSError, ValueError, EOFError, TypeError):
                current = None
        if current == expected:
            continue
        cache.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(source), cfile=str(cache), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)


if sys.platform.startswith("linux"):
    _rewrite_foreign_bytecode(ROOT / "oeagent")

from oeagent.measurement import audit_trial, validate_request
from oeagent.protocol import ToolError

_RUN_KEY = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_STREAM_PREFIX = "OE_WRK2_STREAM "
_OPERATION_LIMIT_SECONDS = {"survey": 1200, "pilot": 900}
_CLEANUP_LIMIT_SECONDS = 45
_STOP_WAIT_SECONDS = 45
_MODES = {"default": "default", "fixed-1000": "fixed", "adaptive": "adaptive"}
_CHECK_NAMES = (
    "linux", "root", "cpus_include_0_1_2", "sched_ext_disabled", "bpffs", "kernel_btf",
    "backend_identity", "wrk2_window_identity", "nginx", "sysbench", "taskset", "env_binary",
    "port_free", "virtualization_microsoft", "controller_affinity", "environment_collected",
    "source_hashed",
)
_BENCH_MODULES = ("__init__.py", "common.py", "environment.py", "metrics.py", "processes.py", "runner.py")


class IdentityRejected(RuntimeError):
    """Boot, source, or binary identity does not match the request."""


class _LineBuffer:
    def __init__(self):
        self.pending = ""

    def feed(self, text):
        self.pending += text
        lines = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            lines.append(line + "\n")
        return lines


class _OwnedProcesses:
    """One ProcessManager, with at most one process_stopped event per child.

    ProcessManager.stop always appends process_stopped. Calling it again from
    stop_all after an in-episode stop would invent a second exit for the same
    pid and start_ticks, so later stops skip children already stopped here.
    """

    def __init__(self, inner, stream, registry_path):
        self.inner = inner
        self.stream = stream
        self.registry_path = registry_path
        self._stopped = set()

    def start(self, role, argv, cpus, logfile, pipe=False):
        return self.inner.start(role, argv, cpus, logfile, pipe=pipe)

    def stop(self, process):
        if id(process) in self._stopped:
            return
        self.inner.stop(process)
        self._stopped.add(id(process))

    def stop_all(self):
        errors = []
        for job in reversed(self.inner.children):
            process = job["process"]
            if id(process) in self._stopped:
                continue
            try:
                self.stop(process)
            except (OSError, RuntimeError) as exc:
                errors.append(str(exc))
        # Failed stops keep the registry. Do not signal any process we did not start.
        if not errors:
            self.inner.children.clear()
            self.registry_path.unlink(missing_ok=True)
        return errors

    def close(self):
        self.stream.close()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _as_json(value, fallback):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        return fallback


def _import_bench():
    bench = ROOT.parent / "euler-bench"
    text = str(bench)
    if text not in sys.path:
        sys.path.insert(0, text)
    return bench


def _is_linux():
    return sys.platform.startswith("linux")


def _boot_id():
    _import_bench()
    from eulerbench.common import boot_id
    return boot_id() or ""


def _tracked_files():
    files = list(sorted((ROOT / "oeagent").glob("*.py")))
    files.append(ROOT / "experiments" / "run_baseline.py")
    files.append(ROOT / "experiments" / "baseline-profile.json")
    files.extend(sorted(path for path in (ROOT / "native").glob("*") if path.suffix in {".c", ".h"}))
    files.extend(sorted((ROOT / "profiles").glob("*.json")))
    bench = _import_bench() / "eulerbench"
    files.extend(bench / name for name in _BENCH_MODULES)
    unique = []
    seen = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not path.is_file():
            raise RuntimeError(f"tracked source missing: {path}")
        unique.append(path)
    return unique


def _source_key(path):
    return path.resolve().relative_to(ROOT.parent.resolve()).as_posix()


def _source_hashes():
    return {_source_key(path): _file_sha256(path) for path in _tracked_files()}


def _load_build_files():
    native = _read_json(ROOT / "state" / "build.json")
    wrk = _read_json(ROOT / "state" / "wrk2-window-build.json")
    return native, wrk


def _binary_identities():
    native, wrk = _load_build_files()
    native_hash = _file_sha256(native["binary"])
    wrk_hash = _file_sha256(wrk["binary"])
    if native_hash != native.get("binary_sha256") or wrk_hash != wrk.get("binary_sha256"):
        raise RuntimeError("binary file hash does not match its build manifest")
    if wrk.get("measurement_protocol") != "oe_wrk2_windows_v1" or wrk.get("window_ms") != 5000:
        raise RuntimeError("windowed wrk2 manifest is not oe_wrk2_windows_v1")
    return {"native": native_hash, "wrk2": wrk_hash, "native_build": native, "wrk2_build": wrk}


def _binary_pair(identities):
    return {"native": identities["native"], "wrk2": identities["wrk2"]}


def _require_expected_identity(request):
    boot = _boot_id()
    if boot != request["expected_boot_id"]:
        raise IdentityRejected("boot identity changed; refusing to reuse calibration")
    source = _source_hashes()
    if source != request["expected_source_sha256"]:
        raise IdentityRejected("source identity changed; refusing to reuse calibration")
    identities = _binary_identities()
    if _binary_pair(identities) != request["expected_binary_sha256"]:
        raise IdentityRejected("binary identity changed; refusing to reuse calibration")
    return {"boot_id": boot, "source_sha256": source, "binary_sha256": _binary_pair(identities), "builds": identities}


def _require_same_identity(captured):
    if _boot_id() != captured["boot_id"]:
        raise IdentityRejected("boot identity changed")
    if _source_hashes() != captured["source_sha256"]:
        raise IdentityRejected("source identity changed")
    identities = _binary_identities()
    if _binary_pair(identities) != captured["binary_sha256"]:
        raise IdentityRejected("binary identity changed")
    return identities


def _sched_state():
    from oeagent.backend import sched_state
    return sched_state()


def _which(name):
    import shutil
    return shutil.which(name) or ""


def _have_env():
    path = Path("/usr/bin/env")
    return path.is_file() and os.access(path, os.X_OK)


def _port_free(port):
    _import_bench()
    from eulerbench.environment import ensure_port_free
    ensure_port_free(port)


def _bind_controller(cpus):
    wanted = {int(cpu) for cpu in cpus}
    os.sched_setaffinity(0, wanted)
    if set(os.sched_getaffinity(0)) != wanted:
        raise RuntimeError("controller affinity does not match controller_cpus")


def _parse_cpu_list(text):
    cpus = set()
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            lo, hi = piece.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        else:
            cpus.add(int(piece))
    return cpus


def _cpu_allowed(pid):
    text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^Cpus_allowed_list:\s*(.+)$", text, re.M)
    if not match:
        raise RuntimeError(f"PID {pid} has no Cpus_allowed_list")
    return sorted(_parse_cpu_list(match.group(1)))


def _assert_affinity(pid, cpus):
    allowed = set(_cpu_allowed(pid))
    if allowed != set(cpus):
        raise RuntimeError(f"PID {pid} allowed CPUs {sorted(allowed)} != {list(cpus)}")


def _process_stats(pid):
    _import_bench()
    from eulerbench.metrics import process_stats
    return process_stats(pid)


def _allowed_cpus():
    if not _is_linux() or not hasattr(os, "sched_getaffinity"):
        return []
    return sorted(os.sched_getaffinity(0))


def _virtualization():
    import subprocess
    try:
        proc = subprocess.run(["systemd-detect-virt"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=5, check=False)
        text = (proc.stdout or "").strip().lower()
        if text and text != "none":
            return text
    except (OSError, subprocess.TimeoutExpired):
        pass
    for candidate in (Path("/sys/class/dmi/id/sys_vendor"), Path("/sys/class/dmi/id/product_name"),
                      Path("/sys/class/dmi/id/product_family")):
        try:
            data = candidate.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if "microsoft" in data or "hyper-v" in data:
            return "microsoft"
    return "unknown"


def _bpffs_mounted():
    import subprocess
    try:
        proc = subprocess.run(["stat", "-f", "-c", "%T", "/sys/fs/bpf"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "bpf_fs"


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _prepare_http(directory, profile):
    _import_bench()
    from eulerbench.runner import _prepare_nginx
    return _prepare_nginx(directory, profile)


def _wait_http_ready(process, url, page):
    _import_bench()
    from eulerbench.runner import _wait_ready
    _wait_ready(process, url, page)


def _make_manager(trial_dir):
    _import_bench()
    from eulerbench.processes import REGISTRY_PATH, ProcessManager
    path = Path(trial_dir) / "driver-events.jsonl"
    stream = path.open("w", encoding="utf-8")
    try:
        inner = ProcessManager(stream, trial_dir)
    except BaseException:
        stream.close()
        raise
    return _OwnedProcesses(inner, stream, REGISTRY_PATH)


def _make_sampler(path):
    _import_bench()
    from eulerbench.metrics import Sampler
    return Sampler(path, os.sysconf("SC_CLK_TCK"))


def _make_background_reader(process, raw_path, interval_path):
    _import_bench()
    from eulerbench.metrics import BackgroundReader
    return BackgroundReader(process.stdout, raw_path, interval_path)


def _acquire_lock():
    from oeagent.runtime import ResourceLock
    return ResourceLock()


def _start_controller(log_dir, mode, fixed_slice_us, duration_seconds, wakeup_preempt):
    from oeagent.runtime import AgentRuntime
    from oeagent.runtime import load_profile
    if wakeup_preempt is not False:
        raise RuntimeError("wakeup_preempt must stay false")
    agent_profile = load_profile(ROOT / "profiles" / "nginx-cpu.json")
    runtime = AgentRuntime(agent_profile, log_dir, mode=mode, fixed_slice_us=fixed_slice_us,
                           emit_changes=False, fixed_wakeup_preempt=False)
    errors = []

    def run():
        try:
            runtime.run(duration_seconds)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=run, name="baseline-controller")
    thread.start()
    return runtime, thread, errors


def _same_code(loaded, expected):
    """Compare code objects without the compiler-context flag.

    py_compile and a direct compile() call on this interpreter disagree on
    bit 0x1000000. Nested functions carry that bit inside co_consts, so plain
    equality rejects a cache that was just rebuilt from the same source.
    """
    if not hasattr(loaded, "co_code") or not hasattr(expected, "co_code"):
        return loaded == expected
    mask = ~0x1000000
    if (loaded.co_code != expected.co_code or loaded.co_names != expected.co_names
            or loaded.co_varnames != expected.co_varnames or loaded.co_filename != expected.co_filename
            or (loaded.co_flags & mask) != (expected.co_flags & mask)
            or len(loaded.co_consts) != len(expected.co_consts)):
        return False
    return all(_same_code(left, right) for left, right in zip(loaded.co_consts, expected.co_consts))


def _audit_loaded_code(destination):
    import importlib.util
    import marshal
    _rewrite_foreign_bytecode(ROOT / "oeagent")
    rows = []
    for source in sorted((ROOT / "oeagent").glob("*.py")):
        cache = Path(importlib.util.cache_from_source(str(source)))
        try:
            data = cache.read_bytes() if cache.is_file() else b""
        except OSError:
            data = b""
        flags = int.from_bytes(data[4:8], "little") if len(data) >= 16 else None
        try:
            loaded = marshal.loads(data[16:]) if len(data) >= 16 else None
            expected = compile(source.read_bytes(), str(source), "exec", dont_inherit=True)
            matches = loaded is not None and (loaded == expected or _same_code(loaded, expected))
        except Exception:
            matches = False
        rows.append({
            "file": source.relative_to(ROOT).as_posix(),
            "cached": bool(data),
            "cached_code_matches_source": bool(matches),
            "flags": flags,
            "checked_hash_matches": bool(flags == 3 and len(data) >= 16
                                         and data[8:16] == importlib.util.source_hash(source.read_bytes())),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        })
    result = {
        "schema_version": 1,
        "python": sys.version,
        "all_cached_code_matches": all(row["cached_code_matches_source"] for row in rows),
        "all_checked_hash": all(row["checked_hash_matches"] for row in rows),
        "files": rows,
    }
    _write_json(destination, result)
    if not result["all_cached_code_matches"]:
        bad = [row["file"] for row in rows if not row["cached_code_matches_source"]]
        raise RuntimeError("loaded Python cache does not match source: " + ",".join(bad))
    return result


def _snapshot_sources(run_dir, identity):
    for path in _tracked_files():
        target = run_dir / "source-files" / _source_key(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    patch = ROOT / "state" / "wrk2-window-measurement.patch"
    if patch.is_file():
        target = run_dir / "source-files" / "oe-resource-agent" / "state" / patch.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(patch.read_bytes())
    _write_json(run_dir / "source-snapshot.json", {
        "sha256": identity["source_sha256"],
        "boot_id": identity["boot_id"],
        "binary_sha256": identity["binary_sha256"],
    })


def _blank_summary(request):
    return {
        "schema_version": 1,
        "run_key": request["run_key"],
        "operation": request["operation"],
        "status": "running",
        "snapshot": None,
        "trials": [],
        "final_sched_ext": "unavailable",
        "cleanup_errors": [],
        "error": None,
    }


def _run_directory(root, run_key):
    if not _RUN_KEY.fullmatch(run_key):
        raise ValueError("Unsafe run key")
    return Path(root) / "results" / f"baseline-{run_key}"


def _failed_checks(snapshot):
    return [name for name, passed in snapshot["checks"].items() if passed is not True]


def inspect_guest(profile):
    """Collect an unbound environment snapshot, then pin the control thread."""
    checks = {name: False for name in _CHECK_NAMES}
    limitations = ["Hyper-V host management permission cannot be confirmed from the guest"]
    native_build = {}
    wrk_build = {}
    environment = {}
    source = {}
    checks["linux"] = _is_linux()
    checks["root"] = bool(checks["linux"] and hasattr(os, "geteuid") and os.geteuid() == 0)
    if checks["linux"]:
        cpus = set(_allowed_cpus())
        checks["cpus_include_0_1_2"] = {0, 1, 2}.issubset(cpus)
    try:
        checks["sched_ext_disabled"] = _sched_state() == "disabled"
    except Exception as exc:
        limitations.append(f"sched_ext unreadable: {exc}")
    checks["bpffs"] = _bpffs_mounted()
    checks["kernel_btf"] = Path("/sys/kernel/btf/vmlinux").is_file()
    try:
        native_build, wrk_build = _load_build_files()
    except Exception as exc:
        limitations.append(f"build manifest unreadable: {exc}")
        native_build, wrk_build = {}, {}
    if native_build:
        try:
            import platform
            kernel_ok = native_build.get("kernel_release") == platform.release()
            btf_ok = checks["kernel_btf"] and _file_sha256("/sys/kernel/btf/vmlinux") == native_build.get("btf_sha256")
            binary_ok = _file_sha256(native_build["binary"]) == native_build.get("binary_sha256")
            checks["backend_identity"] = bool(kernel_ok and btf_ok and binary_ok)
        except Exception as exc:
            limitations.append(f"backend identity check failed: {exc}")
    if wrk_build:
        try:
            checks["wrk2_window_identity"] = bool(
                wrk_build.get("measurement_protocol") == "oe_wrk2_windows_v1"
                and wrk_build.get("window_ms") == profile["window_ms"]
                and _file_sha256(wrk_build["binary"]) == wrk_build.get("binary_sha256"))
        except Exception as exc:
            limitations.append(f"wrk2 identity check failed: {exc}")
    checks["nginx"] = bool(_which("nginx"))
    checks["sysbench"] = bool(_which("sysbench"))
    checks["taskset"] = bool(_which("taskset"))
    checks["env_binary"] = _have_env()
    if checks["linux"]:
        try:
            _port_free(profile["port"])
            checks["port_free"] = True
        except Exception as exc:
            limitations.append(f"port check failed: {exc}")
    virtualization = _virtualization() if checks["linux"] else "unknown"
    checks["virtualization_microsoft"] = virtualization == "microsoft"
    if not Path("/proc/pressure/cpu").is_file():
        limitations.append("PSI unavailable")
    if _read_text("/proc/sys/kernel/sched_schedstats") != "1":
        limitations.append("schedstats unavailable")
    # Environment probe sees the original affinity. The control thread is pinned after it.
    if checks["linux"]:
        try:
            _import_bench()
            from eulerbench.environment import inspect_environment
            binary = native_build.get("binary") if isinstance(native_build, dict) else None
            raw = inspect_environment(str(binary) if binary else None)
            environment = _as_json(raw, {"error": "environment was not JSON"})
            checks["environment_collected"] = "error" not in environment or environment.get("error") is None
            if isinstance(environment, dict) and environment.get("error"):
                checks["environment_collected"] = False
            elif isinstance(environment, dict) and "checks" in environment:
                checks["environment_collected"] = True
        except Exception as exc:
            environment = {"error": str(exc)}
            limitations.append(f"inspect_environment failed: {exc}")
    try:
        source = _source_hashes()
        checks["source_hashed"] = bool(source)
    except Exception as exc:
        limitations.append(f"source hash failed: {exc}")
    if checks["linux"] and checks["cpus_include_0_1_2"]:
        try:
            _bind_controller(profile["controller_cpus"])
            checks["controller_affinity"] = True
        except Exception as exc:
            limitations.append(f"controller affinity failed: {exc}")
    return {
        "schema_version": 1,
        "boot_id": _boot_id(),
        "virtualization": virtualization,
        "environment": environment if isinstance(environment, dict) else {"error": "environment was not an object"},
        "native_build": _as_json(native_build, {}),
        "wrk2_build": _as_json(wrk_build, {}),
        "source_sha256": source if isinstance(source, dict) else {},
        "checks": checks,
        "limitations": limitations,
    }


def _install_signals():
    previous = {}

    def handler(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            continue
    return previous


def _restore_signals(previous):
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            continue


def _client_duration(profile, operation, scenario):
    if operation == "survey":
        return int(profile["survey_duration_seconds"])
    # Dynamic phases need two extra windows: the background start and stop
    # overlap one window each, and those transition windows do not count.
    windows = profile["pilot_mixed_windows"] if scenario == "mixed" else profile["pilot_dynamic_phase_windows"] * 3 + 2
    return int(profile["calibration_timeout_seconds"] + windows * (profile["window_ms"] // 1000) + 5)


def _window_phase(window, events):
    """Same boundary rule as measurement._workload_phase, or None without timestamps."""
    start = window.get("start_ns")
    end = window.get("end_ns")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    boundaries = []
    for event in events:
        for key in ("started_ns", "finished_ns"):
            value = event.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                boundaries.append(value)
    if any(start < boundary < end for boundary in boundaries):
        return "transition"
    phase = "service"
    ordered = sorted(events, key=lambda event: (event.get("started_ns") or 0, 0 if event.get("kind") == "background_start" else 1))
    for event in ordered:
        event_start = event.get("started_ns")
        event_end = event.get("finished_ns")
        if not isinstance(event_start, int) or isinstance(event_start, bool):
            continue
        if start < event_start:
            return phase
        if isinstance(event_end, int) and not isinstance(event_end, bool) and event_start <= start < event_end:
            return "transition"
        phase = "mixed" if event.get("kind") == "background_start" else "service"
    return phase


def _client_argv(binary, profile, duration, rate, url):
    # /usr/bin/env puts the window variable on this exec only. The parent environ stays unchanged.
    if not _have_env():
        raise RuntimeError("/usr/bin/env is unavailable")
    return ["/usr/bin/env", f"OE_WRK2_WINDOW_MS={int(profile['window_ms'])}", str(binary),
            "-t1", f"-c{int(profile['connections'])}", f"-T{int(profile['client_timeout_seconds'])}s",
            f"-d{int(duration)}s", f"-R{int(rate)}", url]


def _spawn(manager, role, argv, cpus, logfile, pipe=False):
    process = manager.start(role, argv, cpus, logfile, pipe=pipe)
    stats = _process_stats(process.pid)
    ticks = None if not isinstance(stats, dict) else stats.get("start_ticks")
    if not isinstance(ticks, int) or isinstance(ticks, bool):
        raise RuntimeError(f"{role} process identity unavailable")
    _assert_affinity(process.pid, cpus)
    return process, stats


def _read_chunk(stream, timeout):
    if stream is None:
        time.sleep(timeout)
        return None
    hook = getattr(stream, "baseline_read_chunk", None)
    if hook is not None:
        return hook(timeout)
    import select
    readable, _, _ = select.select([stream], [], [], timeout)
    if not readable:
        return None
    return stream.readline()


def _workload_event(kind, started_ns, process, stats):
    finished_ns = time.monotonic_ns()
    if finished_ns < started_ns:
        finished_ns = started_ns
    return {
        "kind": kind,
        "started_ns": int(started_ns),
        "finished_ns": int(finished_ns),
        "pid": int(process.pid),
        "start_ticks": int(stats["start_ticks"]),
    }


def _sysbench_argv(sysbench):
    return [sysbench, "cpu", "--threads=1", "--cpu-max-prime=10000", "--time=3600", "--report-interval=1", "run"]


def _codes_from_driver(path):
    if not Path(path).is_file():
        return {}, "driver events missing"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return {}, str(exc)
    if text and not text.endswith("\n"):
        newline = text.rfind("\n")
        text = text[:newline + 1] if newline >= 0 else ""
    pending = {}
    codes = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return {}, "driver events malformed"
        if not isinstance(row, dict):
            return {}, "driver events malformed"
        name = row.get("event")
        role, pid = row.get("role"), row.get("pid")
        if name == "process_started":
            ticks = row.get("start_ticks")
            if (not isinstance(role, str) or not isinstance(pid, int) or isinstance(pid, bool)
                    or not isinstance(ticks, int) or isinstance(ticks, bool)):
                return {}, "driver events malformed"
            pending.setdefault((role, pid), []).append(ticks)
        elif name == "process_stopped":
            code = row.get("returncode")
            if (not isinstance(role, str) or not isinstance(pid, int) or isinstance(pid, bool)
                    or not isinstance(code, int) or isinstance(code, bool)):
                return {}, "driver events malformed"
            queue = pending.get((role, pid)) or []
            if not queue:
                continue
            codes[f"{role}:{pid}:{queue.pop(0)}"] = code
    return codes, None


def _rel(root, path):
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def _wait_fixed(runtime, thread, errors):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if errors:
            break
        snap = runtime.policy_snapshot()
        if (snap.get("policy_id") == "scx.slice" and snap.get("slice_us") == 1000
                and snap.get("wakeup_preempt") is False):
            return
        time.sleep(0.02)
    detail = "; ".join(errors) if errors else "fixed policy did not initialize"
    raise RuntimeError(detail)


def _finish_reader(reader):
    if reader is None or getattr(reader, "baseline_finished", False):
        return
    reader.finish()
    try:
        reader.baseline_finished = True
    except Exception:
        pass


def _run_trial(summary, run_dir, request, identity, deadline, condition, scenario, rate):
    profile = request["profile"]
    operation = request["operation"]
    trial_id = (f"{request['run_key']}-survey-{scenario}-{rate}" if operation == "survey"
                else f"{request['run_key']}-pilot-{condition}-{scenario}")
    trial_dir = run_dir / "trials" / trial_id
    trial_dir.mkdir(parents=True)
    mode = _MODES[condition]
    fixed_slice = 1000 if condition == "fixed-1000" else None
    duration = _client_duration(profile, operation, scenario)
    phase_windows = int(profile["pilot_dynamic_phase_windows"])
    workload_events = []
    commands = []
    cleanup_errors = []
    controller_errors = []
    error = None
    measurement_damaged = False
    client_started = False
    background_used = False
    interrupted = False
    runtime = None
    thread = None
    manager = None
    sampler = None
    reader = None
    client = None
    background = None
    background_done = False
    log = None
    ready = None
    regular = 0
    timed_windows = False
    service_steady = 0
    mixed_steady = 0
    service_after = 0
    episode_started = time.monotonic()
    client_started_at = None
    parent_window = os.environ.get("OE_WRK2_WINDOW_MS")
    try:
        _require_same_identity(identity)
        if time.monotonic() >= deadline or time.monotonic() - episode_started >= profile["episode_timeout_seconds"]:
            raise RuntimeError("operation deadline" if time.monotonic() >= deadline else "episode timeout")
        runtime, thread, controller_errors = _start_controller(
            trial_dir, mode, fixed_slice, profile["episode_timeout_seconds"], False)
        if mode == "fixed":
            _wait_fixed(runtime, thread, controller_errors)
        nginx = _which("nginx")
        sysbench = _which("sysbench")
        if not nginx or not sysbench:
            raise RuntimeError("nginx or sysbench is unavailable")
        _port_free(profile["port"])
        config, page = _prepare_http(trial_dir, profile)
        url = f"http://127.0.0.1:{profile['port']}/index.html"
        manager = _make_manager(trial_dir)
        server_argv = [nginx, "-p", str(trial_dir) + "/", "-c", str(config)]
        commands.append({"role": "server", "argv": server_argv})
        server, _server_stats = _spawn(manager, "server", server_argv, profile["service_cpus"],
                                       trial_dir / "nginx.log")
        _wait_http_ready(server, url, page)
        sampler = _make_sampler(trial_dir / "samples.jsonl")
        if scenario == "mixed":
            background, reader, background_used = _start_background(
                manager, sysbench, profile, trial_dir, workload_events, commands)
        wrk_binary = identity["builds"]["wrk2_build"]["binary"]
        client_argv = _client_argv(wrk_binary, profile, duration, rate, url)
        commands.append({"role": "client", "argv": client_argv})
        before_env = os.environ.get("OE_WRK2_WINDOW_MS")
        client, _client_stats = _spawn(manager, "client", client_argv, profile["client_cpus"],
                                       trial_dir / "client.log", pipe=True)
        client_started = True
        client_started_at = time.monotonic()
        if os.environ.get("OE_WRK2_WINDOW_MS") != before_env:
            raise RuntimeError("parent environment was modified")
        buffer = _LineBuffer()
        log = (trial_dir / "client.log").open("w", encoding="utf-8")
        next_sample = 0.0
        while True:
            now = time.monotonic()
            if client.poll() is not None:
                break
            if now - episode_started >= profile["episode_timeout_seconds"]:
                error = error or "episode timeout"
                break
            if now >= deadline:
                error = error or "operation deadline"
                break
            if server.poll() is not None:
                raise RuntimeError("server exited")
            if background is not None and not background_done and background.poll() is not None:
                raise RuntimeError("background exited")
            if controller_errors:
                raise RuntimeError(controller_errors[0])
            if (ready is None and client_started_at is not None
                    and now - client_started_at >= profile["calibration_timeout_seconds"]):
                error = "calibration timeout"
                break
            if now >= next_sample:
                _sample(sampler, scenario, runtime, profile, server, client, background)
                state = _sched_state()
                if mode == "default" and state != "disabled":
                    raise RuntimeError(f"default condition changed sched_ext to {state}")
                if mode == "fixed" and state != "enabled":
                    raise RuntimeError(f"fixed condition sched_ext is {state}")
                next_sample = time.monotonic() + profile["sample_seconds"]
            chunk = _read_chunk(client.stdout, 0.05)
            if chunk is None:
                continue
            if chunk == "":
                break
            log.write(chunk)
            log.flush()
            for raw_line in buffer.feed(chunk):
                record = _parse_stream_line(raw_line)
                if record is None:
                    if raw_line.startswith(_STREAM_PREFIX):
                        measurement_damaged = True
                    continue
                if record.get("kind") == "ready" and ready is None:
                    ready = record
                    continue
                if record.get("kind") != "window" or record.get("final") is not False:
                    continue
                if ready is None:
                    measurement_damaged = True
                    continue
                regular += 1
                # Act on the windows actually read. Do not invent the time as seq * 5s.
                # Timestamped dynamic windows ignore the transition window that contains
                # the background start or stop, so each phase still yields four steady windows.
                phase = _window_phase(record, workload_events) if scenario == "dynamic" else None
                if phase is None:
                    if scenario == "dynamic" and regular == phase_windows and background is None:
                        background, reader, background_used = _start_background(
                            manager, sysbench, profile, trial_dir, workload_events, commands)
                    elif scenario == "dynamic" and regular == phase_windows * 2 and background is not None and not background_done:
                        _stop_background(manager, background, reader, workload_events)
                        background_done = True
                        reader = None
                else:
                    timed_windows = True
                    if phase == "service" and background is None:
                        service_steady += 1
                        if service_steady >= phase_windows:
                            background, reader, background_used = _start_background(
                                manager, sysbench, profile, trial_dir, workload_events, commands)
                    elif phase == "mixed" and background is not None and not background_done:
                        mixed_steady += 1
                        if mixed_steady >= phase_windows:
                            _stop_background(manager, background, reader, workload_events)
                            background_done = True
                            reader = None
                    elif phase == "service" and background_done:
                        service_after += 1
        if error == "calibration timeout" and client is not None and client.poll() is None:
            manager.stop(client)
        while client is not None and client.stdout is not None:
            extra = _read_chunk(client.stdout, 0)
            if not extra:
                break
            log.write(extra)
            log.flush()
            buffer.feed(extra)
        if buffer.pending:
            log.write(buffer.pending)
            log.flush()
            measurement_damaged = True
        if client is not None and client.poll() not in (None, 0) and not error:
            error = f"client exit {client.poll()}"
        if ready is None and not error:
            error = "client exited before Ready"
        if scenario == "dynamic" and client is not None and client.poll() == 0 and not error:
            short = (service_steady < phase_windows or mixed_steady < phase_windows or service_after < phase_windows
                     if timed_windows else regular < phase_windows * 3)
            if short:
                measurement_damaged = True
        if background is not None and not background_done:
            _stop_background(manager, background, reader, workload_events)
            background_done = True
            reader = None
        if os.environ.get("OE_WRK2_WINDOW_MS") != parent_window:
            raise RuntimeError("parent environment was modified")
    except KeyboardInterrupt:
        interrupted = True
        error = error or "interrupted"
    except Exception as exc:
        error = error or f"{type(exc).__name__}: {exc}"
    finally:
        if runtime is not None:
            runtime.stop_requested = True
        if thread is not None and getattr(thread, "ident", None):
            thread.join(timeout=min(20, _CLEANUP_LIMIT_SECONDS))
            if thread.is_alive():
                cleanup_errors.append("controller thread did not stop")
        if controller_errors and not error:
            error = "; ".join(str(item) for item in controller_errors)
        if manager is not None:
            try:
                stop_errors = manager.stop_all() or []
            except Exception as exc:
                stop_errors = [str(exc)]
            cleanup_errors.extend(str(item) for item in stop_errors)
            closer = getattr(manager, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:
                    cleanup_errors.append(str(exc))
        try:
            _finish_reader(reader)
        except Exception as exc:
            cleanup_errors.append(str(exc))
        if sampler is not None:
            try:
                sampler.close()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if log is not None and not log.closed:
            log.close()
        codes, code_problem = _codes_from_driver(trial_dir / "driver-events.jsonl")
        if code_problem and not error:
            error = code_problem
        if client_started and not any(key.startswith("client:") for key in codes) and not error:
            error = "client return code missing"
        if cleanup_errors and not error:
            error = "cleanup failed"
        if measurement_damaged and not error:
            # Kept out of trial.error so a structurally bad but exited client stays an
            # execution-neutral measurement failure for the caller to audit.
            pass
        record = {
            "trial_id": trial_id,
            "condition": condition,
            "scenario": scenario,
            "rate": int(rate),
            "client_log": _rel(run_dir, trial_dir / "client.log"),
            "samples_log": _rel(run_dir, trial_dir / "samples.jsonl"),
            "background_log": _rel(run_dir, trial_dir / "background.log") if background_used else None,
            "background_intervals_log": _rel(run_dir, trial_dir / "background-intervals.jsonl") if background_used else None,
            "agent_events_log": _rel(run_dir, trial_dir / "events.jsonl"),
            "driver_events_log": _rel(run_dir, trial_dir / "driver-events.jsonl"),
            "workload_events": workload_events,
            "process_returncodes": codes,
            "cleanup_errors": [str(item) for item in cleanup_errors],
            "error": error,
        }
        record["measurement_damaged"] = measurement_damaged
        _write_json(trial_dir / "commands.json", commands)
        _write_json(trial_dir / "trial.json", {key: value for key, value in record.items() if key != "measurement_damaged"})
        summary["trials"].append({key: value for key, value in record.items() if key != "measurement_damaged"})
        for item in record["cleanup_errors"]:
            if item not in summary["cleanup_errors"]:
                summary["cleanup_errors"].append(item)
    if interrupted:
        raise KeyboardInterrupt(error or "interrupted")
    return record


def _start_background(manager, sysbench, profile, trial_dir, workload_events, commands):
    argv = _sysbench_argv(sysbench)
    commands.append({"role": "background", "argv": argv})
    started_ns = time.monotonic_ns()
    process, stats = _spawn(manager, "background", argv, profile["service_cpus"],
                            trial_dir / "background.log", pipe=True)
    workload_events.append(_workload_event("background_start", started_ns, process, stats))
    reader = _make_background_reader(process, trial_dir / "background.log", trial_dir / "background-intervals.jsonl")
    return process, reader, True


def _stop_background(manager, process, reader, workload_events):
    stats = _process_stats(process.pid)
    if not isinstance(stats, dict) or not isinstance(stats.get("start_ticks"), int) or isinstance(stats.get("start_ticks"), bool):
        # The start event already recorded identity. Re-read can fail once the pid is gone.
        stats = {"start_ticks": workload_events[-1]["start_ticks"]} if workload_events else None
    if stats is None:
        raise RuntimeError("background process identity unavailable")
    started_ns = time.monotonic_ns()
    if process.poll() is None:
        manager.stop(process)
    else:
        manager.stop(process)
    workload_events.append(_workload_event("background_stop", started_ns, process, stats))
    _finish_reader(reader)


def _sample(sampler, scenario, runtime, profile, server, client, background):
    pids = {"controller": os.getpid(), "server": server.pid}
    if client is not None and client.poll() is None:
        pids["client"] = client.pid
    if background is not None and background.poll() is None:
        pids["background"] = background.pid
    backend = getattr(runtime, "backend", None)
    native = getattr(backend, "process", None) if backend is not None else None
    if native is not None and native.poll() is None:
        _assert_affinity(native.pid, profile["controller_cpus"])
        pids["native"] = native.pid
    sampler.sample(scenario, pids)


def _parse_stream_line(raw_line):
    if not raw_line.startswith(_STREAM_PREFIX):
        return None
    try:
        item = json.loads(raw_line[len(_STREAM_PREFIX):])
    except json.JSONDecodeError:
        return None
    if not isinstance(item, dict):
        return None
    return item


def _review_trial(run_dir, record, profile):
    try:
        return audit_trial(run_dir, record, profile)
    except ValueError as exc:
        return {"measurement_passed": False, "execution_passed": False, "sustained": None,
                "client_cpu_limited": False, "issues": [str(exc)]}


def _publish_archive(run_dir, run_key):
    target_dir = Path("/tmp")
    if not target_dir.is_dir():
        return None
    target = target_dir / f"oe-baseline-{run_key}.tar.gz"
    temporary = target.with_name(target.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with tarfile.open(temporary, "w:gz") as bundle:
        bundle.add(str(run_dir), arcname=run_dir.name)
    os.replace(temporary, target)
    info = {"archive": str(target), "archive_sha256": _file_sha256(target)}
    _write_json(run_dir / "published-archive.json", info)
    return info


def _normalize(summary):
    if summary["status"] == "running":
        summary["status"] = "failed"
        summary["error"] = summary["error"] or "run did not finish"
    for trial in summary["trials"]:
        for item in trial["cleanup_errors"]:
            if item not in summary["cleanup_errors"]:
                summary["cleanup_errors"].append(item)
    dirty = bool(summary["cleanup_errors"]) or any(trial["error"] or trial["cleanup_errors"] for trial in summary["trials"])
    if summary["status"] == "completed" and (dirty or summary["snapshot"] is None):
        summary["status"] = "failed"
        summary["error"] = summary["error"] or ("cleanup failed" if summary["cleanup_errors"] else "run failed")
    if summary["status"] == "failed" and summary["error"] is None and summary["cleanup_errors"]:
        summary["error"] = "cleanup failed"
    try:
        summary["final_sched_ext"] = str(_sched_state())
    except Exception as exc:
        summary["final_sched_ext"] = "unavailable"
        if summary["status"] == "completed":
            summary["status"] = "failed"
            summary["error"] = summary["error"] or f"sched_ext unreadable: {exc}"
    if summary["status"] == "completed" and summary["final_sched_ext"] != "disabled":
        summary["status"] = "failed"
        summary["error"] = summary["error"] or f"sched_ext is {summary['final_sched_ext']}"
    return summary


def _persist(run_dir, summary):
    stored = {key: value for key, value in summary.items()}
    _write_json(run_dir / "summary.json", stored)


def execute_request(request, root):
    request = validate_request(request)
    if request["operation"] not in ("survey", "pilot"):
        raise ValueError("execute_request only accepts survey or pilot")
    summary = _blank_summary(request)
    run_dir = None
    eligible = False
    operation_started = time.monotonic()
    previous = _install_signals()
    try:
        identity = _require_expected_identity(request)
        wrk = identity["builds"]["wrk2_build"]
        if (wrk.get("measurement_protocol") != "oe_wrk2_windows_v1"
                or wrk.get("window_ms") != request["profile"]["window_ms"]):
            raise RuntimeError("windowed wrk2 manifest does not match the profile")
        run_dir = _run_directory(root, request["run_key"])
        try:
            run_dir.mkdir(parents=True)
        except FileExistsError as exc:
            raise RuntimeError("run directory already exists") from exc
        _persist(run_dir, summary)
        _write_json(run_dir / "request.json", request)
        snapshot = inspect_guest(request["profile"])
        summary["snapshot"] = snapshot
        failed = _failed_checks(snapshot)
        if failed:
            summary["status"] = "failed"
            summary["error"] = "required guest checks failed: " + ",".join(failed)
        elif snapshot["boot_id"] != request["expected_boot_id"]:
            raise IdentityRejected("boot identity changed; refusing to reuse calibration")
        elif snapshot["source_sha256"] != request["expected_source_sha256"]:
            raise IdentityRejected("source identity changed; refusing to reuse calibration")
        else:
            _audit_loaded_code(run_dir / "python-cache-audit.json")
            _snapshot_sources(run_dir, identity)
            _require_same_identity(identity)
            stats = _process_stats(os.getpid())
            ticks = None if not isinstance(stats, dict) else stats.get("start_ticks")
            if not isinstance(ticks, int) or isinstance(ticks, bool) or not _boot_id():
                raise RuntimeError("controller process identity unavailable")
            _write_json(run_dir / "owner.json", {
                "run_key": request["run_key"],
                "pid": os.getpid(),
                "start_ticks": ticks,
                "boot_id": _boot_id(),
            })
            deadline = operation_started + _OPERATION_LIMIT_SECONDS[request["operation"]]
            try:
                with _acquire_lock():
                    # Lock-holder state wins. Never stop the other owner or an unknown scheduler.
                    if _sched_state() != "disabled":
                        raise RuntimeError("Another SCX scheduler is active")
                    _port_free(request["profile"]["port"])
                    _bind_controller(request["profile"]["controller_cpus"])
                    (run_dir / "archive-eligible").write_text("1\n", encoding="utf-8")
                    eligible = True
                    _run_locked(summary, run_dir, request, identity, deadline)
            except ToolError as exc:
                summary["status"] = "failed"
                summary["error"] = f"resource lock busy: {exc}"
            if summary["status"] == "running":
                summary["status"] = "completed"
            if summary["status"] != "failed":
                _require_same_identity(identity)
    except KeyboardInterrupt:
        summary["status"] = "interrupted"
        summary["error"] = summary["error"] or "interrupted"
    except IdentityRejected as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = summary["error"] or f"{type(exc).__name__}: {exc}"
    finally:
        _restore_signals(previous)
    _normalize(summary)
    if run_dir is not None and run_dir.is_dir():
        _persist(run_dir, summary)
        if eligible and summary["status"] != "running":
            _publish_archive(run_dir, summary["run_key"])
    elif summary["error"]:
        print(summary["error"], file=sys.stderr)
    return summary


def _run_locked(summary, run_dir, request, identity, deadline):
    profile = request["profile"]
    operation = request["operation"]
    measurement_failed = False
    if operation == "survey":
        plan = [("default", scenario, rate) for scenario in ("service", "mixed") for rate in profile["survey_rates"]]
    else:
        plan = [(condition, scenario, request["rates"]["high"])
                for condition in request["order"] for scenario in ("mixed", "dynamic")]
    scenario_open = True
    current_scenario = None
    for condition, scenario, rate in plan:
        if operation == "survey" and scenario != current_scenario:
            current_scenario = scenario
            scenario_open = True
        if operation == "survey" and not scenario_open:
            continue
        if time.monotonic() >= deadline:
            summary["status"] = "failed"
            summary["error"] = "operation deadline"
            return
        record = _run_trial(summary, run_dir, request, identity, deadline, condition, scenario, rate)
        damaged = bool(record.pop("measurement_damaged", False))
        if record["error"] or record["cleanup_errors"]:
            summary["status"] = "failed"
            summary["error"] = summary["error"] or record["error"] or "cleanup failed"
            return
        audit = _review_trial(run_dir, record, profile)
        if not audit["execution_passed"]:
            summary["status"] = "failed"
            summary["error"] = summary["error"] or "execution failed"
            return
        if not audit["measurement_passed"] or damaged:
            measurement_failed = True
            if operation == "survey":
                summary["status"] = "failed"
                summary["error"] = summary["error"] or "measurement failed"
                return
            continue
        if operation == "survey" and (audit["client_cpu_limited"] or audit["sustained"] is not True):
            scenario_open = False
    if measurement_failed:
        summary["status"] = "failed"
        summary["error"] = summary["error"] or "measurement failed"


def stop_owned(run_key, root):
    """Signal only the saved baseline process instance for this run key."""
    if not isinstance(run_key, str) or not _RUN_KEY.fullmatch(run_key):
        raise ValueError("Unsafe run key")
    path = Path(root) / "results" / f"baseline-{run_key}" / "owner.json"
    if not path.is_file():
        return {"stopped": False, "reason": "owner_not_published"}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"owner record unreadable: {exc}") from exc
    if not isinstance(record, dict) or record.get("run_key") != run_key or record.get("boot_id") != _boot_id():
        raise RuntimeError("Owner identity mismatch")
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise RuntimeError("Owner identity mismatch")
    try:
        handle = os.pidfd_open(pid)
    except ProcessLookupError:
        return {"stopped": True, "reason": "already_exited"}
    except (AttributeError, OSError) as exc:
        raise RuntimeError(f"stop unconfirmed: {exc}") from exc
    try:
        current = _process_stats(pid)
        if not current or current.get("start_ticks") != record.get("start_ticks"):
            return {"stopped": True, "reason": "original_instance_exited"}
        signal.pidfd_send_signal(handle, signal.SIGTERM)
        import select
        exited = bool(select.select([handle], [], [], _STOP_WAIT_SECONDS)[0])
        return {"stopped": exited, "reason": "owned_SIGTERM"}
    finally:
        os.close(handle)


def _machine_payload(summary, root):
    info_path = Path(root) / "results" / f"baseline-{summary['run_key']}" / "published-archive.json"
    payload = {"run_key": summary["run_key"], "status": summary["status"], "archive": None, "archive_sha256": None}
    if not info_path.is_file():
        return payload
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return payload
    archive = info.get("archive")
    digest = info.get("archive_sha256")
    expected = str(Path("/tmp") / f"oe-baseline-{summary['run_key']}.tar.gz")
    if archive == expected and isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        payload["archive"] = archive
        payload["archive_sha256"] = digest
    return payload


def _main_stop(run_key):
    try:
        result = stop_owned(run_key, ROOT)
    except Exception as exc:
        reason = "owner_identity_mismatch" if "identity" in str(exc).lower() else "stop_unconfirmed"
        result = {"stopped": False, "reason": reason}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    if result.get("reason") == "owned_SIGTERM" and result.get("stopped") is not True:
        return 1
    if result.get("reason") not in ("owner_not_published", "already_exited", "original_instance_exited", "owned_SIGTERM"):
        return 1
    return 0 if result.get("stopped") is True or result.get("reason") == "owner_not_published" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one guest baseline survey or pilot.")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--stop")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if bool(args.stop) == bool(args.request):
        parser.error("provide exactly one of --request or --stop")
    if args.stop:
        return _main_stop(args.stop)
    try:
        payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
        request = validate_request(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if request["operation"] == "preflight":
        try:
            snapshot = inspect_guest(request["profile"])
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False), flush=True)
        return 0 if all(snapshot["checks"].values()) else 1
    summary = execute_request(request, ROOT)
    print(json.dumps(_machine_payload(summary, ROOT), ensure_ascii=False, allow_nan=False), flush=True)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
