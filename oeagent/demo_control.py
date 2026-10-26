"""Dashboard orchestration for measured searches and isolated scenario suites."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import time
import uuid
from dataclasses import asdict

from . import demo_evidence
from .business import align_background_intervals
from .demo_tuning import SCENARIOS, STRATEGIES, StrategySearch
from .live_observe import LiveSampler


class DemoControl:
    def _enable_autonomous(self):
        self.control_mode = "autonomous"
        self.auto_agent = True
        self.live_agent.reset("Agent 已启用，等待当前实时数据；不立即指定配置")
        self.live_sampler = LiveSampler()

    def _step_autonomous(self, metrics):
        current = self._strategy_id()
        observation = self.live_sampler.sample(metrics, self.process_ids(), current, self.policy_since, self.wrk_window)
        action = self.live_agent.observe(observation)
        if action is not None:
            readback = self._apply_strategy(action["strategy_id"])
            self.live_agent.committed(action, time.monotonic(), current)
            event = {"utc": demo_evidence.utc_now(), "observation": asdict(observation), "decision": action,
                     "kernel_readback": readback}
            with (self.work_dir / "live-agent-events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            self._log(f"[Agent 实时决策] {current} → {action['strategy_id']}：{action['reason']}", "warn")
        self.choreography_stage = f"Agent {self.live_agent.phase}：{self.live_agent.reason}"

    def _strategy_id(self, snapshot=None):
        snapshot = snapshot or self.backend.snapshot()
        if snapshot.get("policy_id") == "kernel.default":
            return "default"
        return next((key for key, value in STRATEGIES.items()
                     if value["slice_us"] == snapshot.get("slice_us")
                     and value["wakeup_preempt"] == snapshot.get("wakeup_preempt")), "unknown")

    def _release_groups(self):
        # Only migrate the dashboard's own live processes from its own groups.
        owned = {p for role, p in self.process_ids().items() if role in ("nginx", "sysbench") and p}
        for group in ("auto_latency", "auto_batch"):
            try:
                members = self.cgroup_plugin.inspect({"group": group})["procs"]
            except Exception:
                continue
            for pid in owned.intersection(members):
                self.cgroup_plugin.detach({"group": group, "pid": pid})
        self._auto_cgroup_pids = {"latency": [], "batch": []}

    def _apply_strategy(self, strategy_id):
        spec = STRATEGIES[strategy_id]
        current = self.backend.snapshot()
        if strategy_id == "default":
            if self.backend.process is not None:
                self.backend.stop()
            self._release_groups()
        elif self.backend.process is None:
            self.backend.start(spec["slice_us"], wakeup_preempt=spec["wakeup_preempt"])
        elif self._strategy_id(current) != strategy_id:
            self.backend.update(spec["slice_us"], wakeup_preempt=spec["wakeup_preempt"])
        snapshot = self.backend.snapshot()
        if self._strategy_id(snapshot) != strategy_id or snapshot.get("sched_ext") != (
                "disabled" if strategy_id == "default" else "enabled"):
            raise RuntimeError("策略实际回读与请求不一致")
        self.policy_revision += 1
        self.policy_since = time.monotonic()
        return snapshot

    def _apply_scenario(self, scenario_id):
        if scenario_id not in SCENARIOS:
            raise ValueError("未知测试场景")
        self.traffic_enabled = False
        self.sysbench_enabled = False
        self._stop_traffic_locked()
        self._ensure_sysbench()
        self.scenario_id, self.scenario = scenario_id, deepcopy(SCENARIOS[scenario_id])
        self.offered_rps = self.scenario["rps"]
        self.batch_threads = self.scenario["threads"]
        self.batch_kind = self.scenario.get("batch_kind", "sysbench")
        self.request_path = "/compute" if self.scenario.get("compute") else "/index.html"
        self.traffic_enabled = self.scenario["traffic"]
        self.sysbench_enabled = self.scenario["batch"]
        self.workload_revision += 1
        self.scenario_since = time.monotonic()
        self.front_windows.clear()
        self.auxiliary_logs.clear()
        self.wrk_window = None
        self.scene_events.append({"scenario_id": scenario_id, "scenario_label": self.scenario["label"],
                                  "workload_revision": self.workload_revision,
                                  "started_monotonic": self.scenario_since,
                                  "started_utc": demo_evidence.utc_now()})
        self._ensure_traffic()
        self._ensure_sysbench()
        self.monitor_since = None
        self.next_search_after = None
        self._log(f"[场景] {self.scenario['label']}：{self.scenario['description']}", "info")

    def _effective_scenario(self):
        scenario = deepcopy(self.scenario)
        scenario.update(id=self.scenario_id, traffic=self.traffic_enabled, batch=self.sysbench_enabled,
                        rps=self.offered_rps if self.traffic_enabled else 0, threads=self.batch_threads)
        if not self.traffic_enabled:
            scenario["objective"], scenario["p99_limit_ms"] = "throughput", None
        return scenario

    def _measurement_context(self):
        return {"workload_revision": self.workload_revision, "policy_revision": self.policy_revision,
                "process_ids": {k: v for k, v in self.process_ids().items() if k not in ("scx", "agent")},
                "scenario": self._effective_scenario()}

    def _cancel_search(self, reason="已停止测试", restore=True):
        if self.tuning_running and self.tuning:
            self.tuning.fail(reason)
        self.tuning_running = False
        self.auto_agent = False
        self.control_mode = "manual"
        if hasattr(self, "live_agent"):
            self.live_agent.pause(reason)
        self.choreography_running = False
        self.next_search_after = None
        self.monitor_since = None
        if self.suite["running"]:
            self.suite.update(running=False, status="cancelled", reason=reason)
            self._save_suite()
        if restore:
            self._apply_strategy("default")
        self.choreography_stage = reason

    def _start_search(self, repeats=1, windows=3, automatic=False, rotation=0):
        scenario = self._effective_scenario()
        if not scenario["traffic"] and not scenario["batch"]:
            raise ValueError("当前没有运行负载，请先选择场景")
        # Validate options before changing any kernel state.
        search = StrategySearch(scenario, repeats=repeats, windows=windows, rotation=rotation)
        self._apply_strategy("default")
        self.tuning = search
        self.control_mode = "search"
        if hasattr(self, "live_agent"):
            self.live_agent.pause("可选完整对照接管，独立实时 Agent 暂停")
        self.tuning_running = True
        self.auto_agent = automatic
        if automatic:
            self.auto_search_options = {"repeats": repeats, "windows": windows}
        self.tuning_run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        self.tuning_started_utc = demo_evidence.utc_now()
        self.tuning_seen = set()
        self.monitor_since = None
        self.next_search_after = None
        self.last_decision = None
        for role, pid in self.process_ids().items():
            if pid and role in ("nginx", "sysbench", "compute"):
                try:
                    audit = self.sec_plugin.audit({"pid": pid})
                    self._log(f"[安全审计] {role} PID={pid} label={audit.get('lsm_label')} cap_eff={audit.get('cap_eff_hex')}", "info")
                except Exception as exc:
                    self._log(f"[安全审计] {role} 无法读取：{exc}", "warn")
        self._log(f"[四策略对照] {scenario['label']}，{repeats} 轮，每组 {windows} 个完整窗口；不使用历史结果决策。", "info")

    def _start_suite(self, repeats=2, windows=3, scenario_ids=None):
        ids = list(SCENARIOS) if scenario_ids is None else scenario_ids
        if not isinstance(ids, list) or not ids or len(ids) > len(SCENARIOS) or any(s not in SCENARIOS for s in ids):
            raise ValueError("测试场景列表无效")
        StrategySearch(SCENARIOS[ids[0]], repeats=repeats, windows=windows)
        self._cancel_search("开始新的典型场景测试")
        self.suite = {"running": True, "status": "running", "results": [], "remaining": ids[1:],
                      "scenario_ids": ids, "total": len(ids), "repeats": repeats, "windows": windows,
                      "started_utc": demo_evidence.utc_now(), "machine": deepcopy(self.machine),
                      "reason": "正在执行真实负载的四策略测试"}
        self._apply_scenario(ids[0])
        self._start_search(repeats, windows)

    def _save_json(self, name, report):
        destination = self.repo_root / "state" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)

    def _save_suite(self):
        self._save_json("demo-suite-progress.json", self.suite)
        if self.suite.get("status") == "complete":
            self._save_json("demo-suite-latest.json", self.suite)
            self.saved_suite = deepcopy(self.suite)

    def _background_window(self, start, end):
        points = list(self.background_points)
        aligned = align_background_intervals(points, start, end)
        origin = aligned.get("estimated_origin_monotonic", 0)
        previous = 0
        selected = []
        for point in points:
            if origin + point["elapsed_seconds"] >= start and origin + previous <= end:
                selected.append({**point, "previous_elapsed_seconds": previous})
            previous = point["elapsed_seconds"]
        return {**aligned, "raw_points": selected,
                "timing_note": "sysbench elapsed time origin estimated from stdout delivery; overlap clipped; >=80% coverage and <2s delivery spread required"}

    def _samples_after(self, after, now):
        scenario = self._effective_scenario()
        if scenario["traffic"]:
            candidates = [w for w in list(self.front_windows) if w["start_monotonic"] >= after
                          and 0 <= now - w["end_monotonic"] <= 10]
        else:
            if self.tuning_last_batch_end is None or self.tuning_last_batch_end < after:
                self.tuning_last_batch_end = after
            candidates = []
            while self.tuning_last_batch_end + 5 <= now:
                start = self.tuning_last_batch_end
                candidates.append({"start_monotonic": start, "end_monotonic": start + 5,
                                   "duration_seconds": 5, "seq": start, "errors": {}})
                self.tuning_last_batch_end += 5
        samples = []
        for window in candidates:
            background = self._background_window(window["start_monotonic"], window["end_monotonic"]) if scenario["batch"] else None
            raw = {"frontend": window.get("raw_record"), "background": background}
            digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            samples.append({"window_id": f"{self.workload_revision}:{window.get('pid')}:{window['seq']}",
                "start_monotonic": window["start_monotonic"], "end_monotonic": window["end_monotonic"],
                "duration_seconds": window["duration_seconds"], "p99_ms": window.get("p99_ms"),
                "rps": window.get("rps"), "completion_fraction": window.get("completion_fraction"),
                "eps": background["events_per_sec"] if background else None,
                "background_coverage": background["covered_seconds"] / window["duration_seconds"] if background else None,
                "errors": sum(window["errors"].values()), "source_sha256": digest,
                "frontend_sha256": window.get("source_sha256"), "raw": raw})
        return samples

    def _complete_search(self):
        decision = self.tuning.decision
        self._apply_strategy(decision["strategy_id"])
        self.last_decision = deepcopy(decision)
        report = {"schema_version": 1, "run_id": self.tuning_run_id, "started_utc": self.tuning_started_utc,
                  "completed_utc": demo_evidence.utc_now(), "machine": deepcopy(self.machine),
                  "method": {"warmup_seconds": 5, "window_seconds": 5, "aggregation": "median of window p99/EPS per trial; mean and sample SD across repeated trial periods",
                             "rotation": "cyclic order across rounds; not an independent process restart per window",
                             "cgroup": "owned workloads remain outside weighted demo groups during comparisons",
                             "selection": "zero errors; completion 97%-105%; scenario p99 target and EPS floor (default 90%); minimum gains 5% latency or 3% EPS"},
                  "search": self.tuning.status(raw=True)}
        report["source_hashes"] = {name: hashlib.sha256((self.repo_root / name).read_bytes()).hexdigest()
            for name in ("experiments/web_dashboard.py", "experiments/demo_load_worker.py",
                         "oeagent/demo_tuning.py", "oeagent/demo_control.py", "oeagent/live_agent.py", "oeagent/live_observe.py")}
        report["auxiliary_logs"] = {}
        for mode, path in self.auxiliary_logs.items():
            content = Path(path).read_text(encoding="utf-8", errors="replace")
            report["auxiliary_logs"][mode] = {"path": path, "raw_stdout": content,
                "source_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "note": "burst cumulative completions/errors are additional traffic, separate from wrk2 RPS/error counters"}
        self._save_json("demo-tuning-latest.json", report)
        self._save_json("demo-tuning-runs/" + self.tuning_run_id + ".json", report)
        self.tuning_running = False
        self.monitor_since = time.monotonic() + 5
        self.monitor_context = self._measurement_context()
        self.monitor_bad_streak = 0
        self.last_monitor_feedback = time.monotonic()
        self.tuning_seen = set()
        self.tuning_last_batch_end = None
        self.choreography_stage = f"本次选择：{STRATEGIES[decision['strategy_id']]['label']} · {decision['reason']}"
        self._log("[选择结果] " + self.choreography_stage, "ok" if decision["validated"] else "warn")
        if self.suite["running"]:
            self.suite["results"].append(report)
            self._save_suite()
            if self.suite["remaining"]:
                sid = self.suite["remaining"].pop(0)
                self._apply_scenario(sid)
                self._start_search(self.suite["repeats"], self.suite["windows"], rotation=len(self.suite["results"]) % 4)
            else:
                self.suite.update(running=False, status="complete", completed_utc=demo_evidence.utc_now(),
                                  reason="典型场景测试结束；已恢复纯网页默认调度。未通过目标的场景保留失败约束。")
                self._save_suite()
                self._apply_strategy("default")
                self._apply_scenario("service")
                self.choreography_stage = self.suite["reason"]

    def _step_tuning(self):
        now = time.monotonic()
        if not self.tuning_running:
            if not self.auto_agent:
                return
            if self.next_search_after is not None:
                if now >= self.next_search_after:
                    self._start_search(automatic=True, **self.auto_search_options)
                return
            if self.monitor_since is None or not self.last_decision:
                self._start_search(automatic=True, **self.auto_search_options)
                return
            if self._measurement_context() != self.monitor_context:
                self._start_search(automatic=True, **self.auto_search_options)
                return
            reference = self.last_decision.get("reference")
            if not reference:
                return
            if now - self.last_monitor_feedback > 30:
                self._apply_strategy("default")
                self.monitor_since = None
                self.next_search_after = now + 30
                self._log("[业务保护] 30 秒没有有效反馈，恢复默认；冷却后重新比较。", "warn")
                return
            scenario = self._effective_scenario()
            for sample in self._samples_after(self.monitor_since, now):
                if sample["window_id"] in self.tuning_seen:
                    continue
                if scenario["batch"] and (sample["eps"] is None or sample["background_coverage"] < .8):
                    continue
                self.tuning_seen.add(sample["window_id"])
                self.last_monitor_feedback = now
                bad = sample["errors"] > 0
                if scenario["traffic"]:
                    bad |= not .97 <= sample["completion_fraction"] <= 1.05 or sample["p99_ms"] > scenario["p99_limit_ms"]
                if scenario["batch"] and reference["eps"]:
                    bad |= sample["eps"] < reference["eps"] * .85
                self.monitor_bad_streak = self.monitor_bad_streak + 1 if bad else 0
                if self.monitor_bad_streak >= 3:
                    self._apply_strategy("default")
                    self.monitor_since = None
                    self.next_search_after = now + 30
                    self._log("[业务保护] 连续 3 个完整窗口退化，恢复默认；冷却 30 秒后重新比较。", "warn")
                    break
            return
        if self.tuning.stage == "pending":
            strategy_id = self.tuning.plan[self.tuning.index]["strategy_id"]
            snapshot = self._apply_strategy(strategy_id)
            context = self._measurement_context()
            self.tuning.begin(time.monotonic(), context)
            self.tuning.current["kernel_snapshot"] = snapshot
            self.tuning_last_batch_end = None
            self._log(f"[试运行 {self.tuning.index + 1}/{len(self.tuning.plan)}] {STRATEGIES[strategy_id]['label']}，预热 5 秒后采集。", "info")
        context = self._measurement_context()
        if context != self.tuning.current["context"]:
            raise RuntimeError("测试期间负载、进程身份或策略版本发生变化，结果已作废")
        if now - self.tuning.current["applied_monotonic"] > 120:
            raise RuntimeError("120 秒内没有获得足够有效窗口，停止比较并恢复默认")
        for sample in self._samples_after(self.tuning.current["accept_after"], now):
            if self.tuning.accept(sample, now, context):
                if self.tuning.stage == "complete":
                    self._complete_search()
                break
        if self.tuning_running:
            current = self.tuning.current
            self.choreography_stage = (f"{self.scenario['label']} · {self.tuning.index + 1}/{len(self.tuning.plan)} 组 · "
                f"{STRATEGIES[current['strategy_id']]['label']} · {len(current['windows'])}/{self.tuning.required_windows} 个完整窗口") if current else "准备下一组配置"

    def _step_network(self):
        if not self.auto_agent:
            return
        if getattr(self, "control_mode", "search") == "autonomous" and not getattr(self, "network_auto_requested", False):
            return
        if not self.ebpf_state["attached"]:
            result = self.net_plugin.attach({"interface": "lo", "target_port": self.ebpf_port, "drop_target": False})
            self.ebpf_state.update(attached=True, drop_target=False, stats=result.get("stats") or self.ebpf_state["stats"])
            self._prev_ebpf_target_pkts = int(self.ebpf_state["stats"].get("target_packets", 0))
            return
        result = self.net_plugin.status()
        stats = result.get("stats")
        if not result.get("active") or not stats:
            self.ebpf_state["attached"] = False
            return
        self.ebpf_state["stats"] = stats
        current = int(stats.get("target_packets", 0))
        delta = max(0, current - (self._prev_ebpf_target_pkts if self._prev_ebpf_target_pkts is not None else current))
        self._prev_ebpf_target_pkts = current
        if not self.ebpf_state["drop_target"] and delta >= 15:
            self.net_plugin.attach({"interface": "lo", "target_port": self.ebpf_port, "drop_target": True})
            self.ebpf_state["drop_target"] = True
            self._ebpf_auto_cooldown = 4
            self._log(f"[网络隔离] 测试端口每采样窗口增加 {delta} 个包，进入阻断。", "warn")
        elif self.ebpf_state["drop_target"]:
            self._ebpf_auto_cooldown = max(0, self._ebpf_auto_cooldown - 1)
            if self._ebpf_auto_cooldown == 0 and delta < 15:
                self.net_plugin.attach({"interface": "lo", "target_port": self.ebpf_port, "drop_target": False})
                self.ebpf_state["drop_target"] = False
                self._log("[网络恢复] 冷却已结束且测试流量下降，解除阻断。", "ok")

    def _run_auto_agent_step(self, metrics):
        with self.lock:
            try:
                self._step_network()
            except Exception as exc:
                self._log(f"[网络扩展不可用] {exc}；CPU 测试继续。", "warn")
            try:
                if getattr(self, "control_mode", "manual") == "autonomous" and self.auto_agent:
                    self._step_autonomous(metrics)
                else:
                    self._step_tuning()
            except Exception as exc:
                self._log(f"[测试/自动控制异常] {exc}", "err")
                self._cancel_search(str(exc))

    def tuning_state(self):
        return {"running": self.tuning_running, "automatic": self.auto_agent,
                "monitoring": bool(getattr(self, "control_mode", None) == "search" and self.auto_agent and not self.tuning_running and self.last_decision and self.last_decision.get("validated")),
                "cooldown_seconds": max(0, self.next_search_after-time.monotonic()) if self.next_search_after else None,
                "search": self.tuning.status() if self.tuning else None,
                "monitor_bad_windows": self.monitor_bad_streak,
                "decision": deepcopy(self.last_decision),
                "suite": {k: deepcopy(v) for k, v in self.suite.items() if k != "results"},
                "suite_results": [{"scenario": r["search"]["scenario"], "decision": r["search"]["decision"],
                                   "completed_utc": r["completed_utc"]} for r in self.suite["results"]],
                "saved_suite": None if not self.saved_suite else {
                    "status": self.saved_suite.get("status"), "started_utc": self.saved_suite.get("started_utc"),
                    "boot_id": self.saved_suite.get("machine", {}).get("boot_id"),
                    "results": [{"scenario": r["search"]["scenario"], "decision": r["search"]["decision"]}
                                for r in self.saved_suite.get("results", [])]}}
