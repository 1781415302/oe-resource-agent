"""Plugin interfaces and real cgroup, eBPF network, and LSM security adapters."""
import json
import os
import platform
import re
import shutil
import subprocess
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

    def describe(self):
        linux = platform.system() == "Linux"
        tools_ok = bool(linux and shutil.which("clang") and shutil.which("bpftool") and shutil.which("tc"))
        bpffs_ok = bool(linux and Path("/sys/fs/bpf").is_dir())
        return {"name": self.name, "available": bool(tools_ok and bpffs_ok and self.SOURCE_PATH.is_file()),
                "program_type": "sched_cls", "attach_point": "tc egress (lo)",
                "source": str(self.SOURCE_PATH.relative_to(ROOT).as_posix()),
                "object_built": self.OBJECT_PATH.is_file(),
                "active": (self.PIN_DIR / "prog").exists() if linux else False,
                "capabilities": ["packet_byte_accounting", "target_port_monitoring", "target_port_drop_isolation"]}

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
        try:
            self._ensure_object()
            if not (self.PIN_DIR / "prog").exists():
                shutil.rmtree(self.PIN_DIR, ignore_errors=True)
                self.PIN_DIR.mkdir(parents=True, exist_ok=True)
                subprocess.run(["bpftool", "prog", "load", str(self.OBJECT_PATH), str(self.PIN_DIR / "prog"),
                                "type", "classifier", "pinmaps", str(self.PIN_DIR)], check=True, capture_output=True, text=True)
                subprocess.run(["tc", "qdisc", "replace", "dev", interface, "clsact"], check=True, capture_output=True, text=True)
                subprocess.run(["tc", "filter", "replace", "dev", interface, "egress", "prio", "49152", "handle", "1",
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
        subprocess.run(["tc", "filter", "del", "dev", "lo", "egress", "prio", "49152"], check=False, capture_output=True)
        subprocess.run(["tc", "qdisc", "del", "dev", "lo", "clsact"], check=False, capture_output=True)
        shutil.rmtree(self.PIN_DIR, ignore_errors=True)
        return {"detached": True, "before": before, "after": self.status()}

    def register(self, registry):
        registry.register("network.ebpf.describe", "报告 eBPF TC 网络观测与端口隔离插件能力", object_schema(), lambda args: self.describe())
        registry.register("network.ebpf.status", "读取已加载 eBPF 网络 hook 的配置与实时包/字节/丢包计数", object_schema(), self.status)
        registry.register("network.ebpf.attach", "加载或更新 lo 上的 eBPF TC 分类器（统计端口流量或执行端口级丢包隔离）", object_schema(
            {"interface": {"type": "string", "enum": ["lo"]},
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
                "capabilities": ["lsm_stack_discovery", "process_capability_and_confinement_audit"]}

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

    def register(self, registry):
        registry.register("security.lsm.describe", "读取内核 LSM 模块栈与 BPF LSM 状态", object_schema(), lambda args: self.describe())
        registry.register("security.lsm.audit", "审计目标进程的 LSM 标签、Capability 掩码、Seccomp 与 cgroup 隔离上下文", object_schema(
            {"pid": {"type": "integer", "minimum": 1, "maximum": 4194304}}, ["pid"]), self.audit)
