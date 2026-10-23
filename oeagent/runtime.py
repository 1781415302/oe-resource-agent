"""Run the observe/decide/apply loop with ownership, audit and bounded shutdown."""
import datetime
import hashlib
import json
import os
import platform
import signal
import threading
import time
from collections import deque
from pathlib import Path
from . import __version__

from .backend import NativeBackend, ROOT, sched_state
from .observe import WorkloadObserver
from .policy import PolicyConfig, Router
from .feedback import FeedbackConfig, SloGuard, validate_window
from .protocol import ToolError


class EventLog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / "events.jsonl").open("a", encoding="utf-8")
        self.lock = threading.Lock()

    def __call__(self, event):
        row = {"schema_version": 1, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "monotonic": time.monotonic(), **event}
        with self.lock:
            self.stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            self.stream.flush()

    def close(self):
        self.stream.close()


class ResourceLock:
    def __init__(self):
        self.streams = []

    def __enter__(self):
        import fcntl
        try:
            for name in ("/run/euler-bench.lock", "/run/oeagent.lock"):
                stream = open(name, "a+")
                self.streams.append(stream)
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ToolError("RESOURCE_BUSY", "已有基准或 Agent 控制该虚拟机。") from None
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *args):
        for stream in reversed(self.streams):
            stream.close()
        self.streams.clear()


def load_profile(path):
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    if profile.get("schema_version") != 1 or not profile.get("selectors"):
        raise ToolError("INVALID_PROFILE", "配置版本或业务选择器无效。")
    if not 0.2 <= profile["sample_seconds"] <= 10 or not 5 <= profile["lease_seconds"] <= 120:
        raise ToolError("INVALID_PROFILE", "采样窗口或监督租约超出范围。")
    if profile["lease_seconds"] < profile["sample_seconds"] * 3:
        raise ToolError("INVALID_PROFILE", "监督租约必须覆盖至少三个采样窗口。")
    PolicyConfig(**profile["policy"])
    if "feedback" in profile:
        FeedbackConfig(**profile["feedback"])
    for selector in profile["selectors"]:
        if selector.get("role") not in ("latency", "batch"):
            raise ToolError("INVALID_PROFILE", "角色必须是 latency 或 batch。")
        if not selector.get("comm") and not selector.get("pids"):
            raise ToolError("INVALID_PROFILE", "选择器必须提供进程名或 PID。")
    return profile


def business_roles_present(observation):
    roles = {role for task in observation["tasks"] for role in task["roles"]}
    return {"latency", "batch"} <= roles


