"""Live autonomous controller. Inputs contain observations, never scene IDs.

Short procfs/probe windows propose changes; candidate configurations are then
checked against new observations and full HTTP windows. This is an explainable
feedback controller, not a guarantee of global optimality.
"""
from collections import deque
from dataclasses import asdict, dataclass
import math
import statistics


@dataclass(frozen=True)
class AgentGoals:
    latency_ms: float = 5.0
    background_floor: float = .8
    completion_floor: float = .97
    confirmations: int = 3
    min_residence: float = 6.0
    settle_seconds: float = 1.5
    trial_seconds: float = 8.0
    max_trial_seconds: float = 24.0
    retry_seconds: float = 30.0

    def __post_init__(self):
        for key in ("latency_ms", "background_floor", "completion_floor", "min_residence", "settle_seconds",
                    "trial_seconds", "max_trial_seconds", "retry_seconds"):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid goal: {key}")
        if not .1 <= self.latency_ms <= 500 or not .5 <= self.background_floor <= 1:
            raise ValueError("延迟目标需要在 0.1–500ms，后台保留比例在 50%–100%")
        if not 0 < self.completion_floor <= 1 or type(self.confirmations) is not int or self.confirmations < 2:
            raise ValueError("Invalid completion or confirmation requirement")
        if self.max_trial_seconds < self.trial_seconds or self.trial_seconds <= self.settle_seconds:
            raise ValueError("Invalid trial timing")


