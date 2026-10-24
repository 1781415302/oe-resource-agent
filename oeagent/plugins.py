"""Plugin interfaces and real cgroup, eBPF network, and LSM security adapters."""
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from .backend import ROOT, sched_state
from .observe import process_snapshot, read
from .protocol import ToolError, object_schema


class Plugin:
    name = "unspecified"

    def describe(self):
        raise NotImplementedError

    def register(self, registry):
        raise NotImplementedError


class CgroupPlugin(Plugin):
    name = "resource.cgroup"

    def __init__(self):
        v2 = Path("/sys/fs/cgroup/cgroup.controllers")
        if v2.is_file():
            self.version, self.root, self.filename = 2, Path("/sys/fs/cgroup/oeagent"), "cpu.weight"
            self.minimum, self.maximum = 1, 10000
        else:
            candidates = [Path("/sys/fs/cgroup/cpu,cpuacct"), Path("/sys/fs/cgroup/cpu")]
            base = next((p for p in candidates if (p / "cpu.shares").is_file()), None)
            self.version, self.root, self.filename = 1, base / "oeagent" if base else None, "cpu.shares"
            self.minimum, self.maximum = 2, 262144

    def describe(self):
        return {"name": self.name, "available": self.root is not None and platform.system() == "Linux",
                "cgroup_version": self.version, "controlled_file": self.filename,
                "value_range": [self.minimum, self.maximum], "scope": "owned oeagent child groups only",
                "cpu_weight_under_scx": "unsupported by this backend; writes require sched_ext disabled",
                "attach_processes": "supported (PID and start_ticks verified; safe detach to root cgroup)"}

    def group_path(self, name):
        if self.root is None:
            raise ToolError("CGROUP_UNAVAILABLE", "CPU cgroup 控制器不可用。")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            raise ToolError("INVALID_GROUP", "组名格式无效。")
        path = self.root / name
        if self.root.is_symlink() or path.is_symlink():
            raise ToolError("CGROUP_PATH_INVALID", "控制目录不能是符号链接。")
        return path

    def _read_procs(self, path):
        procs_file = path / "cgroup.procs"
        if not procs_file.is_file():
            return []
        return [int(line.strip()) for line in procs_file.read_text(encoding="utf-8").splitlines() if line.strip().isdigit()]

    def inspect(self, arguments):
        path = self.group_path(arguments["group"])
        if not path.is_dir():
            raise ToolError("CGROUP_NOT_FOUND", "受管组不存在。")
        return {"group": arguments["group"], "path": str(path), "cgroup_version": self.version,
                "file": self.filename, "value": int((path / self.filename).read_text().strip()),
                "procs": self._read_procs(path)}

    def create(self, arguments):
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise ToolError("ROOT_REQUIRED", "创建受管 cgroup 需要 Linux root。")
        path = self.group_path(arguments["group"])
        if self.version == 2:
            raise ToolError("CGROUP_DELEGATION_REQUIRED", "v2 需要先准备控制器委派；当前只实现 v1 创建。")
        self.root.mkdir(exist_ok=True)
        path.mkdir(exist_ok=True)
        return self.inspect(arguments)

    def apply(self, arguments):
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise ToolError("ROOT_REQUIRED", "调整 cgroup 需要 Linux root。")
        if sched_state() == "enabled":
            raise ToolError("POLICY_UNSUPPORTED", "当前 SCX 后端未实现 cgroup 权重语义，拒绝标记此动作生效。")
        before = self.inspect({"group": arguments["group"]})
        if before["value"] != arguments["expected_value"]:
            raise ToolError("STALE_RESOURCE_VALUE", "cgroup 权重已变化，请重新读取。")
        if not self.minimum <= arguments["value"] <= self.maximum:
            raise ToolError("INVALID_RESOURCE_VALUE", "权重超出实际控制器范围。")
        (self.group_path(arguments["group"]) / self.filename).write_text(str(arguments["value"]))
        after = self.inspect({"group": arguments["group"]})
        if after["value"] != arguments["value"]:
            raise ToolError("RESOURCE_READBACK_FAILED", "资源参数回读不一致。")
        return {"before": before, "after": after, "scope": "configured owned group; use resource.cgroup.attach to migrate verified tasks"}

    def attach(self, arguments):
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise ToolError("ROOT_REQUIRED", "迁移进程到受管 cgroup 需要 Linux root。")
        if sched_state() == "enabled":
            raise ToolError("POLICY_UNSUPPORTED", "当前 SCX 后端未实现 cgroup 权重语义，拒绝在 SCX 运行期迁入进程。")
        path = self.group_path(arguments["group"])
        if not path.is_dir():
            raise ToolError("CGROUP_NOT_FOUND", "受管组不存在。")
        pid = arguments["pid"]
        snap = process_snapshot(pid)
        if snap is None:
            raise ToolError("PROCESS_NOT_FOUND", "目标进程不存在或无法读取。")
        expected_ticks = arguments.get("expected_start_ticks")
        if expected_ticks is not None and snap["start_ticks"] != expected_ticks:
            raise ToolError("STALE_PROCESS_IDENTITY", "目标 PID 启动时间不匹配，拒绝迁入。")
        try:
            (path / "cgroup.procs").write_text(f"{pid}\n", encoding="utf-8")
        except OSError as exc:
            raise ToolError("CGROUP_ATTACH_FAILED", f"写入 cgroup.procs 失败：{exc}") from exc
        after = self.inspect({"group": arguments["group"]})
        if pid not in after["procs"]:
            raise ToolError("CGROUP_ATTACH_FAILED", "迁入后回读 cgroup.procs 未发现目标进程。")
        return {"group": arguments["group"], "pid": pid, "start_ticks": snap["start_ticks"],
                "comm": snap["comm"], "attached": True, "after": after}

    def detach(self, arguments):
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise ToolError("ROOT_REQUIRED", "迁出受管 cgroup 进程需要 Linux root。")
        path = self.group_path(arguments["group"])
        if not path.is_dir():
            raise ToolError("CGROUP_NOT_FOUND", "受管组不存在。")
        pid = arguments["pid"]
        current = self._read_procs(path)
        if pid in current:
            root_procs = self.root.parent / "cgroup.procs"
            try:
                root_procs.write_text(f"{pid}\n", encoding="utf-8")
            except OSError as exc:
                if process_snapshot(pid) is not None:
                    raise ToolError("CGROUP_DETACH_FAILED", f"迁回根 cgroup 失败：{exc}") from exc
        after = self.inspect({"group": arguments["group"]})
        if pid in after["procs"]:
            raise ToolError("CGROUP_DETACH_FAILED", "进程仍留在受管 cgroup 中。")
        return {"group": arguments["group"], "pid": pid, "detached": True, "after": after}

    def register(self, registry):
        group = {"type": "string", "minLength": 1, "maxLength": 64}
        pid = {"type": "integer", "minimum": 1, "maximum": 4194304}
        ticks = {"type": "integer", "minimum": 1}
        registry.register("resource.cgroup.describe", "报告 cgroup 插件实际能力与边界", object_schema(), lambda args: self.describe())
        registry.register("resource.cgroup.inspect", "读取本框架受管组的 CPU 权重与成员 PID", object_schema({"group": group}, ["group"]), self.inspect)
        registry.register("resource.cgroup.create", "创建本框架的受管 cgroup", object_schema({"group": group}, ["group"]), self.create, mutates=True)
        registry.register("resource.cgroup.apply", "按预期旧值更新受管组权重并回读", object_schema(
            {"group": group, "expected_value": {"type": "integer", "minimum": 1, "maximum": 262144},
             "value": {"type": "integer", "minimum": 1, "maximum": 262144}}, ["group", "expected_value", "value"]), self.apply, mutates=True)
        registry.register("resource.cgroup.attach", "核对 PID 与启动时间后将进程迁入受管 cgroup", object_schema(
            {"group": group, "pid": pid, "expected_start_ticks": ticks}, ["group", "pid"]), self.attach, mutates=True)
        registry.register("resource.cgroup.detach", "将进程从受管 cgroup 迁回根控制器组", object_schema(
            {"group": group, "pid": pid}, ["group", "pid"]), self.detach, mutates=True)


