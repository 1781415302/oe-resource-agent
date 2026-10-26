"""Relative weighted feedback controller, with no absolute performance targets."""
from dataclasses import asdict, dataclass
import math
import statistics

from .live_agent import LiveAgent, Observation


@dataclass(frozen=True)
class AgentWeights:
    latency: float = .6
    throughput: float = .4

    def __post_init__(self):
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (self.latency, self.throughput)):
            raise ValueError("权重需要是非负有限数值")
        if self.latency + self.throughput <= 0:
            raise ValueError("至少一项权重需要大于零")


@dataclass(frozen=True)
class Timing:
    confirmations: int = 3
    min_residence: float = 6.
    settle_seconds: float = 1.5
    trial_seconds: float = 8.
    max_trial_seconds: float = 24.
    retry_seconds: float = 30.


def weighted_score(reference, measured, weights):
    """100 is the reference. Ratios remove units; logs prevent scale dominance.

    Latency uses the same full-window HDR p99 statistic on both sides. Throughput
    combines real frontend completion RPS (when the reference has it) and EPS
    by geometric mean. Missing dimensions are excluded and weights normalized.
    A monitored background without a fresh EPS report is not scored.
    """
    terms = []
    if reference.get("frontend_present") and weights.latency:
        metric = reference.get("latency_metric", "hdr_p99_ms")
        old, new = reference.get(metric), measured.get(metric)
        if not old or not new:
            return None
        terms.append((weights.latency, math.log(old/new)))
    throughput = []
    if reference.get("background_present"):
        old, new = reference.get("eps"), measured.get("eps")
        if old is None or old <= 0 or new is None:
            return None
        throughput.append(math.log(max(new, 1e-9)/old))
    old, new = reference.get("frontend_rps"), measured.get("frontend_rps")
    if old is not None and old > 0:
        if new is None:
            return None
        throughput.append(math.log(max(new, 1e-9)/old))
    if throughput and weights.throughput:
        terms.append((weights.throughput, statistics.mean(throughput)))
    if not terms:
        return None
    return 100 * math.exp(max(-50., min(50., sum(w*v for w,v in terms)/sum(w for w,_ in terms))))


