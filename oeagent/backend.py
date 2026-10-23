"""Own one native SCX instance; control its map with validated native commands."""
import hashlib
import json
import os
import platform
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .protocol import ToolError

ROOT = Path(__file__).resolve().parents[1]


def sched_state():
    try:
        return Path("/sys/kernel/sched_ext/state").read_text().strip()
    except OSError:
        return "unavailable"


class NativeBackend:
    def __init__(self, events, log_dir, lease_seconds=15):
        manifest_path = ROOT / "state/build.json"
        if not manifest_path.is_file():
            raise ToolError("BACKEND_NOT_BUILT", "请先在目标 openEuler 执行 bash build.sh。")
        self.manifest = json.loads(manifest_path.read_text())
        self.binary = Path(self.manifest["binary"])
        if self.manifest["kernel_release"] != platform.release():
            raise ToolError("KERNEL_CHANGED", "运行内核与后端构建记录不同。")
        if self.manifest["btf_sha256"] != hashlib.sha256(Path("/sys/kernel/btf/vmlinux").read_bytes()).hexdigest():
            raise ToolError("KERNEL_BTF_CHANGED", "运行内核 BTF 与后端构建记录不同。")
        if hashlib.sha256(self.binary.read_bytes()).hexdigest() != self.manifest["binary_sha256"]:
            raise ToolError("BINARY_CHANGED", "原生后端与构建哈希不同。")
        self.events = events
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.process = None
        self.pin_dir = None
        self.map_id = None
        self.generation = None
        self.reader = None
        self.ready = threading.Event()
        self.ready_data = None
        self.output_lock = threading.Lock()
        self.lock = threading.RLock()

    def _read(self, process, path):
        with path.open("w", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(line)
                log.flush()
                if line.startswith("OE_ADAPTIVE_READY "):
                    self.ready_data = json.loads(line.split(" ", 1)[1])
                    self.ready.set()

    def start(self, slice_us, wakeup_preempt=False):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise ToolError("BACKEND_ALREADY_ACTIVE", "当前后端已运行。")
            if sched_state() != "disabled":
                raise ToolError("SCHEDULER_BUSY", "已有 sched_ext 策略运行。")
            root = Path("/sys/fs/bpf/oeagent")
            if subprocess.run(["stat", "-f", "-c", "%T", "/sys/fs/bpf"], capture_output=True, text=True, check=True).stdout.strip() != "bpf_fs":
                raise ToolError("BPFFS_UNAVAILABLE", "需要在 /sys/fs/bpf 挂载 bpffs。")
            root.mkdir(mode=0o700, exist_ok=True)
            self.pin_dir = root / uuid.uuid4().hex
            self.pin_dir.mkdir(mode=0o700)
            self.ready.clear()
            self.ready_data = None
            argv = [str(self.binary), "--pin-dir", str(self.pin_dir), "--slice-us", str(slice_us), "--lease-sec", str(self.lease_seconds)]
            argv.extend(["--wakeup-preempt", "1" if wakeup_preempt else "0"])
            self.events({"event": "backend_starting", "argv": argv, "binary_sha256": self.manifest["binary_sha256"]})
            self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, errors="replace", bufsize=1)
            self.reader = threading.Thread(target=self._read, args=(self.process, self.log_dir / f"native-{self.process.pid}.log"), daemon=True)
            self.reader.start()
            until = time.monotonic() + 8
            while time.monotonic() < until:
                if self.ready.wait(timeout=0.05):
                    self.map_id = self.ready_data["map_id"]
                    self.generation = self.ready_data["generation"]
                    self.events({"event": "backend_ready", **self.ready_data})
                    return self.snapshot()
                if self.process.poll() is not None:
                    break
            self.stop()
            raise ToolError("BACKEND_START_FAILED", "后端未正常加载，请查看 native 日志。")

    def _command(self, action, slice_us=None, wakeup_preempt=None):
        if self.process is None or self.process.poll() is not None or self.map_id is None:
            raise ToolError("BACKEND_INACTIVE", "后端未运行或已退出。")
        argv = [str(self.binary), action, str(self.pin_dir / "control"), "--expected-id", str(self.map_id)]
        if action != "--get-pin":
            argv.extend(["--expected-generation", str(self.generation)])
        if slice_us is not None:
            argv.extend(["--slice-us", str(slice_us)])
        if wakeup_preempt is not None:
            argv.extend(["--wakeup-preempt", "1" if wakeup_preempt else "0"])
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            raise ToolError("CONTROL_TIMEOUT", "原生控制命令超时。") from None
        if result.returncode:
            raise ToolError("CONTROL_FAILED", result.stderr.strip() or "控制命令失败。", {"returncode": result.returncode})
        current = json.loads(result.stdout)
        if current["map_id"] != self.map_id:
            raise ToolError("MAP_ID_CHANGED", "控制 map 身份变化。")
        self.generation = current["generation"]
        return current

    def snapshot(self):
        with self.lock:
            if self.process is None:
                return {"policy_id": "kernel.default", "slice_us": None, "sched_ext": sched_state()}
            current = self._command("--get-pin")
            return {"policy_id": "scx.slice", "slice_us": current["slice_ns"] // 1000,
                    "wakeup_preempt": current["wakeup_preempt"], "kick_interval_us": current["kick_interval_ns"] // 1000,
                    "variant": "wakeup_preempt" if current["wakeup_preempt"] else "weighted_vtime",
                    "sched_ext": sched_state(), "pid": self.process.pid,
                    "map_id": self.map_id, "generation": current["generation"],
                    "max_slice_us": current["max_slice_ns"] // 1000}

    def update(self, slice_us, wakeup_preempt=False):
        with self.lock:
            current = self._command("--set-pin", slice_us, wakeup_preempt)
            if current["slice_ns"] != slice_us * 1000 or current["wakeup_preempt"] is not wakeup_preempt:
                raise ToolError("CONTROL_READBACK_MISMATCH", "回读时间片与请求值不同。")
            self.events({"event": "backend_parameter_updated", **current})
            return self.snapshot()

    def heartbeat(self):
        with self.lock:
            if self.process is not None:
                self._command("--heartbeat-pin")

    def stop(self):
        with self.lock:
            process = self.process
            if process is not None:
                for sig, timeout in ((signal.SIGINT, 5), (signal.SIGTERM, 3), (signal.SIGKILL, 3)):
                    if process.poll() is not None:
                        break
                    try:
                        process.send_signal(sig)
                    except ProcessLookupError:
                        break
                    try:
                        process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        continue
                if process.poll() is None:
                    raise ToolError("BACKEND_STOP_FAILED", "原生后端未能结束。")
                if self.reader:
                    self.reader.join(timeout=3)
                self.events({"event": "backend_stopped", "pid": process.pid, "returncode": process.returncode,
                             "sched_ext": sched_state()})
            if self.pin_dir is not None:
                # Only the private directory allocated by this instance is touched.
                pin = self.pin_dir / "control"
                if pin.exists():
                    pin.unlink()
                self.pin_dir.rmdir()
            self.process, self.pin_dir, self.map_id, self.generation = None, None, None, None
            until = time.monotonic() + 3
            while sched_state() != "disabled" and time.monotonic() < until:
                time.sleep(0.02)
            if sched_state() != "disabled":
                raise ToolError("ROLLBACK_UNVERIFIED", "后端已退出但默认调度恢复状态未确认。")