@dataclass(frozen=True)
class Observation:
    sequence: int
    now: float
    identity: str
    strategy: str
    frontend_present: bool
    background_present: bool
    cpu_busy_pct: float | None
    frontend_cpu_pct: float | None
    background_cpu_pct: float | None
    wait_pct: float | None
    avg_wait_us: float | None
    background_runnable: int | None
    voluntary_per_sec: float | None
    involuntary_per_sec: float | None
    probe_p95_ms: float | None
    probe_samples: int
    probe_errors: int
    eps: float | None
    hdr_id: str | None = None
    hdr_start: float | None = None
    hdr_end: float | None = None
    hdr_p99_ms: float | None = None
    frontend_rps: float | None = None
    completion_fraction: float | None = None
    request_errors: int | None = None
    valid: bool = True

    def __post_init__(self):
        for key in ("now", "cpu_busy_pct", "frontend_cpu_pct", "background_cpu_pct", "wait_pct", "avg_wait_us",
                    "voluntary_per_sec", "involuntary_per_sec", "probe_p95_ms", "eps", "hdr_start", "hdr_end",
                    "hdr_p99_ms", "frontend_rps", "completion_fraction"):
            value = getattr(self, key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid observed measurement: {key}")
        if self.hdr_id is not None:
            if any(value is None for value in (self.hdr_start, self.hdr_end, self.hdr_p99_ms,
                                               self.frontend_rps, self.completion_fraction, self.request_errors)):
                raise ValueError("Incomplete business window")
            if not 4.5 <= self.hdr_end - self.hdr_start <= 5.5 or not 0 <= self.now - self.hdr_end <= 12:
                raise ValueError("Business window is incomplete, future-dated or expired")


class LiveAgent:
    def __init__(self, goals=None):
        self.goals = goals or AgentGoals()
        self.identity = None
        self.started = None
        self.samples = deque(maxlen=24)
        self.phase = "observing"
        self.reason = "等待实时采样；未读取场景名称或历史结果"
        self.proposal = None
        self.confirmations = 0
        self.trial = None
        self.last_change = -math.inf
        self.blocked_until = {}
        self.events = deque(maxlen=80)
        self.last_observation = None
        self.last_sequence = None
        self.error_streak = 0
        self.last_hdr_end = None
        self.last_action = None
        self.accepted_reference = None
        self.monitored_hdr_id = None
        self.monitor_bad_windows = 0
        self.eps_bad_streak = 0

    def pause(self, reason):
        self.phase, self.reason = "paused", reason
        self.trial = None
        self.samples.clear()

    def reset(self, reason="继续观察当前数据"):
        self.identity = None
        self.trial = None
        self.samples.clear()
        self.proposal, self.confirmations = None, 0
        self.phase, self.reason = "observing", reason
        self.last_sequence = None
        self.accepted_reference = None
        self.blocked_until.clear()
        self.monitored_hdr_id = None
        self.monitor_bad_windows = self.eps_bad_streak = self.error_streak = 0
        self.last_hdr_end = None

    def configure(self, goals):
        self.goals = goals
        self.reset("Agent 目标已单独更新，等待新的实时样本")

    @staticmethod
    def _summary(samples):
        output = {}
        for key in ("cpu_busy_pct", "frontend_cpu_pct", "background_cpu_pct", "wait_pct", "avg_wait_us",
                    "background_runnable", "voluntary_per_sec", "involuntary_per_sec", "probe_p95_ms", "eps"):
            values = [getattr(s, key) for s in samples if getattr(s, key) is not None]
            output[key] = statistics.median(values) if values else None
        output["sample_count"] = len(samples)
        return output

    def _action(self, target, reason, *, kind="recovery", baseline=None):
        self.reason = reason
        return {"action": "apply", "strategy_id": target, "reason": reason, "kind": kind,
                "baseline": baseline, "observations": self._summary(list(self.samples)[-3:])}

    def committed(self, action, now, current):
        self.last_change = now
        self.last_action = {k: v for k, v in action.items() if k != "baseline"}
        self.events.append({"monotonic": now, "from": current, **self.last_action})
        self.proposal, self.confirmations = None, 0
        if action["kind"] == "trial":
            self.trial = {"from": current, "target": action["strategy_id"], "since": now,
                          "kind": action["trial_kind"], "baseline": action["baseline"], "samples": []}
            self.phase, self.reason = "probing", "已应用候选，等待后续实时指标验证"
        else:
            self.trial = None
            self.phase = "cooldown" if action["kind"] == "rollback" else "holding"
        self.samples.clear()
        self.accepted_reference = None
        self.monitor_bad_windows = self.eps_bad_streak = 0

    def _rollback(self, observation, reason):
        target = self.trial["from"] if self.trial else "default"
        if self.trial:
            self.blocked_until[self.trial["target"]] = observation.now + self.goals.retry_seconds
        return self._action(target, reason, kind="rollback")

    def _evaluate_trial(self, observation):
        trial = self.trial
        elapsed = observation.now - trial["since"]
        if observation.strategy != trial["target"]:
            self.reset("实际内核配置已变化，重新观察")
            return None
        if elapsed >= self.goals.settle_seconds:
            trial["samples"].append(observation)
        fresh_hdr = observation.hdr_id is not None and observation.hdr_start is not None and observation.hdr_start >= trial["since"] + self.goals.settle_seconds
        if fresh_hdr and (observation.request_errors or 0):
            return self._rollback(observation, "候选配置出现请求错误，恢复上一配置")
        if fresh_hdr and (observation.completion_fraction is None or not self.goals.completion_floor <= observation.completion_fraction <= 1.05):
            return self._rollback(observation, "候选实际完成率未通过约束，恢复上一配置")
        if fresh_hdr and observation.hdr_p99_ms is not None and observation.hdr_p99_ms > self.goals.latency_ms:
            return self._rollback(observation, "候选完整窗口尾延迟未达到 Agent 目标，恢复上一配置")
        if elapsed < self.goals.trial_seconds or len(trial["samples"]) < 3:
            self.reason = "候选观察中：收集预热后的实时样本"
            return None
        current = self._summary(trial["samples"])
        baseline = trial["baseline"]
        if observation.background_present and (current["eps"] is None or baseline.get("eps") is None):
            if elapsed >= self.goals.max_trial_seconds:
                return self._rollback(observation, "缺少可比较的后台报告，恢复上一配置")
            self.reason = "等待有效后台吞吐，尚不确认候选收益"
            return None
        if baseline.get("eps") and current["eps"] is not None and current["eps"] < baseline["eps"] * self.goals.background_floor:
            return self._rollback(observation, "候选后台吞吐低于保留要求，恢复上一配置")
        if observation.frontend_present and not fresh_hdr:
            if elapsed >= self.goals.max_trial_seconds:
                return self._rollback(observation, "未取得可归属候选的完整业务窗口，恢复上一配置")
            self.reason = "快速指标已更新，等待候选完整业务窗口作进一步验证"
            return None
        if trial["kind"] == "throughput":
            useful = bool(baseline.get("eps") and current["eps"] and current["eps"] >= baseline["eps"] * 1.03)
            if not useful and baseline.get("eps") and baseline.get("involuntary_per_sec") and current["involuntary_per_sec"] is not None:
                useful = bool(current.get("eps") and current["eps"] >= baseline["eps"] * .98
                              and current["involuntary_per_sec"] <= baseline["involuntary_per_sec"] * .7)
        else:
            latency_better = bool(baseline.get("probe_p95_ms") and current["probe_p95_ms"] is not None
                                  and current["probe_p95_ms"] <= baseline["probe_p95_ms"] * .95)
            wait_better = bool(baseline.get("wait_pct") and current["wait_pct"] is not None
                               and current["wait_pct"] <= baseline["wait_pct"] * .8)
            latency_safe = baseline.get("probe_p95_ms") is None or (current["probe_p95_ms"] is not None
                and current["probe_p95_ms"] <= max(self.goals.latency_ms, baseline["probe_p95_ms"] * 1.2))
            useful = latency_better or (wait_better and latency_safe)
        if not useful:
            return self._rollback(observation, "本次候选没有足够实测收益，恢复上一配置并暂缓重试")
        self.events.append({"monotonic": observation.now, "action": "accept", "strategy_id": observation.strategy,
                            "reason": "后续实时指标通过保护约束且观察到收益", "baseline": baseline, "candidate": current})
        self.trial = None
        self.accepted_reference = current
        self.phase, self.reason = "accepted", "候选后续实测通过，保留当前配置并继续监测"
        return None

    def _propose(self, observation, summary):
        busy = summary["cpu_busy_pct"] or 0
        wait = summary["wait_pct"] or 0
        background = summary["background_cpu_pct"] or 0
        frontend = summary["frontend_cpu_pct"] or 0
        runnable = summary["background_runnable"] or 0
        if not observation.frontend_present:
            if observation.background_present and busy >= 70 and background >= 60 and runnable >= 2:
                return "scx-10ms", "throughput", "观测到多计算线程持续争用 CPU，试用较长时间片"
            return "default", "recovery", "未观测到前台响应竞争或多线程持续争用，使用默认调度"
        if busy < 70 and wait < 15:
            return "default", "recovery", "实时 CPU 压力和服务排队已降低，恢复默认调度"
        target_bad = (observation.hdr_p99_ms is not None and observation.hdr_p99_ms > self.goals.latency_ms) or (
            summary["probe_p95_ms"] is not None and summary["probe_p95_ms"] > self.goals.latency_ms)
        if observation.strategy == "scx-1ms" and busy >= 70 and target_bad:
            return "scx-500us", "latency", "1ms 下仍观测到延迟目标未满足，试用更短时间片"
        if busy >= 70 and wait >= 25 and (summary["avg_wait_us"] or 0) >= 200:
            if observation.strategy in ("default", "scx-10ms"):
                return "scx-1ms", "latency", "连续观测到 CPU 繁忙和服务排队，试用短时间片与唤醒抢占"
        if observation.hdr_id and not target_bad and frontend < 25 and background >= 60 and runnable >= 2:
            return "scx-10ms", "throughput", "前台完整窗口已达目标且计算竞争持续，试用较长时间片观察吞吐"
        return observation.strategy, "hold", "当前实时证据不足以支持更换配置，继续观察"

    def observe(self, observation):
        if not isinstance(observation, Observation):
            raise TypeError("Agent only accepts the live Observation contract")
        self.last_observation = observation
        if self.last_sequence is not None and observation.sequence <= self.last_sequence:
            return None
        self.last_sequence = observation.sequence
        if observation.identity != self.identity:
            self.identity, self.started = observation.identity, observation.now
            self.samples.clear()
            self.trial, self.proposal, self.confirmations = None, None, 0
            self.blocked_until.clear()
            self.last_hdr_end = None
            self.error_streak = 0
            self.accepted_reference = None
            self.monitored_hdr_id = None
            self.monitor_bad_windows = self.eps_bad_streak = 0
            self.phase, self.reason = "observing", "观测到受监控进程身份变化，重新采集；未按场景指定策略"
        if observation.hdr_id and observation.hdr_end is not None:
            self.last_hdr_end = observation.hdr_end
        self.error_streak = self.error_streak + 1 if observation.frontend_present and observation.probe_errors else 0
        if observation.strategy != "default" and self.error_streak >= 2:
            return self._action("default", "连续观测到前台探测失败，安全恢复默认", kind="rollback")
        if observation.hdr_id and observation.hdr_id != self.monitored_hdr_id:
            self.monitored_hdr_id = observation.hdr_id
            bad = bool(observation.request_errors) or observation.completion_fraction is None or not (
                self.goals.completion_floor <= observation.completion_fraction <= 1.05)
            bad |= observation.hdr_p99_ms is not None and observation.hdr_p99_ms > self.goals.latency_ms
            self.monitor_bad_windows = self.monitor_bad_windows + 1 if bad else 0
            if not self.trial and observation.strategy != "default" and self.monitor_bad_windows >= 2:
                return self._action("default", "连续两个不同的完整业务窗口退化，安全恢复默认", kind="rollback")
        reference_eps = (self.accepted_reference or {}).get("eps")
        if reference_eps and observation.eps is not None and observation.eps < reference_eps * self.goals.background_floor:
            self.eps_bad_streak += 1
        else:
            self.eps_bad_streak = 0
        if observation.strategy != "default" and self.eps_bad_streak >= 3:
            return self._action("default", "已保留配置的后台吞吐持续退化，安全恢复默认", kind="rollback")
        if not observation.frontend_present and not observation.background_present:
            self.samples.append(observation)
            self.reason = "未观测到运行中的受管业务进程"
            if len(self.samples) >= 3 and observation.strategy != "default":
                return self._action("default", "连续确认受管业务已退出，恢复默认", kind="recovery")
            return None
        if not observation.valid or observation.cpu_busy_pct is None:
            self.reason = "实时样本不完整，保持配置并等待有效数据"
            if observation.strategy != "default" and self.started is not None and observation.now - self.started > 30:
                return self._action("default", "持续缺少有效实时反馈，安全恢复默认", kind="rollback")
            return None
        feedback_since = self.last_hdr_end if self.last_hdr_end is not None else self.started
        if observation.frontend_present and observation.strategy != "default" and observation.now - feedback_since > 30:
            return self._action("default", "完整业务反馈过期，安全恢复默认", kind="rollback")
        self.samples.append(observation)
        if self.trial:
            # A vanished competitor is an observed load change, not a scene ID.
            summary = self._summary(list(self.samples)[-3:])
            if len(self.samples) >= 3 and observation.frontend_present and (summary["cpu_busy_pct"] or 0) < 65 and (summary["wait_pct"] or 0) < 10:
                return self._action("default", "候选期间观测到争用已消失，恢复默认", kind="recovery")
            return self._evaluate_trial(observation)
        if len(self.samples) < 3:
            self.phase, self.reason = "observing", f"采集实时趋势 {len(self.samples)}/3"
            return None
        summary = self._summary(list(self.samples)[-3:])
        target, kind, reason = self._propose(observation, summary)
        if target == observation.strategy:
            self.proposal, self.confirmations = None, 0
            if self.phase != "accepted": self.phase = "holding"
            self.reason = "当前配置已通过后续采样验证，继续监测" if self.phase == "accepted" else reason
            return None
        if self.blocked_until.get(target, 0) > observation.now:
            self.phase, self.reason = "cooldown", "该候选上次未通过效果验证，暂缓重试"
            return None
        if self.proposal != target:
            self.proposal, self.confirmations = target, 1
        else:
            self.confirmations += 1
        self.phase, self.reason = "confirming", reason
        if self.confirmations < self.goals.confirmations or observation.now - self.last_change < self.goals.min_residence:
            return None
        if kind != "recovery" and observation.background_present and summary["eps"] is None:
            self.reason = "已有调整迹象，等待后台吞吐报告建立基线"
            return None
        action = self._action(target, reason, kind="trial" if kind != "recovery" else "recovery", baseline=summary)
        if action["kind"] == "trial": action["trial_kind"] = kind
        return action

    def status(self):
        trial = None if not self.trial else {k: v for k, v in self.trial.items() if k != "samples"}
        return {"phase": self.phase, "reason": self.reason, "confirmations": self.confirmations,
                "required_confirmations": self.goals.confirmations, "pending_candidate": self.proposal,
                "goals": asdict(self.goals), "observation": asdict(self.last_observation) if self.last_observation else None,
                "trial": trial, "last_action": self.last_action, "events": list(self.events),
                "decision_source": "live_observations_only"}