class WeightedAgent(LiveAgent):
    def __init__(self, weights=None):
        # Inherited utilities use only timing fields. No p99/EPS threshold exists.
        super().__init__(Timing())
        self.weights = weights or AgentWeights()
        self.reference = None
        self.measured = {}
        self.last_valid = None
        self.last_context = None
        self.reason = "等待实时数据，按相对延迟与吞吐加权比较"

    def reset(self, reason="重新观察当前数据"):
        super().reset(reason)
        self.reference = None
        self.measured = {}
        self.last_valid = None
        self.last_context = None

    def configure(self, weights):
        self.weights = weights
        self.reset("权重已更新，不设置性能达标门槛")

    def summary(self, samples):
        result = self._summary(samples)
        result["frontend_present"] = samples[-1].frontend_present
        result["background_present"] = samples[-1].background_present
        values = [o.frontend_rps for o in samples if o.frontend_rps is not None]
        result["frontend_rps"] = statistics.median(values) if values else None
        values = [o.hdr_p99_ms for o in samples if o.hdr_p99_ms is not None]
        result["hdr_p99_ms"] = statistics.median(values) if values else None
        result["latency_metric"] = "hdr_p99_ms"
        return result

    def _evaluate_trial(self, observation):
        trial = self.trial
        elapsed = observation.now-trial["since"]
        if observation.strategy != trial["target"]:
            self.reset("内核实际配置变化，重新观察")
            return None
        if elapsed >= self.goals.settle_seconds:
            trial["samples"].append(observation)
        fresh = observation.hdr_id and observation.hdr_start >= trial["since"]+self.goals.settle_seconds
        if elapsed < self.goals.trial_seconds or len(trial["samples"]) < 3:
            self.reason = "候选采样中，等待可比较的实时趋势"
            return None
        current = self.summary(trial["samples"])
        if fresh:
            current["hdr_p99_ms"] = observation.hdr_p99_ms
            current["frontend_rps"] = observation.frontend_rps
        score = weighted_score(self.reference, current, self.weights)
        if score is None or (observation.frontend_present and not fresh):
            if elapsed >= self.goals.max_trial_seconds:
                return self._rollback(observation, "缺少可比较反馈，恢复上一配置")
            self.reason = "等待完整业务窗口及有效吞吐，不用缺测值评分"
            return None
        previous = self.measured[trial["from"]]["score"]
        self.measured[observation.strategy] = {"score": score, "metrics": current, "measured_at": observation.now}
        # This 2% margin is an anti-flapping tolerance, not an SLO.
        keep = score >= previous*1.02 or (observation.strategy == "default" and score >= previous*.99)
        if not keep:
            action = self._rollback(observation, f"候选加权分 {score:.2f}，未超过当前 {previous:.2f} 的防抖幅度，恢复上一配置")
            action["weighted_score"] = score
            return action
        self.events.append({"monotonic": observation.now, "action": "accept", "strategy_id": observation.strategy,
                            "reason": "加权实测收益通过，保留当前配置", "weighted_score": score,
                            "previous_score": previous, "reference": self.reference, "candidate": current})
        self.trial = None
        self.phase, self.reason = "accepted", f"加权实测分 {score:.2f}，保留并继续观察"
        return None

    def observe(self, observation):
        if not isinstance(observation, Observation):
            raise TypeError("Only real Observation input is accepted")
        if self.last_sequence is not None and observation.sequence <= self.last_sequence:
            return None
        self.last_sequence, self.last_observation = observation.sequence, observation
        if observation.identity != self.identity:
            self.identity, self.started = observation.identity, observation.now
            self.samples.clear()
            self.reference, self.trial, self.proposal = None, None, None
            self.measured, self.blocked_until = {}, {}
            self.confirmations = self.error_streak = 0
            self.last_hdr_end = self.last_valid = None
            self.phase, self.reason = "observing", "实际进程变化，重新建立相对基线"
        if observation.hdr_id:
            self.last_hdr_end = observation.hdr_end
        self.error_streak = self.error_streak+1 if observation.frontend_present and observation.probe_errors else 0
        bad_hdr = observation.hdr_id and observation.request_errors
        if observation.strategy != "default" and (self.error_streak >= 2 or bad_hdr):
            return self._action("default", "观测到请求错误，保护恢复默认", kind="rollback")
        if not observation.valid or observation.cpu_busy_pct is None:
            self.reason = "等待有效采样，不对缺测数据评分"
            if observation.strategy != "default" and observation.now-(self.last_valid or self.started) > 30:
                return self._action("default", "有效观测过期，保护恢复默认", kind="rollback")
            return None
        self.last_valid = observation.now
        if observation.frontend_present and observation.strategy != "default" and observation.now-(self.last_hdr_end or self.started) > 30:
            return self._action("default", "完整业务反馈过期，保护恢复默认", kind="rollback")
        self.samples.append(observation)
        if not observation.frontend_present and not observation.background_present:
            if len(self.samples) >= 3 and observation.strategy != "default":
                return self._action("default", "受管业务退出，恢复默认", kind="recovery")
            return None
        if self.trial and len(self.samples) < 3:
            return self._evaluate_trial(observation)
        if len(self.samples) < 3:
            self.phase, self.reason = "observing", f"采集相对基线 {len(self.samples)}/3"
            return None
        summary = self.summary(list(self.samples)[-3:])
        # Detect independently changing CPU pressure, without scene IDs. Policy
        # effects on waiting/latency do not invalidate their own comparisons.
        if self.reference and abs(summary["cpu_busy_pct"]-self.reference["cpu_busy_pct"]) >= 30:
            self.reference, self.trial, self.proposal = None, None, None
            self.measured, self.blocked_until = {}, {}
            self.confirmations = 0
            self.reason = "实测 CPU 压力发生明显变化，重建基线"
        if self.reference is None:
            if summary["frontend_present"] and summary["hdr_p99_ms"] is None:
                self.reason = "实时图持续更新，等待完整 HDR p99 建立可比较基线"
                return None
            if summary["background_present"] and not summary.get("eps"):
                self.reason = "等待现场后台报告建立相对基线"
                return None
            if summary["frontend_present"] and not summary["background_present"] and not self.weights.latency and summary["frontend_rps"] is None:
                self.reason = "仅选择吞吐权重，等待实际完成 RPS 建立基线"
                return None
            self.reference = summary
            self.measured[observation.strategy] = {"score": 100., "metrics": summary, "measured_at": observation.now}
            self.last_context = observation.now
        if weighted_score(self.reference, self.reference, self.weights) is None:
            self.phase, self.reason = "holding", "当前没有可评分的加权指标，继续观察"
            return None
        if self.trial:
            return self._evaluate_trial(observation)
        current_score = weighted_score(self.reference, summary, self.weights)
        if current_score is not None:
            self.measured[observation.strategy] = {"score": current_score, "metrics": summary, "measured_at": observation.now}
        # A new measurement cycle refreshes old observations instead of using
        # a historical scenario winner indefinitely.
        if observation.now-self.last_context >= 90:
            self.reference = summary
            self.measured = {observation.strategy: {"score": 100., "metrics": summary, "measured_at": observation.now}}
            self.blocked_until.clear()
            self.last_context = observation.now
        if not observation.frontend_present:
            order = ["scx-10ms", "default", "scx-1ms", "scx-500us"]
        elif summary["cpu_busy_pct"] >= 70 and (summary["wait_pct"] or 0) >= 25:
            order = ["scx-1ms", "scx-500us", "default", "scx-10ms"]
        else:
            order = ["default", "scx-1ms", "scx-500us", "scx-10ms"]
        target = next((p for p in order if p != observation.strategy and p not in self.measured
                       and self.blocked_until.get(p, 0) <= observation.now), None)
        if target is None and current_score is not None:
            alternatives = [(p, r) for p, r in self.measured.items() if p != observation.strategy
                            and self.blocked_until.get(p, 0) <= observation.now
                            and r["score"] >= current_score*1.05]
            if alternatives:
                target = max(alternatives, key=lambda item:item[1]["score"])[0]
        if target is None:
            self.phase, self.reason = "holding", "已比较本轮候选，保留加权收益较好的配置"
            self.proposal, self.confirmations = None, 0
            return None
        if target != self.proposal:
            self.proposal, self.confirmations = target, 1
        else:
            self.confirmations += 1
        self.phase, self.reason = "confirming", f"连续确认后试用 {target}，依据实测加权分决定保留"
        if self.confirmations < self.goals.confirmations or observation.now-self.last_change < self.goals.min_residence:
            return None
        action = self._action(target, self.reason, kind="trial", baseline=self.reference)
        action["trial_kind"] = "weighted"
        return action

    def status(self):
        result = super().status()
        result.pop("goals", None)
        result.update(weights=asdict(self.weights), measured_configurations=self.measured,
                      reference=self.reference, selection="relative_weighted_score", performance_targets=None,
                      latency_score_source="complete_HDR_window_p99", timing=asdict(self.goals))
        return result
