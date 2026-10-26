"""Four-configuration search using complete, attributable measurement windows.

No scenario declares a winning scheduler. A scenario defines workload and goals;
selection uses the measurements taken during that search only.
"""
from copy import deepcopy
import math
import statistics


STRATEGIES = {
    "default": {"label": "默认 EEVDF", "slice_us": None, "wakeup_preempt": False},
    "scx-1ms": {"label": "SCX 1ms + 唤醒抢占", "slice_us": 1000, "wakeup_preempt": True},
    "scx-500us": {"label": "SCX 500μs + 唤醒抢占", "slice_us": 500, "wakeup_preempt": True},
    "scx-10ms": {"label": "SCX 10ms 无抢占", "slice_us": 10000, "wakeup_preempt": False},
}

SCENARIOS = {
    "service": {"label": "纯网页服务", "description": "Nginx 小静态页面；无后台争抢，验证专用策略是否有必要。",
                "traffic": True, "batch": False, "rps": 25600, "threads": 1,
                "objective": "latency", "p99_limit_ms": 3.0},
    "latency": {"label": "延迟优先混部", "description": "Nginx 与 1 个计算线程共用 CPU0，关注尾延迟并保护后台吞吐。",
                "traffic": True, "batch": True, "rps": 25600, "threads": 1,
                "objective": "latency", "p99_limit_ms": 3.0},
    "throughput": {"label": "吞吐优先混部", "description": "4,000 RPS 与 4 个计算线程共用 CPU0；先满足网页延迟，再比较后台吞吐。",
                   "traffic": True, "batch": True, "rps": 4000, "threads": 4,
                   "objective": "throughput", "p99_limit_ms": 8.0},
    "batch": {"label": "纯后台多线程", "description": "关闭网页流量，4 个计算线程竞争 CPU0；只比较真实计算吞吐。",
              "traffic": False, "batch": True, "rps": 0, "threads": 4,
              "objective": "throughput", "p99_limit_ms": None},
}
SCENARIOS.update({
    "burst": {"label": "突发请求混部", "description": "8,000 RPS 主流 + 每秒 200ms 的额外并发突发；后台 1 线程。突发请求单独计数。",
              "traffic": True, "batch": True, "rps": 8000, "threads": 1, "burst": True,
              "objective": "latency", "p99_limit_ms": 8.0},
    "compute": {"label": "请求内 CPU 计算", "description": "Nginx 代理 CPU0 上的 PBKDF2 计算服务（每请求 4,000 次迭代）；300 RPS + 后台 1 线程。",
                "traffic": True, "batch": True, "rps": 300, "threads": 1, "compute": True,
                "objective": "latency", "p99_limit_ms": 20.0},
    "wakeup": {"label": "频繁休眠唤醒", "description": "8,000 RPS；后台 4 线程每轮计算后休眠 2ms，模拟周期性短任务。EPS 为完成周期/s。",
               "traffic": True, "batch": True, "rps": 8000, "threads": 4, "batch_kind": "wakeup",
               "objective": "latency", "p99_limit_ms": 8.0},
    "cache": {"label": "较大工作集计算", "description": "4,000 RPS；后台 2 线程各反复散列 32MiB 数据，总工作集 64MiB。EPS 为散列次数/s。",
              "traffic": True, "batch": True, "rps": 4000, "threads": 2, "batch_kind": "cache",
              "objective": "throughput", "p99_limit_ms": 8.0},
})


def aggregate(trials):
    """Each trial contributes one median; repeats are separate trial periods."""
    rows = []
    for strategy_id, spec in STRATEGIES.items():
        periods = [t for t in trials if t["strategy_id"] == strategy_id and t["status"] == "measured"]
        row = {"strategy_id": strategy_id, "label": spec["label"], "periods": len(periods),
               "window_count": sum(len(t["windows"]) for t in periods),
               "errors": sum(w["errors"] for t in periods for w in t["windows"])}
        for field in ("p99_ms", "rps", "eps", "completion_fraction"):
            medians = [statistics.median(w[field] for w in t["windows"] if w.get(field) is not None)
                       for t in periods if any(w.get(field) is not None for w in t["windows"])]
            row[field] = statistics.mean(medians) if medians else None
            row[field + "_sd"] = statistics.stdev(medians) if len(medians) > 1 else None
        row["min_completion"] = min((w["completion_fraction"] for t in periods for w in t["windows"]
                                      if w.get("completion_fraction") is not None), default=None)
        row["max_completion"] = max((w["completion_fraction"] for t in periods for w in t["windows"]
                                      if w.get("completion_fraction") is not None), default=None)
        rows.append(row)
    return rows