class NetworkHookPlugin(Plugin):
    name = "network.ebpf_tc"
    PIN_DIR = Path("/sys/fs/bpf/oeagent_net")
    SOURCE_PATH = ROOT / "native/oe_net_hook.bpf.c"
    OBJECT_PATH = ROOT / "state/oe_net_hook.bpf.o"
    PROTECTED_PORTS = (80, 443, 18080, 18086, 18091)

    def describe(self):
        linux = platform.system() == "Linux"
        tools_ok = bool(linux and shutil.which("clang") and shutil.which("bpftool") and shutil.which("tc"))
        bpffs_ok = bool(linux and Path("/sys/fs/bpf").is_dir())
        return {"name": self.name, "available": bool(tools_ok and bpffs_ok and self.SOURCE_PATH.is_file()),
                "program_type": "sched_cls", "attach_point": "tc egress/ingress (lo)",
                "source": str(self.SOURCE_PATH.relative_to(ROOT).as_posix()),
                "object_built": self.OBJECT_PATH.is_file(),
                "active": (self.PIN_DIR / "prog").exists() if linux else False,
                "protected_ports": list(self.PROTECTED_PORTS),
                "capabilities": ["packet_byte_accounting", "target_port_monitoring", "target_port_drop_isolation",
                                 "protected_port_guard", "egress_ingress_direction"]}

    def _ensure_object(self):
        self.OBJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not self.OBJECT_PATH.is_file() or self.OBJECT_PATH.stat().st_mtime < self.SOURCE_PATH.stat().st_mtime:
            subprocess.run(["clang", "-O2", "-g", "-target", "bpf", "-c", str(self.SOURCE_PATH), "-o", str(self.OBJECT_PATH)],
                           check=True, capture_output=True, text=True)

    def _u32_hex(self, value):
        return [f"{b:02x}" for b in int(value).to_bytes(4, "little")]

    def status(self, arguments=None):
        del arguments
        if platform.system() != "Linux":
            return {"active": False, "reason": "non_linux"}
        prog = self.PIN_DIR / "prog"
        stats_pin = self.PIN_DIR / "oe_net_stats_map"
        cfg_pin = self.PIN_DIR / "oe_net_cfg_map"
        if not prog.exists() or not stats_pin.exists() or not cfg_pin.exists():
            return {"active": False, "interface": "lo", "config": None, "stats": None}
        stats_raw = json.loads(subprocess.check_output(
            ["bpftool", "-j", "map", "lookup", "pinned", str(stats_pin), "key", "hex", "00", "00", "00", "00"], text=True))
        cfg_raw = json.loads(subprocess.check_output(
            ["bpftool", "-j", "map", "lookup", "pinned", str(cfg_pin), "key", "hex", "00", "00", "00", "00"], text=True))
        return {"active": True, "interface": "lo",
                "config": cfg_raw.get("formatted", {}).get("value", {}),
                "stats": stats_raw.get("formatted", {}).get("value", {})}

    def attach(self, arguments):
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise ToolError("ROOT_REQUIRED", "挂载 eBPF 网络 hook 需要 Linux root。")
        interface = arguments.get("interface", "lo")
        if interface != "lo":
            raise ToolError("INVALID_INTERFACE", "当前演示仅允许在回环接口 lo 挂载 eBPF 策略。")
        target_port = arguments["target_port"]
        drop_target = bool(arguments.get("drop_target", False))
        direction = arguments.get("direction", "egress")
        if direction not in ("egress", "ingress", "both"):
            raise ToolError("INVALID_DIRECTION", "direction 必须是 egress、ingress 或 both。")
        if drop_target and target_port in self.PROTECTED_PORTS:
            raise ToolError("PROTECTED_PORT_REFUSED", f"端口 {target_port} 属于受保护业务白名单，拒绝下发丢包阻断策略。")
        try:
            self._ensure_object()
            if not (self.PIN_DIR / "prog").exists():
                shutil.rmtree(self.PIN_DIR, ignore_errors=True)
                self.PIN_DIR.mkdir(parents=True, exist_ok=True)
                subprocess.run(["bpftool", "prog", "load", str(self.OBJECT_PATH), str(self.PIN_DIR / "prog"),
                                "type", "classifier", "pinmaps", str(self.PIN_DIR)], check=True, capture_output=True, text=True)
                subprocess.run(["tc", "qdisc", "replace", "dev", interface, "clsact"], check=True, capture_output=True, text=True)
                dirs = ("egress", "ingress") if direction == "both" else (direction,)
                for d in dirs:
                    subprocess.run(["tc", "filter", "replace", "dev", interface, d, "prio", "49152", "handle", "1",
                                    "bpf", "da", "pinned", str(self.PIN_DIR / "prog")], check=True, capture_output=True, text=True)
            value_hex = self._u32_hex(target_port) + self._u32_hex(1 if drop_target else 0)
            subprocess.run(["bpftool", "map", "update", "pinned", str(self.PIN_DIR / "oe_net_cfg_map"),
                            "key", "hex", "00", "00", "00", "00", "value", "hex", *value_hex],
                           check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = getattr(exc, "stderr", None) or str(exc)
            raise ToolError("EBPF_NET_HOOK_FAILED", f"eBPF 网络 hook 配置失败：{stderr.strip()}") from exc
        return self.status()

    def detach(self, arguments=None):
        del arguments
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise ToolError("ROOT_REQUIRED", "卸载 eBPF 网络 hook 需要 Linux root。")
        before = self.status()
        for d in ("egress", "ingress"):
            subprocess.run(["tc", "filter", "del", "dev", "lo", d, "prio", "49152"], check=False, capture_output=True)
        subprocess.run(["tc", "qdisc", "del", "dev", "lo", "clsact"], check=False, capture_output=True)
        shutil.rmtree(self.PIN_DIR, ignore_errors=True)
        return {"detached": True, "before": before, "after": self.status()}

    def register(self, registry):
        registry.register("network.ebpf.describe", "报告 eBPF TC 网络观测与端口隔离插件能力", object_schema(), lambda args: self.describe())
        registry.register("network.ebpf.status", "读取已加载 eBPF 网络 hook 的配置与实时包/字节/丢包计数", object_schema(), self.status)
        registry.register("network.ebpf.attach", "加载或更新 lo 上的 eBPF TC 分类器（统计端口流量或执行端口级丢包隔离）", object_schema(
            {"interface": {"type": "string", "enum": ["lo"]},
             "direction": {"type": "string", "enum": ["egress", "ingress", "both"]},
             "target_port": {"type": "integer", "minimum": 1, "maximum": 65535},
             "drop_target": {"type": "boolean"}}, ["target_port"]), self.attach, mutates=True)
        registry.register("network.ebpf.detach", "卸载 lo 上的 eBPF TC 网络分类器并清理 bpffs 引脚", object_schema(), self.detach, mutates=True)


class SecurityHookPlugin(Plugin):
    name = "security.lsm"

    def _lsm_list(self):
        if platform.system() != "Linux":
            return []
        raw = (read("/sys/kernel/security/lsm") or "").strip()
        return [item.strip() for item in raw.split(",") if item.strip()]

    def describe(self):
        lsms = self._lsm_list()
        return {"name": self.name, "available": platform.system() == "Linux" and bool(lsms),
                "active_lsm_stack": lsms, "bpf_lsm_enabled": "bpf" in lsms,
                "capabilities": ["lsm_stack_discovery", "process_capability_and_confinement_audit",
                                 "unconfined_process_priority_and_cpu_confinement"]}

    def audit(self, arguments):
        if platform.system() != "Linux":
            raise ToolError("PLATFORM_UNSUPPORTED", "安全审计需要 Linux。")
        pid = arguments["pid"]
        status = read(f"/proc/{pid}/status")
        if status is None:
            raise ToolError("PROCESS_NOT_FOUND", "目标进程不存在或不可读。")
        fields = {}
        for key in ("Name", "Uid", "Gid", "NoNewPrivs", "Seccomp", "CapInh", "CapPrm", "CapEff", "CapBnd"):
            match = re.search(rf"^{key}:\s+(.+)$", status, re.M)
            if match:
                fields[key] = match.group(1).strip()
        attr_current = (read(f"/proc/{pid}/attr/current") or "").strip()
        cap_eff = int(fields.get("CapEff", "0"), 16)
        cap_sys_admin = bool(cap_eff & (1 << 21))
        cap_net_admin = bool(cap_eff & (1 << 12))
        return {"pid": pid, "comm": fields.get("Name"), "lsm_stack": self._lsm_list(),
                "lsm_label": attr_current or "unconfined",
                "no_new_privs": fields.get("NoNewPrivs") == "1",
                "seccomp_mode": int(fields["Seccomp"]) if fields.get("Seccomp", "").isdigit() else 0,
                "cap_eff_hex": fields.get("CapEff"),
                "has_cap_sys_admin": cap_sys_admin,
                "has_cap_net_admin": cap_net_admin,
                "cgroups": (read(f"/proc/{pid}/cgroup") or "").strip()}

    def enforce(self, arguments):
        if platform.system() != "Linux" or os.geteuid() != 0:
            raise ToolError("ROOT_REQUIRED", "执行安全隔离策略需要 Linux root。")
        pid = arguments["pid"]
        snap = process_snapshot(pid)
        if snap is None:
            raise ToolError("PROCESS_NOT_FOUND", "目标进程不存在或不可读。")
        expected_ticks = arguments.get("expected_start_ticks")
        if expected_ticks is not None and snap["start_ticks"] != expected_ticks:
            raise ToolError("STALE_PROCESS_IDENTITY", "目标 PID 启动时间不匹配，拒绝执行安全限制。")
        audit_before = self.audit({"pid": pid})
        nice_val = int(arguments.get("nice", 19))
        if not -20 <= nice_val <= 19:
            raise ToolError("INVALID_SECURITY_POLICY", "nice 值必须在 -20..19 之间。")
        iso_cpus = arguments.get("isolation_cpus")
        try:
            os.setpriority(os.PRIO_PROCESS, pid, nice_val)
            if iso_cpus is not None:
                os.sched_setaffinity(pid, set(iso_cpus))
        except OSError as exc:
            raise ToolError("SECURITY_ENFORCE_FAILED", f"执行安全隔离策略失败：{exc}") from exc
        snap_after = process_snapshot(pid)
        actual_nice = os.getpriority(os.PRIO_PROCESS, pid)
        return {"pid": pid, "comm": snap["comm"], "start_ticks": snap["start_ticks"],
                "enforced": True, "nice": actual_nice,
                "allowed_cpus_before": snap["allowed_cpus"],
                "allowed_cpus_after": snap_after["allowed_cpus"] if snap_after else None,
                "audit": audit_before}

    def register(self, registry):
        pid = {"type": "integer", "minimum": 1, "maximum": 4194304}
        ticks = {"type": "integer", "minimum": 1}
        registry.register("security.lsm.describe", "读取内核 LSM 模块栈与 BPF LSM 状态", object_schema(), lambda args: self.describe())
        registry.register("security.lsm.audit", "审计目标进程的 LSM 标签、Capability 掩码、Seccomp 与 cgroup 隔离上下文", object_schema(
            {"pid": pid}, ["pid"]), self.audit)
        registry.register("security.lsm.enforce", "核对进程身份与 LSM 上下文后执行安全降权与 CPU 亲和性隔离策略", object_schema(
            {"pid": pid, "expected_start_ticks": ticks,
             "nice": {"type": "integer", "minimum": -20, "maximum": 19},
             "isolation_cpus": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 1024}}},
            ["pid"]), self.enforce, mutates=True)


