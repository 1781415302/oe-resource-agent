"""Build the native backend against the exact running openEuler kernel tree."""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TREE = Path("/root/rpmbuild/BUILD/kernel-6.6.0/linux-6.6.0-159.4.3.154.x86_64")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise SystemExit("原生后端构建需要目标 openEuler 的 root 终端。")
    tree = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TREE
    tree = tree.resolve(strict=True)
    sched = tree / "tools/sched_ext"
    if Path("/sys/kernel/sched_ext/state").read_text().strip() != "disabled":
        raise SystemExit("请在当前实验结束、sched_ext disabled 后构建。")
    release = subprocess.check_output(["make", "-s", "-C", str(tree), "kernelrelease"], text=True).strip()
    if release != platform.release():
        raise SystemExit("源码 kernelrelease 与运行内核不同。")
    compat = (sched / "include/scx/compat.h").read_text()
    enums = (sched / "include/scx/enums.h").read_text()
    enum_values = (sched / "include/scx/enums.autogen.h").read_text()
    if "SCX_ENUM_INIT(__skel)" not in compat or "rodata->__##name" not in enums or "SCX_SLICE_DFL" not in enum_values:
        raise SystemExit("SCX 兼容枚举接口与已核对的 openEuler 后端不同。")
    state = ROOT / "state"
    state.mkdir(exist_ok=True)
    installed = state / "installed-sources.json"
    previous = json.loads(installed.read_text()) if installed.is_file() else {}
    sources = ["scx_oe_adaptive.c", "scx_oe_adaptive.bpf.c", "oe_adaptive.h"]
    for name in sources:
        target, source = sched / name, ROOT / "native" / name
        if target.is_symlink():
            raise SystemExit(f"目标是符号链接：{target}")
        if target.exists() and digest(target) != digest(source):
            expected = previous.get("sha256", {}).get(name) if previous.get("kernel_tree") == str(tree) else None
            if digest(target) != expected:
                raise SystemExit(f"目标含未识别修改，请核对：{target}")
            backup = state / "backups" / digest(target) / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
    for name in sources:
        shutil.copyfile(ROOT / "native" / name, sched / name)
    evidence = {"kernel_tree": str(tree), "sha256": {name: digest(ROOT / "native" / name) for name in sources}}
    installed.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["make", "-C", str(sched), "-j2", "c-sched-targets=scx_oe_adaptive",
                    "VMLINUX_BTF=/sys/kernel/btf/vmlinux", "scx_oe_adaptive"], check=True)
    binary = sched / "build/bin/scx_oe_adaptive"
    description = subprocess.run([str(binary), "--describe"], capture_output=True, text=True, check=True)
    config_lines = [line for line in description.stdout.splitlines() if line.startswith("OE_ADAPTIVE_CONFIG ")]
    if len(config_lines) != 1:
        raise SystemExit("后端构建后配置读取失败。")
    config = json.loads(config_lines[0].split(" ", 1)[1])
    if config["version"] != "0.2.0" or config["max_slice_ns"] <= 0:
        raise SystemExit("后端版本或内核默认值不符。")
    manifest = {"version": "0.2.0", "kernel_release": release, "architecture": platform.machine(),
                "binary": str(binary), "binary_sha256": digest(binary), "configuration": config,
                "btf_sha256": digest("/sys/kernel/btf/vmlinux"), "source": evidence,
                "native_load_verified": False, "performance_verified": False}
    net_src = ROOT / "native/oe_net_hook.bpf.c"
    if net_src.is_file() and shutil.which("clang"):
        net_obj = state / "oe_net_hook.bpf.o"
        subprocess.run(["clang", "-O2", "-g", "-target", "bpf", "-c", str(net_src), "-o", str(net_obj)], check=True)
        manifest["net_hook_object"] = str(net_obj)
        manifest["net_hook_sha256"] = digest(net_obj)
    subprocess.run(["sysctl", "-w", "kernel.sched_schedstats=1"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (state / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"原生后端构建完成：{binary}")
    print(f"内核默认上限：{config['max_slice_ns'] / 1000:g}us；实际加载与在线更新待验证。")


if __name__ == "__main__":
    main()