class AgentRuntime:
    def __init__(self, profile, log_dir, dry_run=False, mode=None, fixed_slice_us=None, emit_changes=True, fixed_wakeup_preempt=False):
        mode = mode or ("feedback" if "feedback" in profile else "adaptive")
        if mode not in ("adaptive", "fixed", "default", "feedback"):
            raise ToolError("INVALID_RUN_MODE", "运行模式必须是 adaptive、fixed 或 default。")
        if mode == "fixed" and (not isinstance(fixed_slice_us, int) or isinstance(fixed_slice_us, bool)
                                 or not 100 <= fixed_slice_us <= 20000):
            raise ToolError("INVALID_POLICY", "固定模式需要合法的 fixed_slice_us。")
        if not isinstance(fixed_wakeup_preempt, bool) or fixed_wakeup_preempt and mode != "fixed":
            raise ToolError("INVALID_POLICY", "固定抢占开关需要 fixed 模式和布尔参数。")
        self.profile = profile
        self.log = EventLog(log_dir)
        self.dry_run = dry_run
        self.observer = WorkloadObserver(profile["selectors"])
        self.router = Router(PolicyConfig(**profile["policy"]))
        self.backend = None if dry_run else NativeBackend(self.log, log_dir, profile["lease_seconds"])
        self.current = {"policy_id": "kernel.default", "slice_us": None}
        self.policy_since = time.monotonic()
        self.revision = 0
        self.stop_requested = False
        self.mode = mode
        self.fixed_slice_us = fixed_slice_us
        self.fixed_wakeup_preempt = fixed_wakeup_preempt
        self.thread_cpu_seconds = 0.0
        self._state_lock = threading.RLock()
        self.feedback = SloGuard(FeedbackConfig(**profile["feedback"])) if mode == "feedback" else None
        self.history = deque(maxlen=4096)
        self.nonmixed_windows = 0
        self.emit_changes = emit_changes

    def apply(self, policy_id, slice_us=None, expected_revision=None, source="manual", wakeup_preempt=False):
        with self._state_lock:
            if self.feedback and source == "manual" and policy_id != "kernel.default":
                raise ToolError("AUTOMATIC_CONTROL_ACTIVE", "SLO会话由业务反馈控制；请在普通控制会话手动调参。")
            result = self._apply_locked(policy_id, slice_us, expected_revision, wakeup_preempt)
            if self.feedback and source == "manual":
                self.feedback.committed({"policy_id": policy_id}, time.monotonic())
            return result

    def policy_snapshot(self):
        with self._state_lock:
            return {**self.current, "revision": self.revision, "policy_since_monotonic": self.policy_since,
                    "boot_id": self.observer.boot_id, "profile_id": self.profile["name"]}

    def status(self):
        with self._state_lock:
            actual = self.backend.snapshot() if self.backend else self.current
            snapshot = self.policy_snapshot()
            metadata = {key: snapshot[key] for key in ("revision", "policy_since_monotonic", "boot_id", "profile_id")}
            return {**metadata, **actual, "control_mode": self.mode,
                    "observation_ready": bool(self.history), "feedback": self.feedback.status() if self.feedback else None}

    def _business_identity(self, start, end):
        selected = [row for row in self.history if row["ended_monotonic"] > start
                    and row["ended_monotonic"] - row["window_seconds"] < end]
        if (not selected or selected[0]["ended_monotonic"] - selected[0]["window_seconds"] > start
                or selected[-1]["ended_monotonic"] < end):
            raise ToolError("OBSERVATION_COVERAGE_INSUFFICIENT", "业务窗口尚未被workload观察完整覆盖。")
        signatures = []
        previous = start
        for observation in selected:
            lo = observation["ended_monotonic"] - observation["window_seconds"]
            if lo - previous > max(.25, self.profile["sample_seconds"] * .25):
                raise ToolError("OBSERVATION_GAP", "业务窗口内的workload观察存在间隙。")
            previous = observation["ended_monotonic"]
            if observation["quality"]["missing"] or any(task["cpu_percent"] is None for task in observation["tasks"]):
                raise ToolError("WORKLOAD_IDENTITY_INCOMPLETE", "窗口内含新建或未完整核对的任务。")
            tasks = [{key: task[key] for key in ("identity", "roles", "allowed_cpus", "cgroups", "threads")}
                     for task in sorted(observation["tasks"], key=lambda value: value["pid"])]
            if not any("latency" in row["roles"] for row in tasks) or not any("batch" in row["roles"] for row in tasks):
                raise ToolError("WORKLOAD_NOT_MIXED", "当前反馈策略要求完整的服务与后台任务。")
            if any(len(row["roles"]) != 1 for row in tasks):
                raise ToolError("AMBIGUOUS_WORKLOAD_ROLE", "任务匹配了相互冲突的角色。")
            signatures.append(hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest())
        if len(set(signatures)) != 1:
            raise ToolError("WORKLOAD_CHANGED_IN_WINDOW", "业务窗口内的任务身份、线程数、亲和性或cgroup变化。")
        return signatures[0]

    def _feedback_action(self, decision):
        if decision["action"] == "apply":
            self.log({"event": "feedback_decided", "decision": decision, "revision": self.revision})
            result = self.apply(decision["policy_id"], decision["slice_us"], self.revision, source="feedback",
                                wakeup_preempt=decision.get("wakeup_preempt", False))
            self.feedback.committed(decision, time.monotonic())
            return result
        return self.policy_snapshot()

    def submit_metrics(self, window):
        with self._state_lock:
            if not self.feedback:
                raise ToolError("FEEDBACK_NOT_ENABLED", "当前配置未启用业务SLO反馈。")
            try:
                metrics = validate_window(window, self.profile["name"], self.observer.boot_id,
                                          self.revision, self.policy_since, time.monotonic(), self.feedback.config)
                identity = self._business_identity(window["start_monotonic"], window["end_monotonic"])
                decision = self.feedback.submit(window, metrics, identity, self.current["policy_id"], time.monotonic())
                self.log({"event": "business_window_accepted", "window": window, "derived_metrics": metrics,
                          "observed_identity": identity, "decision": decision, "revision": self.revision})
                actual = self._feedback_action(decision)
                return {"decision": decision, "actual": actual, "feedback": self.feedback.status()}
            except ToolError as exc:
                self.log({"event": "business_window_rejected", "code": exc.code, "message": str(exc)})
                raise

    def _apply_locked(self, policy_id, slice_us=None, expected_revision=None, wakeup_preempt=False):
        action_started = time.monotonic()
        if expected_revision is not None and expected_revision != self.revision:
            raise ToolError("STALE_REVISION", "策略版本已变化，请重新观察。")
        if policy_id not in ("kernel.default", "scx.slice"):
            raise ToolError("POLICY_UNAVAILABLE", "策略未验证或未注册。")
        if not isinstance(wakeup_preempt, bool) or policy_id == "kernel.default" and wakeup_preempt:
            raise ToolError("INVALID_POLICY", "抢占开关必须是SCX策略的布尔参数。")
        if policy_id == "scx.slice" and (not isinstance(slice_us, int) or isinstance(slice_us, bool)
                                        or not 100 <= slice_us <= 20000):
            raise ToolError("INVALID_POLICY", "时间片超出此版本控制范围。")
        before = dict(self.current)
        if not self.dry_run:
            if policy_id == "kernel.default":
                self.backend.stop()
                after = {"policy_id": "kernel.default", "slice_us": None, "sched_ext": sched_state()}
            elif self.backend.process is None:
                after = self.backend.start(slice_us, wakeup_preempt)
            else:
                after = self.backend.update(slice_us, wakeup_preempt)
        else:
            after = {"policy_id": policy_id, "slice_us": slice_us if policy_id == "scx.slice" else None,
                     "wakeup_preempt": wakeup_preempt, "simulated": True}
        self.current = after
        self.revision += 1
        self.policy_since = time.monotonic()
        self.log({"event": "policy_applied" if not self.dry_run else "policy_simulated", "before": before,
                  "after": after, "revision": self.revision, "dry_run": self.dry_run,
                  "action_started_monotonic": action_started,
                  "action_finished_monotonic": self.policy_since})
        return {**after, "revision": self.revision}

    def run(self, duration_seconds):
        started = time.monotonic()
        cpu_started = time.thread_time()
        self.log({"event": "agent_started", "profile": self.profile, "dry_run": self.dry_run,
                  "agent_version": __version__, "python_cache_mode": "checked_hash",
                  "kernel": platform.release(), "mode": self.mode, "fixed_slice_us": self.fixed_slice_us,
                  "profile_sha256": hashlib.sha256(json.dumps(self.profile, sort_keys=True).encode()).hexdigest()})
        try:
            if self.mode == "fixed":
                self.apply("scx.slice", self.fixed_slice_us, self.revision, wakeup_preempt=self.fixed_wakeup_preempt)
            while not self.stop_requested and time.monotonic() - started < duration_seconds:
                observation = self.observer.window(self.profile["sample_seconds"])
                if self.backend:
                    self.backend.heartbeat()
                self.log({"event": "workload_observed", "observation": observation})
                with self._state_lock:
                    self.history.append(observation)
                    if self.feedback:
                        # Explicit business roles remain valid for a CPU-bound
                        # service that rarely sleeps. Business feedback decides
                        # benefit; the sleep heuristic must not hide that role.
                        self.nonmixed_windows = 0 if business_roles_present(observation) else self.nonmixed_windows + 1
                        if self.current["policy_id"] == "scx.slice" and self.nonmixed_windows >= self.router.config.confirmations:
                            self._feedback_action(self.feedback.rollback("mixed_workload_disappeared"))
                        else:
                            self._feedback_action(self.feedback.tick(time.monotonic(), self.current["policy_id"]))
                        continue
                decision = self.router.decide(observation, self.current["slice_us"], self.current["policy_id"]) if self.mode == "adaptive" else {
                    "action": "hold", "reason": "frozen_reference_policy", "mode": self.mode,
                    "policy_id": self.current["policy_id"], "slice_us": self.current["slice_us"]}
                self.log({"event": "policy_decided", "decision": decision, "revision": self.revision})
                if decision["action"] == "apply":
                    applied = self.apply(decision["policy_id"], decision["slice_us"], self.revision)
                    self.router.applied(observation["ended_monotonic"])
                    if self.emit_changes:
                        print(json.dumps({"event": "policy_changed", "mode": decision["mode"], **applied}, ensure_ascii=False), flush=True)
        except BaseException as exc:
            self.log({"event": "agent_error", "type": type(exc).__name__, "message": str(exc)})
            raise
        finally:
            if self.backend:
                self.backend.stop()
            self.thread_cpu_seconds = time.thread_time() - cpu_started
            self.log({"event": "agent_stopped", "dry_run": self.dry_run, "sched_ext": sched_state(),
                      "revision": self.revision, "elapsed_seconds": time.monotonic() - started,
                      "thread_cpu_seconds": self.thread_cpu_seconds,
                      "cpu_scope": "Python control thread only; native helpers excluded"})
            self.log.close()