class PluginAutoConfig:
    def __init__(self, enabled=True, cgroup_enabled=True, latency_group="auto_latency",
                 batch_group="auto_batch", latency_shares=4096, batch_shares=1024,
                 ebpf_enabled=True, monitor_port=18095, drop_threshold_pps=20.0,
                 drop_hold_windows=3, lsm_audit_enabled=True, lsm_enforce_batch_nice=True,
                 batch_nice_value=15):
        for flag, val in (("enabled", enabled), ("cgroup_enabled", cgroup_enabled),
                          ("ebpf_enabled", ebpf_enabled), ("lsm_audit_enabled", lsm_audit_enabled),
                          ("lsm_enforce_batch_nice", lsm_enforce_batch_nice)):
            if not isinstance(val, bool):
                raise ToolError("INVALID_PLUGIN_CONFIG", f"{flag} 必须是布尔值。")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(latency_group)) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(batch_group)):
            raise ToolError("INVALID_PLUGIN_CONFIG", "受管 cgroup 组名格式无效。")
        if latency_group == batch_group:
            raise ToolError("INVALID_PLUGIN_CONFIG", "latency 与 batch 受管组不能相同。")
        for name, shares in (("latency_shares", latency_shares), ("batch_shares", batch_shares)):
            if not isinstance(shares, int) or isinstance(shares, bool) or not 2 <= shares <= 262144:
                raise ToolError("INVALID_PLUGIN_CONFIG", f"{name} 超出有效范围。")
        if not isinstance(monitor_port, int) or isinstance(monitor_port, bool) or not 1 <= monitor_port <= 65535:
            raise ToolError("INVALID_PLUGIN_CONFIG", "monitor_port 必须在 1..65535 之间。")
        if monitor_port in NetworkHookPlugin.PROTECTED_PORTS:
            raise ToolError("INVALID_PLUGIN_CONFIG", f"monitor_port {monitor_port} 属于受保护业务白名单端口，禁止设为熔断目标。")
        if not isinstance(drop_threshold_pps, (int, float)) or isinstance(drop_threshold_pps, bool) or drop_threshold_pps <= 0:
            raise ToolError("INVALID_PLUGIN_CONFIG", "drop_threshold_pps 必须为正数。")
        if not isinstance(drop_hold_windows, int) or isinstance(drop_hold_windows, bool) or not 1 <= drop_hold_windows <= 120:
            raise ToolError("INVALID_PLUGIN_CONFIG", "drop_hold_windows 超出有效范围。")
        if not isinstance(batch_nice_value, int) or isinstance(batch_nice_value, bool) or not 0 <= batch_nice_value <= 19:
            raise ToolError("INVALID_PLUGIN_CONFIG", "batch_nice_value 必须在 0..19 之间。")
        self.enabled = enabled
        self.cgroup_enabled = cgroup_enabled
        self.latency_group = latency_group
        self.batch_group = batch_group
        self.latency_shares = latency_shares
        self.batch_shares = batch_shares
        self.ebpf_enabled = ebpf_enabled
        self.monitor_port = monitor_port
        self.drop_threshold_pps = float(drop_threshold_pps)
        self.drop_hold_windows = drop_hold_windows
        self.lsm_audit_enabled = lsm_audit_enabled
        self.lsm_enforce_batch_nice = lsm_enforce_batch_nice
        self.batch_nice_value = batch_nice_value


