"""Read-only capability discovery; unsupported features are explicit."""
import json
import os
import platform
from pathlib import Path
from . import __version__

from .backend import ROOT, sched_state
from .observe import read


def discover():
    linux = platform.system() == "Linux"
    cgroup_v2 = Path("/sys/fs/cgroup/cgroup.controllers").is_file() if linux else False
    build = ROOT / "state/build.json"
    lsm_raw = (read("/sys/kernel/security/lsm") or "").strip() if linux else ""
    lsms = [item.strip() for item in lsm_raw.split(",") if item.strip()]
    return {"schema_version": 1, "agent_version": __version__, "python_cache_mode": "checked_hash",
            "system": platform.system(), "kernel_release": platform.release(),
            "architecture": platform.machine(), "root": bool(linux and os.geteuid() == 0),
            "linux": linux, "sched_ext": sched_state() if linux else "unavailable",
            "btf": bool(linux and Path("/sys/kernel/btf/vmlinux").is_file()),
            "cgroup_version": 2 if cgroup_v2 else 1 if linux and Path("/sys/fs/cgroup").is_dir() else None,
            "psi_cpu": bool(linux and Path("/proc/pressure/cpu").is_file()),
            "schedstats_enabled": (read("/proc/sys/kernel/sched_schedstats") or "").strip() == "1" if linux else False,
            "backend_built": build.is_file(),
            "backend": json.loads(build.read_text()) if build.is_file() else None,
            "available_plugins": ["cpu.sched_ext", "resource.cgroup", "network.ebpf_tc", "security.lsm"],
            "extension_hooks": {
                "network": "ebpf_tc_sched_cls" if (ROOT / "native/oe_net_hook.bpf.c").is_file() else "interface_only",
                "security": "lsm_capability_audit" if ("bpf" in lsms or not linux) else "interface_only"
            }}
