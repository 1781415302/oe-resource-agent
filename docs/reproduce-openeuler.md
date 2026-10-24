# openEuler 6.6.0-scx 环境复现与实验验证指南

## 1. 已验证环境

| 项目 | 实际记录 |
|---|---|
| 操作系统 | openEuler 24.03 LTS-SP4 (x86_64) |
| 发行版原始内核 | 6.6.0-159.4.3.154.oe2403sp4.x86_64 |
| 自建内核运行名 | 6.6.0-scx (包含 `sched_ext` 与 BPF 支持) |
| 架构 | x86_64 |
| 虚拟化平台 | Windows 11 + Hyper-V（来宾 `systemd-detect-virt` 为 `microsoft`，4 个 vCPU） |
| 宿主 CPU | Intel(R) Core(TM) i7-14650HX（16 核 24 线程，宿主电源方案 High performance） |
| 编译器 | GCC 12.3.1，Clang 17.0.6 (BPF 目标支持) |
| Python 环境 | Python 3.11.6；代码兼容 3.9+ 语法 |
| cgroup 支持 | Cgroup v1，CPU/CPUacct 联合挂载及相关控制器完整支持 |
| BTF 支持 | `/sys/kernel/btf/vmlinux` 存在且可用（`CONFIG_DEBUG_INFO_BTF=y`） |
| 时钟中断频率 | `CONFIG_HZ=1000` |
| 调度统计支持 | `kernel.sched_schedstats = 1`（采集真实 `/proc/<pid>/schedstat` 排队等待时间） |
| 默认调度器 | EEVDF（`sched_base_slice_ns = 2,100,000 ns`） |

`repro/kernel.config` 是实际运行内核的完整配置，包含 `CONFIG_SCHED_CLASS_EXT=y`、`CONFIG_BPF_SYSCALL=y`、`CONFIG_BPF_JIT=y`、`CONFIG_DEBUG_INFO_BTF=y` 与 `LOCALVERSION="-scx"`。配置中的构建签名材料在本机准备，不在项目代码库内分发私钥。

## 2. 先准备能启动的内核

使用 openEuler 24.03 LTS-SP4 ISO 安装虚拟机（分配 4 个 vCPU、8GB 内存）。检查 `uname -r`、内核配置、BTF 与 `/sys/kernel/sched_ext/state`。官方出厂内核默认未启用 `sched_ext`；需以对应 SP4 源码 RPM 准备树启用该配置、构建并安装独立的 `-scx` 内核，再从该内核启动。

发行版补丁与 SCX ABI 必须与目标运行内核匹配。准备树路径位于 `/root/rpmbuild/BUILD/kernel-6.6.0/linux-6.6.0-159.4.3.154.x86_64`。构建前需确认：
```bash
uname -r                          # 应输出 6.6.0-scx
cat /sys/kernel/sched_ext/state   # 应输出 disabled（就绪态）
test -r /sys/kernel/btf/vmlinux && echo "BTF ready"
sysctl -w kernel.sched_schedstats=1
```

## 3. 构建框架

构建依赖为 Python 3、GNU make、GCC、Clang BPF、elfutils-libelf 开发包、zlib 开发包。工具链复用内核 `tools/sched_ext` 构建的 libbpf 与 bpftool。无需 Rust、第三方 Python 依赖或在线大模型服务。

```bash
cd /root/oe-resource-agent
bash build.sh
./agent capabilities
./agent tools list
```

构建必须在无其他调度实验进行、`sched_ext` 处于 `disabled` 状态时执行。构建脚本会自动编译 `scx_oe_adaptive` 调度器及 `native/oe_net_hook.bpf.c` 网络钩子，校验内核 SCX 接口，并将二进制、BTF 和源码哈希写入 `state/build.json`。

## 4. 验证顺序

复现验证应按以下流水线依次执行：

1. **离线确定性单元测试（103 项全部通过）**：
   ```bash
   python3 -m unittest discover -s tests -v
   ```
2. **各子系统独立与真机闭环验证（`experiments/check_*.py`）**：
   ```bash
   python3 experiments/check_native.py            # BPF 加载、Map 热更、边界与租约看门狗回退
   python3 experiments/check_workload_loop.py      # 负载感知与调度策略路由
   python3 experiments/check_control_session.py    # 持久 JSON 控制会话与工具调用
   python3 experiments/check_cgroup_plugin.py      # Cgroup v1 4096:1024 份额隔离实测
   python3 experiments/check_ebpf_hooks.py         # eBPF TC 端口拦截丢弃与 LSM 栈审计
   python3 experiments/check_slo_continuous.py     # 阶段二连续窗口业务 SLO 反馈闭环
   python3 experiments/check_multi_resource_auto.py # 阶段三多维资源协同自治闭环（5阶段全自动）
   ```
3. **开题 6 策略 × 4 场景严谨多周期基准测试套件**：
   ```bash
   python3 experiments/run_rigorous_suite.py
   ```
   执行 5 周期拉丁方平衡旋转矩阵测试、全量强度梯度覆盖与多资源定向消融，生成包含 `Mean ± SD` 统计指标的原始度量数据（`state/rigorous-suite-validation.json`）与自包含 HTML 实验报告（`docs/experiment-report.html`）。
4. **报告与实验数据全量一致性自动化审计**：
   ```bash
   python3 tools/check_report_data_consistency.py
   ```
   自动化核对总结报告、README 及 HTML 报告中的每一项指标与 `state/*.json` 的原始实测数据完全一致。

## 5. 绑核规划与性能波动说明

### 5.1 物理核心与 vCPU 绑定方案
为完全隔离系统抖动，保证微秒级调度与网络测量的确定性，系统规划 4 个 vCPU 分工如下：
- **CPU0（业务核心）**：运行前台受管业务 `nginx`（worker 绑核 CPU0）与同核后台争抢源 `sysbench`（线程绑核 CPU0），形成确定性单核争用；
- **CPU1（压测核心）**：运行客户端压测工具 `wrk2`，避免发压开销与被测服务争抢算力；
- **CPU2（控制核心）**：运行 Agent 自治运行时、度量采集与 BPF 控制器；
- **CPU3（隔离核心）**：用于将 `security.policy.enforce` 判定的非授权/高竞争进程驱逐流放，保障 CPU0 纯净。

### 5.2 性能波动与统计置信度说明
在虚拟化与高并发实测环境下，单次测量的尾延迟（p99）受内核中断、主机调度分配及网络协议栈偶发处理的影响存在毫秒级波动。因此：
- **禁止依赖单次测试判定调度性能**：所有稳态对照组均基于 **5 周期独立运行的样本统计（N=5，Mean ± SD）** 进行严谨评估；
- 动态场景（`dynamic`）记录单次 45 秒时序过渡实况，切入耗时与回退耗时为特定瞬态事件触发时延；
- 完整的统计假设检验（如 Welch's t-test）与显著性分析均已固化在报告与测试套件中。