class AutoPluginCoordinator:
    """Autonomous multi-resource controller for LSM security audit, Cgroup isolation, and eBPF TC port protection."""

    def __init__(self, config, log, dry_run=False, cgroup_plugin=None, net_plugin=None, lsm_plugin=None):
        self.config = config
        self.log = log
        self.dry_run = dry_run
        self.cgroup = cgroup_plugin or CgroupPlugin()
        self.ebpf = net_plugin or NetworkHookPlugin()
        self.lsm = lsm_plugin or SecurityHookPlugin()
        self.audited_identities = set()
        self.enforced_identities = set()
        self.cgroup_active = False
        self.attached_pids = {"latency": set(), "batch": set()}
        self.ebpf_attached = False
        self.ebpf_dropping = False
        self.ebpf_hold_remaining = 0
        self.prev_ebpf_packets = None
        self.prev_ebpf_mono = None
        self.last_observed_pps = 0.0

    def step(self, observation, current_policy_id):
        if not self.config.enabled:
            return {"actions": []}
        actions = []
        tasks = observation.get("tasks", [])
        roles_present = {role for task in tasks for role in task.get("roles", [])}
        is_mixed = {"latency", "batch"} <= roles_present
        has_latency = "latency" in roles_present
        now_mono = observation.get("ended_monotonic") or time.monotonic()

        # 1. Automatic LSM Security Audit for newly observed processes
        if self.config.lsm_audit_enabled:
            for task in tasks:
                pid = task["pid"]
                start_ticks = (task.get("identity") or {}).get("start_ticks")
                ident = (pid, start_ticks)
                if ident not in self.audited_identities:
                    if self.dry_run:
                        audit_info = {"pid": pid, "comm": task.get("comm"), "lsm_label": "simulated", "simulated": True}
                    else:
                        try:
                            audit_info = self.lsm.audit({"pid": pid})
                        except ToolError as exc:
                            audit_info = {"pid": pid, "error": exc.code, "message": str(exc)}
                    self.audited_identities.add(ident)
                    event = {"event": "plugin_security_audited", "pid": pid, "start_ticks": start_ticks,
                             "roles": task.get("roles", []), "audit": audit_info}
                    self.log(event)
                    actions.append(event)
                    if self.config.lsm_enforce_batch_nice and "batch" in task.get("roles", []) and ident not in self.enforced_identities:
                        if self.dry_run:
                            enforce_info = {"pid": pid, "enforced": True, "nice": self.config.batch_nice_value, "simulated": True}
                        else:
                            try:
                                enforce_info = self.lsm.enforce({"pid": pid, "expected_start_ticks": start_ticks,
                                                                 "nice": self.config.batch_nice_value})
                            except ToolError as exc:
                                enforce_info = {"pid": pid, "enforced": False, "error": exc.code, "message": str(exc)}
                        self.enforced_identities.add(ident)
                        enf_event = {"event": "plugin_security_enforced", "pid": pid, "start_ticks": start_ticks,
                                     "roles": task.get("roles", []), "enforcement": enforce_info}
                        self.log(enf_event)
                        actions.append(enf_event)

        # 2. Automatic Cgroup Weight Isolation & Task Migration
        if self.config.cgroup_enabled:
            if is_mixed:
                latency_tasks = [t for t in tasks if "latency" in t.get("roles", [])]
                batch_tasks = [t for t in tasks if "batch" in t.get("roles", [])]
                need_attach = (not self.cgroup_active
                               or any(t["pid"] not in self.attached_pids["latency"] for t in latency_tasks)
                               or any(t["pid"] not in self.attached_pids["batch"] for t in batch_tasks))
                if need_attach and (self.dry_run or (current_policy_id == "kernel.default" and sched_state() != "enabled")):
                    try:
                        if not self.dry_run:
                            for grp, shares in ((self.config.latency_group, self.config.latency_shares),
                                                (self.config.batch_group, self.config.batch_shares)):
                                info = self.cgroup.create({"group": grp})
                                if info["value"] != shares:
                                    self.cgroup.apply({"group": grp, "expected_value": info["value"], "value": shares})
                            for t in latency_tasks:
                                if t["pid"] not in self.attached_pids["latency"]:
                                    self.cgroup.attach({"group": self.config.latency_group, "pid": t["pid"],
                                                        "expected_start_ticks": (t.get("identity") or {}).get("start_ticks")})
                                    self.attached_pids["latency"].add(t["pid"])
                            for t in batch_tasks:
                                if t["pid"] not in self.attached_pids["batch"]:
                                    self.cgroup.attach({"group": self.config.batch_group, "pid": t["pid"],
                                                        "expected_start_ticks": (t.get("identity") or {}).get("start_ticks")})
                                    self.attached_pids["batch"].add(t["pid"])
                        else:
                            for t in latency_tasks:
                                self.attached_pids["latency"].add(t["pid"])
                            for t in batch_tasks:
                                self.attached_pids["batch"].add(t["pid"])
                        self.cgroup_active = True
                        event = {"event": "plugin_cgroup_isolated", "reason": "mixed_contention_detected",
                                 "latency_group": self.config.latency_group, "batch_group": self.config.batch_group,
                                 "latency_shares": self.config.latency_shares, "batch_shares": self.config.batch_shares,
                                 "latency_pids": sorted(self.attached_pids["latency"]),
                                 "batch_pids": sorted(self.attached_pids["batch"]), "dry_run": self.dry_run}
                        self.log(event)
                        actions.append(event)
                    except ToolError as exc:
                        self.log({"event": "plugin_cgroup_error", "code": exc.code, "message": str(exc)})
            elif self.cgroup_active and (self.dry_run or (current_policy_id == "kernel.default" and sched_state() != "enabled")):
                if not self.dry_run:
                    for role, grp in (("latency", self.config.latency_group), ("batch", self.config.batch_group)):
                        for pid in list(self.attached_pids[role]):
                            try:
                                self.cgroup.detach({"group": grp, "pid": pid})
                            except ToolError:
                                pass
                released_lat = sorted(self.attached_pids["latency"])
                released_bat = sorted(self.attached_pids["batch"])
                self.attached_pids["latency"].clear()
                self.attached_pids["batch"].clear()
                self.cgroup_active = False
                event = {"event": "plugin_cgroup_released", "reason": "mixed_contention_cleared",
                         "latency_group": self.config.latency_group, "batch_group": self.config.batch_group,
                         "released_latency_pids": released_lat, "released_batch_pids": released_bat,
                         "dry_run": self.dry_run}
                self.log(event)
                actions.append(event)

        # 3. Automatic eBPF TC Network Monitoring & Port Anomaly Isolation
        if self.config.ebpf_enabled:
            if not self.ebpf_attached and has_latency:
                try:
                    if not self.dry_run:
                        st = self.ebpf.attach({"interface": "lo", "target_port": self.config.monitor_port, "drop_target": False})
                        pkts = int((st.get("stats") or {}).get("target_packets", 0))
                    else:
                        pkts = int((observation.get("ebpf_stats") or {}).get("target_packets", 0))
                    self.ebpf_attached = True
                    self.ebpf_dropping = False
                    self.prev_ebpf_packets = pkts
                    self.prev_ebpf_mono = now_mono
                    event = {"event": "plugin_ebpf_attached", "mode": "observe", "interface": "lo",
                             "target_port": self.config.monitor_port, "drop_target": False, "dry_run": self.dry_run}
                    self.log(event)
                    actions.append(event)
                except ToolError as exc:
                    self.log({"event": "plugin_ebpf_error", "code": exc.code, "message": str(exc)})
            elif self.ebpf_attached:
                try:
                    if not self.dry_run:
                        st = self.ebpf.status()
                        stats = st.get("stats") or {}
                    else:
                        stats = observation.get("ebpf_stats") or {"target_packets": self.prev_ebpf_packets or 0, "dropped_packets": 0}
                    cur_packets = int(stats.get("target_packets", 0))
                    elapsed = max(0.2, now_mono - (self.prev_ebpf_mono if self.prev_ebpf_mono is not None else (now_mono - 1.0)))
                    delta_packets = max(0, cur_packets - (self.prev_ebpf_packets or 0))
                    observed_pps = delta_packets / elapsed
                    self.prev_ebpf_packets = cur_packets
                    self.prev_ebpf_mono = now_mono
                    self.last_observed_pps = round(observed_pps, 2)

                    if not self.ebpf_dropping and observed_pps >= self.config.drop_threshold_pps:
                        if not self.dry_run:
                            st = self.ebpf.attach({"interface": "lo", "target_port": self.config.monitor_port, "drop_target": True})
                            stats = st.get("stats") or stats
                        self.ebpf_dropping = True
                        self.ebpf_hold_remaining = self.config.drop_hold_windows
                        event = {"event": "plugin_ebpf_isolated", "reason": "target_port_rate_exceeded",
                                 "target_port": self.config.monitor_port, "observed_pps": round(observed_pps, 2),
                                 "threshold_pps": self.config.drop_threshold_pps, "drop_target": True,
                                 "stats": stats, "dry_run": self.dry_run}
                        self.log(event)
                        actions.append(event)
                    elif self.ebpf_dropping:
                        self.ebpf_hold_remaining = max(0, self.ebpf_hold_remaining - 1)
                        if self.ebpf_hold_remaining <= 0 and observed_pps < self.config.drop_threshold_pps:
                            if not self.dry_run:
                                st = self.ebpf.attach({"interface": "lo", "target_port": self.config.monitor_port, "drop_target": False})
                                stats = st.get("stats") or stats
                            self.ebpf_dropping = False
                            event = {"event": "plugin_ebpf_recovered", "reason": "cooldown_elapsed_rate_normal",
                                     "target_port": self.config.monitor_port, "observed_pps": round(observed_pps, 2),
                                     "threshold_pps": self.config.drop_threshold_pps, "drop_target": False,
                                     "stats": stats, "dry_run": self.dry_run}
                            self.log(event)
                            actions.append(event)
                except ToolError as exc:
                    self.log({"event": "plugin_ebpf_error", "code": exc.code, "message": str(exc)})
        return {"actions": actions}

    def status(self):
        return {
            "enabled": self.config.enabled,
            "lsm_audited_count": len(self.audited_identities),
            "lsm_enforced_count": len(self.enforced_identities),
            "cgroup": {
                "enabled": self.config.cgroup_enabled,
                "active": self.cgroup_active,
                "latency_group": self.config.latency_group,
                "batch_group": self.config.batch_group,
                "latency_shares": self.config.latency_shares,
                "batch_shares": self.config.batch_shares,
                "latency_pids": sorted(self.attached_pids["latency"]),
                "batch_pids": sorted(self.attached_pids["batch"]),
            },
            "ebpf": {
                "enabled": self.config.ebpf_enabled,
                "attached": self.ebpf_attached,
                "target_port": self.config.monitor_port,
                "drop_target": self.ebpf_dropping,
                "threshold_pps": self.config.drop_threshold_pps,
                "last_observed_pps": self.last_observed_pps,
                "hold_remaining": self.ebpf_hold_remaining,
            },
        }

    def close(self):
        if not self.config.enabled:
            return
        if not self.dry_run:
            if self.cgroup_active:
                for role, grp in (("latency", self.config.latency_group), ("batch", self.config.batch_group)):
                    for pid in list(self.attached_pids[role]):
                        try:
                            self.cgroup.detach({"group": grp, "pid": pid})
                        except Exception:
                            pass
            if self.ebpf_attached:
                try:
                    self.ebpf.detach()
                except Exception:
                    pass
        self.attached_pids["latency"].clear()
        self.attached_pids["batch"].clear()
        self.cgroup_active = False
        self.ebpf_attached = False
        self.ebpf_dropping = False
        self.log({"event": "plugin_cleanup_completed", "dry_run": self.dry_run})