def select(rows, scenario, expected_periods=1):
    """Reject incomplete/error/throughput-violating candidates before ranking."""
    rows = deepcopy(rows)
    baseline = next((r for r in rows if r["strategy_id"] == "default"), None)
    if baseline is None or baseline["periods"] < expected_periods:
        return {"strategy_id": "default", "reason": "默认基线不完整；恢复默认，不发布最优结论。", "rows": rows,
                "validated": False}
    for row in rows:
        reasons = []
        if row["periods"] < expected_periods:
            reasons.append("有效重复轮次不足")
        if row["errors"]:
            reasons.append("请求发生错误")
        if scenario["traffic"]:
            if row.get("min_completion") is None or row["min_completion"] < .97 or row["max_completion"] > 1.05:
                reasons.append("实际完成率不在 97%–105% 内")
            if row["p99_ms"] is None or row["p99_ms"] > scenario["p99_limit_ms"]:
                reasons.append("窗口 p99 未满足目标")
        if scenario["batch"]:
            if row["eps"] is None or row["eps"] <= 0:
                reasons.append("缺少有效后台吞吐")
            elif scenario["objective"] == "latency" and baseline["eps"] and row["eps"] < baseline["eps"] * scenario.get("eps_floor_fraction", .9):
                reasons.append(f"后台吞吐低于默认基线的 {scenario.get('eps_floor_fraction', .9)*100:g}%")
        row.update(eligible=not reasons, rejection_reasons=reasons)
    eligible = [r for r in rows if r["eligible"]]
    if not eligible:
        return {"strategy_id": "default", "reason": "四组配置均未通过业务约束；恢复默认，当前负载/目标需要检查。",
                "rows": rows, "validated": False}
    baseline = next(r for r in rows if r["strategy_id"] == "default")
    if scenario["objective"] == "throughput" and scenario["batch"]:
        best = max(eligible, key=lambda r: r["eps"])
        if baseline["eligible"] and best["eps"] < baseline["eps"] * 1.03:
            best, reason = baseline, "后台吞吐提升不足 3%，保留默认，避免微小波动引起切换。"
        else:
            reason = "在请求完成率、错误和延迟约束通过的配置中，选择后台 EPS 最高的一组。"
    else:
        best = min(eligible, key=lambda r: r["p99_ms"])
        if baseline["eligible"] and best["p99_ms"] > baseline["p99_ms"] * .95:
            best, reason = baseline, "默认已达目标且尾延迟改善不足 5%，保留默认，避免微小波动引起切换。"
        else:
            reason = "在完成率、错误及后台吞吐约束通过的配置中，选择窗口 p99 最低的一组。"
    return {"strategy_id": best["strategy_id"], "reason": reason, "rows": rows, "validated": True,
            "reference": deepcopy(best)}


class StrategySearch:
    """A bounded search. The caller performs kernel mutations and supplies data."""
    def __init__(self, scenario, repeats=1, windows=3, rotation=0):
        if type(repeats) is not int or repeats not in (1, 2, 3):
            raise ValueError("重复轮次只允许 1、2、3")
        if type(windows) is not int or not 2 <= windows <= 6:
            raise ValueError("每组需要 2–6 个完整窗口")
        self.scenario = deepcopy(scenario)
        self.repeats, self.required_windows = repeats, windows
        ids = list(STRATEGIES)
        self.plan = []
        for cycle in range(repeats):
            offset = (rotation + cycle) % len(ids)
            self.plan.extend({"strategy_id": s, "round": cycle + 1} for s in ids[offset:] + ids[:offset])
        self.index = 0
        self.trials = []
        self.current = None
        self.decision = None
        self.stage = "pending"
        self.reason = "等待应用第一组配置"
        self.rejections = {}
        self._seen = set()

    def begin(self, now, context):
        self.current = {**self.plan[self.index], "status": "collecting", "windows": [],
                        "applied_monotonic": now, "accept_after": now + 5.0, "context": deepcopy(context)}
        self.stage, self.reason = "warming", "等待预热结束；跳过跨策略窗口"

    def accept(self, sample, now, context):
        if self.current is None or self.stage not in ("warming", "measuring"):
            return False
        cause = None
        key = sample.get("window_id")
        if context != self.current["context"]:
            cause = "进程或策略身份变化"
        elif key in self._seen:
            return False
        elif sample.get("start_monotonic", -1) < self.current["accept_after"]:
            cause = "预热或跨策略窗口"
        elif not 4.8 <= sample.get("duration_seconds", 0) <= 8:
            cause = "测量窗口时长无效"
        elif not 0 <= now - sample.get("end_monotonic", math.inf) <= 10:
            cause = "窗口过期或时间无效"
        elif not sample.get("source_sha256"):
            cause = "缺少原始来源摘要"
        elif self.scenario["batch"] and (sample.get("eps") is None or sample.get("background_coverage", 0) < .8):
            cause = "后台报告覆盖不足"
        elif self.scenario["traffic"] and any(sample.get(k) is None for k in ("rps", "p99_ms", "completion_fraction")):
            cause = "缺少完整 HTTP 窗口"
        if cause:
            self.rejections[cause] = self.rejections.get(cause, 0) + 1
            self.reason = cause
            return False
        numeric = ("eps", "rps", "p99_ms", "completion_fraction")
        if any(sample.get(k) is not None and (not isinstance(sample[k], (int, float))
                or not math.isfinite(sample[k]) or sample[k] < 0) for k in numeric):
            self.reason = "非有限或负测量值"
            return False
        self._seen.add(key)
        self.stage, self.reason = "measuring", "采集完整窗口"
        self.current["windows"].append(deepcopy(sample))
        if len(self.current["windows"]) >= self.required_windows:
            self.current["status"] = "measured"
            self.trials.append(self.current)
            self.current = None
            self.index += 1
            if self.index == len(self.plan):
                self.decision = select(aggregate(self.trials), self.scenario, self.repeats)
                self.stage, self.reason = "complete", self.decision["reason"]
            else:
                self.stage, self.reason = "pending", "应用下一组配置"
            return True
        return False

    def fail(self, reason):
        self.stage, self.reason = "failed", str(reason)

    def status(self, raw=False):
        current = deepcopy(self.current)
        trials = deepcopy(self.trials) if raw else [
            {k: v for k, v in trial.items() if k not in ("windows", "context")}
            for trial in self.trials]
        if current and not raw:
            current["window_count"] = len(current.pop("windows"))
        return {"stage": self.stage, "reason": self.reason, "scenario": self.scenario,
                "completed_trials": self.index, "total_trials": len(self.plan),
                "required_windows": self.required_windows, "repeats": self.repeats,
                "current": current, "trials": trials, "rejections": dict(self.rejections),
                "rows": self.decision["rows"] if self.decision else aggregate(self.trials),
                "decision": deepcopy(self.decision)}
