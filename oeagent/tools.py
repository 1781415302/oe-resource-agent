"""The same versioned tools are available to CLI, Skills and control sessions."""
import json
from pathlib import Path

from .backend import ROOT
from .capabilities import discover
from .evaluate import compare
from .feedback import WINDOW_SCHEMA
from .observe import WorkloadObserver
from .plugins import CgroupPlugin, NetworkHookPlugin, SecurityHookPlugin
from .protocol import Registry, ToolError, object_schema
from .runtime import load_profile, run_agent


def profile_path(name):
    choices = {path.stem: path for path in (ROOT / "profiles").glob("*.json")}
    if name not in choices:
        raise ToolError("PROFILE_UNAVAILABLE", "配置未注册。", {"available": sorted(choices)})
    return choices[name]


def create_registry(runtime=None, event_sink=None):
    registry = Registry(event_sink)
    empty = object_schema()
    name = {"type": "string", "enum": sorted(path.stem for path in (ROOT / "profiles").glob("*.json"))}
    registry.register("system.capabilities", "读取内核、BTF、cgroup 与插件能力", empty, lambda args: discover())
    def observe(arguments):
        profile = load_profile(profile_path(arguments["profile"]))
        return WorkloadObserver(profile["selectors"]).window(arguments.get("seconds", 1))
    registry.register("workload.observe", "采集按身份核对的实际 workload 时间窗口", object_schema(
        {"profile": name, "seconds": {"type": "number", "minimum": 0.2, "maximum": 10}}, ["profile"]), observe)
    registry.register("policy.list", "列出内核默认与本机可配置 SCX 策略", empty, lambda args: {
        "policies": [{"id": "kernel.default", "action": "stop owned SCX backend"},
                     {"id": "scx.slice", "parameter": "slice_us", "range": [100, 20000],
                      "optional_parameters": {"wakeup_preempt": {"type": "boolean", "default": False,
                          "status": "experimental; host-controlled comparison required"}},
                      "requires": "matching compiled native backend"}],
        "selection_evidence": "initial 0.1.4 quick; final calibration and broader workloads pending"})
    policy_fields = {"policy_id": {"type": "string", "enum": ["kernel.default", "scx.slice"]},
                     "slice_us": {"type": "integer", "minimum": 100, "maximum": 20000},
                     "wakeup_preempt": {"type": "boolean"}}
    def validate_policy(arguments):
        if arguments["policy_id"] == "scx.slice" and "slice_us" not in arguments:
            raise ToolError("INVALID_POLICY", "scx.slice 需要 slice_us。")
        if arguments["policy_id"] == "kernel.default" and ("slice_us" in arguments or "wakeup_preempt" in arguments):
            raise ToolError("INVALID_POLICY", "内核默认策略不接受时间片参数。")
        return {"valid": True, "plan": arguments, "scope": "parameter validation; load compatibility requires guest build"}
    registry.register("policy.validate", "核对参数与策略语义", object_schema(policy_fields, ["policy_id"]), validate_policy)
    def active():
        if runtime is None:
            raise ToolError("CONTROL_SESSION_REQUIRED", "策略控制需要持续运行的 serve 会话或 Agent 内部上下文。")
        return runtime
    registry.register("policy.status", "读取会话的实际策略、map 与动作版本", empty,
                      lambda args: active().status())
    registry.register("metrics.submit", "提交同一策略和任务身份内的业务窗口；SLO会话可据此保留或回退策略",
                      WINDOW_SCHEMA, lambda args: active().submit_metrics(args), mutates=True)
    registry.register("metrics.status", "读取SLO反馈阶段、基线和最近评价", empty,
                      lambda args: active().status())
    def apply(arguments):
        validate_policy({key: value for key, value in arguments.items() if key != "expected_revision"})
        return active().apply(arguments["policy_id"], arguments.get("slice_us"), arguments["expected_revision"],
                              wakeup_preempt=arguments.get("wakeup_preempt", False))
    registry.register("policy.apply", "按预期版本应用策略并回读真实参数", object_schema(
        {**policy_fields, "expected_revision": {"type": "integer", "minimum": 0}}, ["policy_id", "expected_revision"]), apply, mutates=True)
    registry.register("policy.rollback", "停止本会话原生后端并确认恢复默认调度", object_schema(
        {"expected_revision": {"type": "integer", "minimum": 0}}, ["expected_revision"]),
                      lambda args: active().apply("kernel.default", expected_revision=args["expected_revision"]), mutates=True)
    metric_fields = {key: {"type": "number", "minimum": 0} for key in ("frontend_p99_ms", "frontend_rps", "background_eps")}
    goals = object_schema({"max_p99_ms": {"type": "number", "minimum": 0.001},
                           "min_frontend_fraction": {"type": "number", "minimum": 0, "maximum": 1},
                           "min_background_fraction": {"type": "number", "minimum": 0, "maximum": 1}},
                          ["max_p99_ms", "min_frontend_fraction", "min_background_fraction"])
    registry.register("policy.evaluate", "评估实际业务窗口的延迟、吞吐与后台约束；缺少指标时不作收益判定", object_schema(
        {"before": object_schema(metric_fields), "after": object_schema(metric_fields), "goals": goals}, ["before", "after", "goals"]),
                      lambda args: compare(args["before"], args["after"], args["goals"]))
    def execute_agent(arguments):
        if runtime is not None:
            raise ToolError("CONTROL_SESSION_OWNED", "当前持久会话已有控制器，请另开独立run命令。")
        return run_agent(profile_path(arguments["profile"]), arguments["duration_seconds"], dry_run=arguments.get("dry_run", False))
    registry.register("agent.run", "执行有限时长的采样、决策、控制与回退闭环", object_schema(
        {"profile": name, "duration_seconds": {"type": "number", "minimum": 1, "maximum": 43200},
         "dry_run": {"type": "boolean"}}, ["profile", "duration_seconds"]),
                      execute_agent, mutates=True)
    CgroupPlugin().register(registry)
    NetworkHookPlugin().register(registry)
    SecurityHookPlugin().register(registry)
    return registry