def run_agent(profile_path, duration_seconds, log_dir=None, dry_run=False):
    if platform.system() != "Linux":
        raise ToolError("PLATFORM_UNSUPPORTED", "Agent 运行需要 Linux。")
    if not dry_run and os.geteuid() != 0:
        raise ToolError("ROOT_REQUIRED", "实际调度控制需要 root。")
    profile = load_profile(profile_path)
    if not dry_run and sched_state() != "disabled":
        raise ToolError("SCHEDULER_BUSY", "已有 sched_ext 策略运行，请结束其所属实验后再启动。")
    cpu = profile.get("controller_cpu")
    if cpu is not None:
        if cpu not in os.sched_getaffinity(0):
            raise ToolError("CPU_UNAVAILABLE", "配置的控制器 CPU 不在可用亲和性中。")
        os.sched_setaffinity(0, {cpu})
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(log_dir) if log_dir else ROOT / "results" / f"{stamp}-agent-{os.getpid()}"
    runtime = AgentRuntime(profile, directory, dry_run)
    previous = {}
    def request_stop(signum, frame):
        runtime.stop_requested = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, request_stop)
    try:
        if dry_run:
            runtime.run(duration_seconds)
        else:
            with ResourceLock():
                runtime.run(duration_seconds)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return {"run_directory": str(directory), "dry_run": dry_run, "final_sched_ext": sched_state(), "revision": runtime.revision}
