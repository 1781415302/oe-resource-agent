# 阶段一开发设计：Hyper-V 性能基准与连续窗口测量

适用对象：负责拆分任务和编排 workflow 的主 Agent，以及各独立上下文的 Coding Agent。

当前执行入口为 [阶段一默认环境测量续跑](stage-1-baseline-followup.md)。下一轮只通过现有配置将宿主 CPU 上限设为 100%，不再按占用高低筛选；保留完整采样、数据和清理校验，沿用现有 CLI 与 schema_version=1。实际配置尚待执行者修改。

本设计只覆盖阶段一。交付可信测量链路、默认调度负载定档和一次完整小规模对照，不修改业务决策算法，不承担最终比赛性能验收。执行前向每个子 Agent 提供本文第 1、4、7、9 节，以及其负责文件和对应 Stage；不能只转发步骤标题。

## 1. 整体设计理念

### 1.1 目标

在当前 Windows/Hyper-V/openEuler 环境中回答三个问题：测量是否完整且可复算；默认调度在当前负载下有哪些可用请求速率；同一测量流程能否观测固定策略和现有 Agent 的实际行为。

阶段出口是可复用的采集实现、冻结的环境及负载配置、原始记录和当前宿主环境下的测量报告。新的 survey 和 pilot 均独立审计通过后完成阶段一测量验收；一次 pilot 不产生稳定收益结论，不以 p99 改善为本阶段通过条件。

### 1.2 明确决策

- 保留既有实验入口，新建阶段一入口；复用现有资源锁、进程管理、工作负载准备、采样和运行时。
- 给锁定版本 wrk2 增加可选的连续窗口导出，仅用于单线程、非流水线 HTTP 客户端。独立记录窗口 HDR，不重置或替换上游主直方图，不改发压节奏、追赶算法、超时及延迟定义。
- survey 使用默认调度；pilot 比较 `default`、`fixed-1000`、`adaptive`，其中 adaptive 明确是 `nginx-cpu` 行为规则 Agent。阶段一不提交 SLO 业务窗口、不改 `nginx-slo`。
- Windows 负责宿主采样、SSH 编排、质量判定和结果下载；Linux 负责实际业务、SCX、客户端和原始记录；纯 Python 测量模块负责跨端解析与复算。
- 所有实验使用同一测量版 wrk2，原始数据按尝试保存。性能不佳与执行失败、测量损坏、宿主质量失败分别记录。

扩展通过新增实验配置和消费连续窗口实现，不向 Agent 运行时加入实验专用状态。本阶段的记录可以供后续 SLO 采集适配使用，但不把该适配提前实现。

### 1.3 环境与现有证据

已在本次会话确认：来宾为 Hyper-V（`systemd-detect-virt` 返回 `microsoft`），openEuler 24.03 LTS-SP4、x86_64、6.6.0-scx、4 vCPU、Python 3.11.6。54 项确定性测试、原生 8 项、普通/SLO 控制会话、真实负载自动切换及唤醒抢占检查通过。已有结果可以复用，不为开始本阶段再次全量运行这些负载检查。

实际工作区是三个并列目录：Windows 为 `Z:/oe-resource-agent`、`Z:/euler-bench`、`Z:/research`；来宾为 `/root/oe-resource-agent`、`/root/euler-bench`、`/root/research`。Z 盘是 `\\172.26.1.3\root` 的映射。本机可通过 SSH 默认身份连接 `root@172.26.1.3`；执行时重新探测地址，不将地址或私钥写进源码。

源码核对依据：

| 现有实现 | 已确认的设计约束 |
|---|---|
| `tools/prepare_wrk2.py:instrument/main` | 固定上游提交和归档摘要；已有导出只覆盖校准成功到事件循环结束 |
| `experiments/run_fixed_rate.py:episode` | 每个窗口重启客户端；检查窗口内策略 revision 不变，不适合直接测跨策略切换 |
| `experiments/run_dynamic.py:episode` | 使用闭环 wrk，并在各窗口重启客户端 |
| `oeagent/runtime.py:AgentRuntime.run` | default/fixed/adaptive 已有独立模式，可共享 observer；不必修改运行时 |
| `oeagent/histogram.py:percentile` | 使用 wrk 报告分布的秩公式；新 HDR 窗口不能直接沿用该公式 |
| `eulerbench.metrics.BackgroundReader` | EPS 时间原点根据输出接收时间推断；只能宣称近似对齐 |
| `research/run_host_controlled.py` | 已有宿主门槛、保留尝试和所属实例中断思路，但默认路径、策略组及分析结构绑定旧实验 |

## 2. 系统架构设计

```text
Windows run_baseline.py
  ├─ collect_host_cpu.ps1 → host-cpu.jsonl（Windows 单调时钟）
  ├─ SSH/SCP → Linux experiments/run_baseline.py
  │             ├─ ResourceLock / ProcessManager / AgentRuntime
  │             ├─ nginx + sysbench + windowed wrk2
  │             └─ 客户端日志、任务/策略事件、资源采样、summary、归档
  ├─ oeagent.measurement → 解析、质量复算、速率选择
  └─ analyze_baseline.py → audit.json / CSV / analysis.html
```

边界：

- `measurement.py` 是纯计算和文件解析模块，允许读取传入目录，禁止启动进程、访问 procfs、通过 SSH 控制资源或写报告。
- 来宾驱动只执行有界工作负载、记录实际状态和清理；不得根据性能数字重试整个周期，也不作宿主质量判断。
- 宿主驱动拥有尝试预算、质量门槛和下载流程，不根据来宾墙上时钟切割宿主样本。
- 分析器从原始记录复算，不信任保存的 `valid=true` 或衍生百分位；可复用解析模块，不调用实验执行函数。
- 现有 `policy.py`、`feedback.py`、`runtime.py`、SCX C/eBPF 源码和 `euler-bench` 源码全部保持不变。阶段一测量允许同一窗口跨策略；标记实际重叠，不套用旧 SLO 的同 revision 窗口约束。

## 3. 文件级设计

下表是实现文件白名单；F14 是本设计文档本身，不属于执行时实现改动。

| ID | 新增/修改及路径 | 职责与必要性 | 对外接口/关系 |
|---|---|---|---|
| F01 | 修改 `Z:/oe-resource-agent/tools/prepare_wrk2.py` | 保留旧构建，新增窗口导出构建；补丁仍由固定归档生成，禁止编辑 cache 作为源码 | 保持 `instrument(header, source)`；新增 `instrument_windows(header, source)` 和 `--windowed` CLI |
| F02 | 新增 `Z:/oe-resource-agent/oeagent/measurement.py` | 统一数据契约、客户端解析、HDR 统计、试验审计、负载定档；避免两端各解释一套数据 | 第 4 节所有共享类型与纯函数 |
| F03 | 修改 `Z:/oe-resource-agent/oeagent/quality.py` | 给既有宿主门槛增加单调时间字段支持，兼容旧 UTC 调用 | `host_gate(..., *, time_key="utc")` |
| F04 | 新增 `Z:/oe-resource-agent/experiments/baseline-profile.json` | 冻结阶段一负载、超时、定档规则和宿主门槛 | `BaselineProfile`；唯一预声明配置 |
| F05 | 新增 `Z:/oe-resource-agent/experiments/run_baseline.py` | 来宾环境探测、survey/pilot 执行、所属实例停止和归档 | `inspect_guest`、`execute_request`、`stop_owned`、CLI |
| F06 | 新增 `Z:/research/run_baseline.py` | Windows 编排、采样、preflight、SSH/SCP、尝试预算与状态 | `run_study`、CLI；消费 F02/F03/F05/F07/F08 |
| F07 | 修改 `Z:/research/collect_host_cpu.ps1` | 保留旧字段和参数，增加跨 Windows 进程可比较的单调时间；Hyper-V 诊断允许无权限 | 原参数不变；增加 HostSample 字段 |
| F08 | 新增 `Z:/research/analyze_baseline.py` | 独立复算、完整尝试展示、CSV/HTML；不改旧分析器 | `audit_study`、`write_report`、CLI |
| F09 | 新增 `Z:/oe-resource-agent/tests/test_measurement.py` | 流记录、HDR 秩、计数/时间不变量、定档和新宿主时间字段测试 | unittest，可在 Windows/Linux 无负载执行 |
| F10 | 新增 `Z:/oe-resource-agent/tests/test_baseline_guest.py` | 来宾调度、状态机、假进程清理及流边界测试 | unittest；操作系统动作 mock，不实际加载 SCX |
| F11 | 新增 `Z:/oe-resource-agent/tests/test_baseline_host.py` | 宿主重试、下载、collector 退出、独立审计和参数契约测试 | unittest；SSH/collector/文件系统按测试需要隔离 |
| F12 | 新增 `Z:/oe-resource-agent/experiments/check_wrk2_windows.py` | 一个实际客户端最小用例验证补丁、累计与窗口分布一致 | CLI：`--duration-seconds`；默认只用默认调度、低速率 |
| F13 | 新增 `Z:/oe-resource-agent/docs/baseline-measurement.md` | Windows/Linux 执行步骤、结果语义、失败恢复和阶段一结论入口 | 文档；不得把一次 pilot 写成正式性能收益 |
| F14 | 新增 `Z:/oe-resource-agent/docs/stage-1-development-plan.md` | 本实施设计与 workflow 契约 | 只读输入 |

运行数据目录 `state/`、`cache/`、`results/` 和显式传入的 Windows `--output-root` 不属于源码白名单；允许按第 6 节生成记录。禁止在未知系统目录写结果。不要新增包装框架、数据库、依赖清单或另外一套插件体系。

## 4. 接口与数据结构设计：自包含契约

### 4.1 通用规则与完整共享类型

实现使用标准库 `typing.TypedDict`/`Literal`；如下是接口声明，不要求照搬排版。所有字段必填，只有标为 `| None` 的值可为 null。JSON 中 tuple 表示数组，整数拒绝 bool；数值必须有限；单位写在字段名内。结构字段不允许任意附加字段，只有显式 `dict[str, ...]` 是动态映射。路径均以 UTF-8 保存；日志中的文件引用是所在尝试目录内的相对路径，不允许 `..`。`Json` 表示 JSON 标量、数组或字符串键对象，仅用于原样环境诊断。

```python
Condition = Literal["default", "fixed-1000", "adaptive"]
Scenario = Literal["service", "mixed", "dynamic"]
Operation = Literal["preflight", "survey", "pilot"]
Bins = list[tuple[int, int]]  # [HDR 桶的最高等价值，计数]，单位 us
Json = Union[None, bool, int, float, str, list["Json"], dict[str, "Json"]]

class ErrorCounts(TypedDict):
    connect: int
    read: int
    write: int
    status: int
    timeout: int

class HostQualityConfig(TypedDict):
    sample_seconds: int
    preflight_seconds: int
    preflight_cpu_limit: float
    during_cpu_limit: float
    maximum_sample_gap_seconds: float
    maximum_preflight_wait_seconds: int
    maximum_attempts: int

class BaselineProfile(TypedDict):
    schema_version: Literal[1]
    seed: int
    conditions: list[Condition]
    service_cpus: list[int]
    client_cpus: list[int]
    controller_cpus: list[int]
    port: int
    page_bytes: int
    connections: int
    window_ms: int
    calibration_timeout_seconds: int
    client_timeout_seconds: int
    episode_timeout_seconds: int
    sample_seconds: int
    survey_rates: list[int]
    survey_duration_seconds: int
    survey_required_windows: int
    completion_min: float
    completion_max: float
    client_cpu_limit_percent: float
    min_background_coverage: float
    max_background_delivery_delay_seconds: float
    pilot_service_windows: int
    pilot_mixed_windows: int
    pilot_dynamic_phase_windows: int
    host_quality: HostQualityConfig

class Ready(TypedDict):
    schema_version: Literal[1]
    kind: Literal["ready"]
    start_ns: int
    window_ms: int
    offered_rps: int
    connections: int
    client_threads: Literal[1]
    pipeline: Literal[1]
    histogram: Literal["wrk2_expected_latency_hdr_3sig"]

class Window(TypedDict):
    schema_version: Literal[1]
    kind: Literal["window"]
    seq: int
    start_ns: int
    end_ns: int
    final: bool
    requests: int
    bytes: int
    errors: ErrorCounts
    terminal_exclusions: int
    histogram_overflows: int
    bins_us: Bins

class Footer(TypedDict):
    schema_version: Literal[1]
    kind: Literal["footer"]
    start_ns: int
    end_ns: int
    windows: int
    requests: int
    bytes: int
    errors: ErrorCounts
    terminal_exclusions: int
    histogram_overflows: int
    bins_us: Bins  # 未重置的上游主 HDR，独立复核窗口合并
    export_duration_ns: int  # 已完成的 Ready/Window 导出耗时之和；不含 Footer 自身

class ClientStream(TypedDict):
    ready: Ready | None
    windows: list[Window]
    footer: Footer | None
    complete: bool
    issues: list[str]

class WindowStats(TypedDict):
    duration_seconds: float
    requests: int
    latency_samples: int
    rps: float
    completion_fraction: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    bins_us: Bins

class HostSample(TypedDict):
    utc: str
    elapsed_seconds: float
    collector_pid: int
    host_cpu_percent: float | None
    host_monotonic_seconds: float
    vmware: list[dict[str, Json]]
    hyperv: dict[str, Json] | None
    error: str | None
    hyperv_error: str | None

class Gate(TypedDict):
    passed: bool
    issues: list[str]
    sample_count: int
    maximum_host_cpu_percent: float | None
    limit_percent: float
    maximum_sample_gap_seconds: float | None
    scope: str

class GuestSnapshot(TypedDict):
    schema_version: Literal[1]
    boot_id: str
    virtualization: str
    environment: dict[str, Json]  # inspect_environment 的完整原样输出
    native_build: dict[str, Json]  # state/build.json 原样内容
    wrk2_build: dict[str, Json]  # state/wrk2-window-build.json 原样内容
    source_sha256: dict[str, str]
    checks: dict[str, bool]
    limitations: list[str]

class Request(TypedDict):
    schema_version: Literal[1]
    operation: Operation
    run_key: str
    profile: BaselineProfile
    rates: dict[str, int] | None  # pilot 必须恰为 low/mid/high；其他为 null
    order: list[Condition]  # pilot 必须是三条件的一个排列；其他为空数组
    expected_boot_id: str | None  # preflight 为 null，其余使用 preflight 结果
    expected_source_sha256: dict[str, str]  # preflight 为空，之后精确匹配
    expected_binary_sha256: dict[str, str]  # preflight 为空，其余恰为 native/wrk2

class WorkloadEvent(TypedDict):
    kind: Literal["background_start", "background_stop"]
    started_ns: int
    finished_ns: int
    pid: int
    start_ticks: int

class TrialRecord(TypedDict):
    trial_id: str
    condition: Condition
    scenario: Scenario
    rate: int
    client_log: str
    samples_log: str
    background_log: str | None
    background_intervals_log: str | None
    agent_events_log: str
    driver_events_log: str
    workload_events: list[WorkloadEvent]
    process_returncodes: dict[str, int | None]
    cleanup_errors: list[str]
    error: str | None

class WindowAudit(TypedDict):
    seq: int
    stats: WindowStats
    background_eps: float | None
    background_coverage: float | None  # 纯网页为 null，mixed/transition 为实际比例
    background_delivery_delay_seconds: float | None
    workload_phase: Literal["service", "mixed", "transition"]
    workload_transition: bool
    policy_transition: bool
    issues: list[str]

class TrialAudit(TypedDict):
    trial_id: str
    measurement_passed: bool
    execution_passed: bool
    issues: list[str]
    stats: WindowStats | None
    sustained: bool | None  # survey 可比较时 true/false；前提不足或非 survey 时 null
    client_cpu_limited: bool
    transition_coverage: Literal["observed", "not_triggered", "not_applicable"]
    performance_flags: list[str]  # 请求错误、未达完成率等，独立于测量完整性
    windows: list[WindowAudit]

class RateSelection(TypedDict):
    schema_version: Literal[1]
    status: Literal["selected", "insufficient"]
    service_reference_rps: int | None
    mixed_reference_rps: int | None
    capacity_bracketed: dict[str, bool]  # service/mixed，区分测得上界与保守下界
    rates: dict[str, int]  # selected 时恰为 low/mid/high；否则为空
    source_trial_ids: list[str]
    limitations: list[str]

class GuestSummary(TypedDict):
    schema_version: Literal[1]
    run_key: str
    operation: Operation
    status: Literal["running", "completed", "failed", "interrupted"]
    snapshot: GuestSnapshot | None  # 探测尚未成功/初始化失败时为 null
    trials: list[TrialRecord]
    final_sched_ext: str
    cleanup_errors: list[str]
    error: str | None

class Attempt(TypedDict):
    run_key: str
    operation: Literal["survey", "pilot"]
    index: int
    order: list[Condition]
    preflight_start: float  # Windows QPC 秒
    preflight_end: float
    host_start: float
    host_end: float | None
    guest_returncode: int | None
    fetch_returncode: int | None
    archive: str | None
    archive_sha256: str | None
    preflight_gate: Gate
    run_gate: Gate | None
    status: Literal["running", "completed", "host_rejected", "execution_failed", "interrupted"]
    error: str | None

class StudyManifest(TypedDict):
    schema_version: Literal[1]
    study_id: str
    mode: Literal["survey", "pilot"]
    host: str
    guest_root: str
    profile: BaselineProfile
    calibration: RateSelection | None
    calibration_source: str | None  # pilot 的已审计 survey 目录
    guest_preflight: GuestSnapshot | None  # 网络/环境探测尚未成功时为 null
    host_environment: dict[str, Json]
    host_source_sha256: dict[str, str]
    attempts: list[Attempt]
    status: Literal["running", "completed", "quality_incomplete", "failed", "interrupted"]
    collector_returncode: int | None
    error: str | None

class StudyAudit(TypedDict):
    schema_version: Literal[1]
    status: Literal["passed", "quality_incomplete", "failed"]
    selected_run_key: str | None
    issues: list[str]
    trials: list[TrialAudit]  # 包含全部尝试，用 trial_id 区分
    calibration: RateSelection | None
    limitations: list[str]
```

`run_key` 为 `[A-Za-z0-9_-]{1,100}`；`trial_id` 在整个 study 内唯一且包含 run_key。动态日志不预先假定某窗口一定是纯 service/mixed；通过实际 WorkloadEvent 区间解释，原始窗口保持原样。

### 4.2 F01：构建与窗口导出协议

接口：

- `instrument(header: str, source: str) -> tuple[str, str]`：保持旧对齐补丁行为。
- `instrument_windows(header: str, source: str) -> tuple[str, str]`：输入原始固定提交源码，组合旧补丁并添加新导出，返回头/源文本。
- `python3 tools/prepare_wrk2.py ARCHIVE --windowed`：新构建输出 `state/wrk2-window-build.json`、`state/wrk2-window-measurement.patch`、`state/wrk2-window-build.log`；不得覆盖旧 wrk2-build、旧 patch 和系统 wrk。

窗口构建 manifest 沿用旧 manifest 的全部字段，额外必填 `measurement_protocol="oe_wrk2_windows_v1"`、`window_ms=5000`。固定提交 `44a94c17d8e6a0bac8559b53da76848e430cb7a7`，归档 SHA256 为 `3bc11d20d7d3e72a6b67ce26407eb7c34ae816c882669dcedf578d1c27ad4c20`；本来宾归档在 `/root/vendor/wrk2-upstream-44a94c17-lf.tar.gz`。

启动时通过环境 `OE_WRK2_WINDOW_MS=5000` 启用窗口功能；未设置时表现与旧补丁一致。启用时拒绝多线程、pipeline 非 1、非默认逐响应记录模式；禁止静默降级。

客户端 stdout 保留原始输出，新增每行 `OE_WRK2_STREAM <JSON>`，JSON 为 Ready/Window/Footer 三种之一，每条写完 flush。驱动始终排空 stdout 并保存完整日志，不等待进程退出才读。未完整的一行只能在进程运行中暂缓解析；进程结束后的半行视为测量损坏。

非显然的采集设计：

1. 校准真正成功、上游主 HDR 被重置的同一 event-loop 回调中，建立窗口基点并输出唯一 Ready；此前不计入窗口。校准重试不是 Ready。
2. 每个响应沿用上游算出的 expected_latency_timing，在现有记录位置另写相同配置的窗口 HDR。上游主 HDR、连接计数和发送行为不改；终止分支仍不补造延迟样本。
3. 在同一 event loop 上每约 5 秒切窗，记录实际 CLOCK_MONOTONIC 边界。窗口直方图、请求/字节/错误差分、终止排除数属于同一个连续区间；导出后只清空镜像直方图和镜像计数。导出耗时仍包含在连续时间轴内，不人为扣除。
4. 事件循环结束时导出非空时长的尾窗，`final=true`；正常周期窗为 false。若恰好在前一边界结束，不输出零时长窗，最后一窗可以为 false。随后由 join 后主线程输出唯一 Footer，其直方图来自未重置的上游主 HDR，计数为旧对齐区间的计数。
5. 每次 Ready/Window 导出在 flush 完成后累计序列化、写入和 flush 耗时，最后由 Footer.export_duration_ns 汇总。该字段不含 Footer 自身，也不代表镜像 HDR 的全部采集开销；不能在当前 Window 中填入尚未发生的自身 flush 耗时。不在信号处理器中分配或导出。若 flush/镜像记录失败，明确退出失败或在计数中暴露，不能继续声称采集正常。

逐窗不变量：`requests == sum(bins counts) + terminal_exclusions + histogram_overflows`。没有终止排除的常规窗口通常两者为零；溢出非零即测量不通过。所有窗 seq 从 1 连续递增、前窗 end_ns 等于后窗 start_ns；Footer 的全部计数、区间、桶与窗口合并精确一致。边界相等是代码采用同一基点得到的契约，不用浮点容差修复不一致。

### 4.3 F02/F03：共享纯函数与计算语义

```python
def load_profile(path: Path) -> BaselineProfile: ...
def validate_request(value: object) -> Request: ...
def parse_client_stream(path: Path, *, allow_incomplete: bool = False) -> ClientStream: ...
def summarize_windows(windows: Sequence[Window], offered_rps: int) -> WindowStats: ...
def audit_windows(directory: Path, record: TrialRecord, profile: BaselineProfile) -> list[WindowAudit]: ...
def audit_trial(directory: Path, record: TrialRecord, profile: BaselineProfile) -> TrialAudit: ...
def select_rates(trials: Sequence[tuple[TrialRecord, TrialAudit]], profile: BaselineProfile) -> RateSelection: ...
def host_gate(points, start, end, limit, maximum_gap=8, minimum_points=2,
              *, time_key: str = "utc") -> Gate: ...  # F03，旧调用兼容
```

- 验证、解析的结构错误抛 `ValueError`，正常完整性问题也写入审计 issues。`allow_incomplete=True` 只用于保留中断原件，绝不能返回 complete=true 或使最终审计通过。
- 桶值为 HDR 的最高等价值，正计数、升序不重复，最小允许 0us；空样本允许空数组，百分位为 null，不能写成 0ms。
- HDR 秩必须使用锁定源码 `hdr_value_at_percentile` 的公式 `max(1, floor(N*p/100 + 0.5))`，返回累计数达到该秩的桶最高等价值。与旧 wrk 分布函数分开，禁止修改旧 `histogram.py`。
- 合并通过相同桶值累加后求百分位；时长是所选窗口时长之和，RPS=请求数/该时长，completion_fraction=RPS/计划速率。若所选窗口不连续，报告的是测量覆盖时长内统计，不能冒充跨缺口的全程速率。
- EPS 复用 BackgroundReader 的原始 interval 日志，独立重算相同估计时间原点，只计完整落入目标区间的报告区间，按时长加权。必须同时给出覆盖比例和 delivery delay；混合稳态聚合要求覆盖至少 80%、最大估计延迟 <2 秒。短尾窗/跨负载窗口若不足则 EPS 为 null 并标注，保留窗口，不伪造精确时间对齐。
- `audit_windows` 是逐窗 EPS、覆盖和动作归属的唯一公共计算入口；`audit_trial` 调用它并将结果放入 windows。F08 直接消费这些结果，不重写算法。若流结构无法解析，audit_trial 以 windows=[] 和明确 issues 返回失败；部分可解析记录仅作失败诊断。
- `audit_trial` 复核：主/镜像 HDR 守恒、时轴、CPU/后台数据、实际策略事件和进程清理。request errors 和完成率不足进入 performance_flags；计数完整时不因此把记录判成损坏，也不能据此触发质量重试。
- F03 `time_key="utc"` 保持既有行为；`time_key="host_monotonic_seconds"` 读取有限数值作为同一宿主时间轴，不再解析 ISO 时间。门槛、样本错误和 gap 语义保持原样。

最小输入输出示例：一个 5 秒 Window，requests=5000，bins_us=[[1000,4950],[3000,50]]、其他计数为零；offered_rps=1000。结果 duration_seconds=5、rps=1000、completion_fraction=1、latency_samples=5000、p99_ms=1。按 HDR 源码秩为 4950，不能复用旧公式得到 3ms。若 terminal_exclusions=1，则 requests 必须改为 5001。

### 4.4 F04：冻结配置的具体值

F04 必须是第 4.1 节完整 BaselineProfile，值固定如下：schema_version=1、seed=20261021、conditions=[default,fixed-1000,adaptive]、service_cpus=[0]、client_cpus=[1]、controller_cpus=[2]、port=18088、page_bytes=4096、connections=32、window_ms=5000、calibration_timeout_seconds=25、client_timeout_seconds=3、episode_timeout_seconds=130、sample_seconds=1。

survey_rates=[2000,4000,8000,16000,32000,64000,128000]；survey_duration_seconds=50、survey_required_windows=3；completion_min=0.97、completion_max=1.03；client_cpu_limit_percent=85；min_background_coverage=0.8、max_background_delivery_delay_seconds=2。50 秒包含 25 秒校准上限、三窗各最多 8 秒以及 1 秒余量；客户端 -d 从进程启动前开始计时，不能误当作 Ready 后时长。

pilot_service_windows=4、pilot_mixed_windows=4、pilot_dynamic_phase_windows=4。host_quality={sample_seconds:5,preflight_seconds:30,preflight_cpu_limit:100,during_cpu_limit:100,maximum_sample_gap_seconds:8,maximum_preflight_wait_seconds:120,maximum_attempts:2}。

配置含义：single-client procfs CPU 百分比的 100% 是一个 CPU，85% 是本阶段的客户端 CPU 饱和告警线。该门槛不能证明不存在其他客户端瓶颈。速率表和所有门槛在看见结果之前冻结，执行者不得为获取通过结果改值。

### 4.5 F05/F12：来宾接口与实验语义

```python
def inspect_guest(profile: BaselineProfile) -> GuestSnapshot: ...
def execute_request(request: Request, root: Path) -> GuestSummary: ...
def stop_owned(run_key: str, root: Path) -> dict[str, str | bool]: ...
def main(argv: Sequence[str] | None = None) -> int: ...  # F05/F12 各自入口
```

F05 CLI：`python3 experiments/run_baseline.py --request /tmp/oe-baseline-RUNKEY.json`；停止 CLI：`python3 experiments/run_baseline.py --stop RUNKEY`。停止返回 JSON `{stopped:bool,reason:str}`，reason 为 owner_not_published/already_exited/original_instance_exited/owned_SIGTERM；owner 身份不符或退出未确认时返回非零并保留证据。

preflight 操作由 main 调用 inspect_guest，只输出 `GuestSnapshot` JSON，不创建负载、owner 或归档；execute_request 只接受 survey/pilot，拒绝 preflight。survey/pilot 最后一条机器结果输出为 `{run_key:str,status:str,archive:str|null,archive_sha256:str|null}`，正常完成时 archive 固定为 `/tmp/oe-baseline-RUNKEY.tar.gz`。锁冲突或早期失败尚无归档时允许两个字段同时为 null，必须非零退出并保存宿主日志，不能伪造空归档。宿主从这些固定字段获取结果，但必须核对 run_key、路径和本地摘要；普通进度日志不能被当成完成记录。

Request 经 SCP 上传，文件名必须来自验证后的 run_key；不能把 JSON 拼进远程 shell。SSH 的远程 argv 用 shlex.join 序列化，本地命令用参数数组。

GuestSnapshot 检查必须包括 Linux/root、允许 CPU 包含 0/1/2、sched_ext disabled、bpffs、内核/BTF/后端身份、窗口版 wrk2 身份、nginx/sysbench/taskset、端口空闲，以及 virtualization==microsoft。先采集未绑定 CPU 的环境，再绑定控制线程。缺 PSI、schedstats 或 Hyper-V 宿主管理权限只记限制；实际必需项缺失则拒绝负载执行。

snapshot 的 source_sha256 覆盖本轮实际执行的 Agent Python、实验/测量代码、原生源码、profiles、使用的 eulerbench Python/Lua，键相对共同工作区根目录，以 `oe-resource-agent/...` 或 `euler-bench/...` 开头；source-files 使用同一键布局。native_build 和 wrk2_build 记录实际二进制身份；Request.expected_binary_sha256 分别核对 native/wrk2 的实际构建摘要。按既有缓存审计能力检查已加载 Python 代码与源码，原件随归档保存。源码或二进制在执行前后发生变化则尝试失败，不覆盖源文件继续运行。

固定负载定义：nginx 使用现有 `_prepare_nginx` 的单 worker、master_process off 和 4KiB 静态页面，nginx 与 sysbench 均绑定 service_cpus=[0]；wrk2 单客户端线程绑定 client_cpus=[1]；驱动、Agent 和继承其亲和性的原生后端绑定 controller_cpus=[2]。后台固定 `sysbench cpu --threads=1 --cpu-max-prime=10000 --time=3600 --report-interval=1 run`，由所属进程管理器有界结束。wrk2 使用 `-t1 -c32 -T3s`、显式计划速率、时长、相同 URL 和非流水线默认请求；各组启动参数均保存，实际允许 CPU 从 procfs 核对。不能只根据角色名称推断绑核成功。

**survey**：在默认调度下分别执行 service 与 mixed（1 后台线程）的升序速率测试。每次 50 秒、同一客户端只校准一次；等待 Ready 后采集，基于最后 3 个非尾窗评估。每种场景遇到首个可比较但不 sustainable 的速率或客户端 CPU 告警即停止该场景递增，仍保留该次原始数据。客户端正常退出且结构完整的错误/过载是测量结果；进程异常、超时或清理失败是执行失败。

持续条件：最后三窗逐窗完成率均在 [0.97,1.03]，全程客户端 error 为 0，三窗均有完整资源样本、客户端 CPU 无 >=85% 的区间；mixed 还要求三窗合并后的有效 EPS 覆盖和延迟满足门槛。因完成率或错误不满足，sustained=false；若缺三窗、资源采样不足或 EPS 对齐不足，sustained=null，属于前提不足，不能充当服务容量上界；测量结构损坏直接失败。遇到 null 则停止该场景递增，保留之前合格速率为保守下界，capacity_bracketed=false。资源完整指三窗合并区间的可用连续采样覆盖至少 80%，最大采样间隔不超过 2.5 秒，且进程身份一致。

选档：每种场景取最大 sustainable rate；公共参考值取两者较小值。low/mid/high 分别取公共值的 40%/70%/90% 向下取到 100 RPS 的倍数，必须为正且严格递增。任一场景无有效值则 insufficient。例：service=64000、mixed=32000 → low=12800、mid=22400、high=28800。capacity_bracketed 仅在已有合格下界、随后因完成率/请求错误出现可比较的不持续结果且客户端未饱和时为 true；该布尔值表示当前测量定义下的区间，不是严格物理容量。只跑到列表上限仍 sustained、数据不足或被客户端饱和打断时为 false，并明确这些是保守可用档位，不是测得的最大服务能力。

**pilot**：选用已审计 survey 的 high 档，按 Request.order 逐条件执行 mixed steady 和 dynamic，共 6 个 trial；service steady 已由 survey 覆盖，不另做重复试验。mixed 启动后台后再启动客户端，Ready 后保留至少 4 个常规窗口；dynamic 每个客户端 Ready 后连续运行网页、混合、网页各 4 个窗口，只有一个客户端 PID、一次校准。

动态动作安排：收到第 4 个普通窗口后启动后台，收到第 8 个后停止；分别记录动作前后 started_ns/finished_ns 与任务身份。包含动作区间或跨相邻状态的窗口标记为 transition，不能把其直方图按比例拆给两个阶段；全程汇总包含所有窗口，阶段稳态统计只合并完全落在该状态内的窗口并报告覆盖。控制端读到窗口与采取动作有延迟，必须保留实际动作时间，禁止拿 seq*5s 冒充发生时刻。

pilot 客户端 -d 固定为 `calibration_timeout_seconds + 目标常规窗口数*5 + 5` 秒：mixed 为 50 秒，dynamic 为 90 秒。收到所需窗口不提前发信号截断；额外完整窗口与尾窗保留，dynamic 回到 service 后持续到自然结束。每个 episode 最迟 130 秒开始强制清理；客户端必须在 25 秒内输出 Ready，否则停止本实例并报告校准失败。正常结束后若未获得所需常规窗口数，属于测量前提失败，不能作为算法未响应或容量不足的证据。

三个条件分别构造 AgentRuntime(profile=nginx-cpu,mode=default/fixed/adaptive)，fixed_slice_us=1000，仅 fixed 模式设置；wakeup_preempt 维持 false。所有组保留相同 observer。control 线程始终显式 join 并记录异常，不能只依据 stdout/管道关闭判断退出。

阶段一不强迫 adaptive 一定启用 SCX：如果观测不满足现有规则，记录 not_triggered 和实际观测，交给阶段二诊断；不得为让本次结果好看修改阈值。观测到切入和回退则 transition_coverage=observed。必须始终确认实际动作、map 参数和退出状态一致。

F12 CLI：`python3 experiments/check_wrk2_windows.py --duration-seconds 35`，启动本实例 nginx、低速率 2000 RPS 的窗口版 wrk2，校验 F01/F02 守恒和导出协议；使用 ResourceLock、端口 18088 和同样的所属实例清理，写 results 中独立目录并返回 0/1。不启动 sysbench，不加载 SCX。

### 4.6 F06/F07/F08：宿主接口

```python
def run_study(*, host: str, key: Path | None, guest_root: str,
              output_root: Path, mode: Literal["survey", "pilot"],
              calibration_dir: Path | None) -> Path: ...  # F06，返回 study 目录
def audit_study(directory: Path) -> StudyAudit: ...  # F08，不修改原始数据
def write_report(directory: Path, audit: StudyAudit) -> None: ...  # F08
def main(argv: Sequence[str] | None = None) -> int: ...  # 各入口独立
```

F06 CLI：`python Z:/research/run_baseline.py --host HOST --guest-root /root/oe-resource-agent --output-root C:/指定本地目录 --mode survey [--key KEY]`。pilot 增加 `--calibration-dir 已通过审计的survey目录`。--key 省略时沿用 OpenSSH 默认身份，不注入不存在的旧私钥路径；提供时使用 -i/IdentitiesOnly。BatchMode、ConnectTimeout=8、StrictHostKeyChecking=yes；未知主机身份由用户按常规 SSH 配置处理，不自动关闭核验。

F08 CLI：`python Z:/research/analyze_baseline.py STUDY_DIRECTORY`；审计 passed 返回 0，quality_incomplete 返回 2，数据/执行失败返回 1。报告始终包含全部尝试，不因单次失败删除其他原件。

宿主在网络探测前初始化合法 manifest，guest_preflight=null、attempts=[]、status=running；失败时保留 null 和明确 error。来宾实际操作的 summary 同样可先以 snapshot=null 初始化，成功探测后填充；completed 状态绝不允许空 snapshot。preflight 不满足必需检查时不得进入负载。run_study 对已创建目录的预期实验失败也返回该目录，由 CLI 根据最终审计/manifest 返回状态；无法创建结果目录等 I/O 初始化错误可以抛异常。宿主与来宾 CLI 的执行失败返回 1，宿主仅质量/定档证据不足返回 2；参数用法错误采用 argparse 的 2。

F07 原有参数保持兼容。新增 `host_monotonic_seconds=[Stopwatch]::GetTimestamp()/[Stopwatch]::Frequency`，Windows Python 使用 `time.perf_counter()`；二者基于当前 Windows 的 QPC，同一台机器可比较。实现测试需核对启动后采样值落在驱动记录的前后边界内。utc 仅作展示，不用来跨来宾和宿主换算时间。

保留 vmware 数组以兼容旧流程；Hyper-V 诊断写入 hyperv/hyperv_error，无权限时为 null/错误说明，不能令有效 CPU 样本失败。error 仅表达宿主 CPU 样本失败；不得把读取不到 vmwp 或 Get-VM 权限不足折算为 0% 负载。采集器按 5 秒目标周期调度，记录实际 QPC 时间，分析使用实际间隔。

宿主命令开始前记录 host_start、远程进程完成后记录 host_end，质量判定覆盖这整个较宽区间；不依赖两台系统 UTC 同步。新驱动使用 pwsh 隐藏启动 collector，不额外设置 ExecutionPolicy Bypass。collector 必须先输出有效样本并通过 30 秒 preflight 才开始远程负载。

一次 study 包含 survey 或一个 pilot 周期；两者独立归档。只因完整尝试的宿主负载/采样质量不合格允许重试整个操作，最多 2 次，pilot 的 condition 顺序保持不变。不允许性能择优重试，也不允许只补跑其中一个策略来替换配对结果。首次完整执行且质量通过的尝试被选中，其他尝试保留。preflight 最多等待 120 秒，不能等待则 quality_incomplete 并退出，不启动远程负载。

### 4.7 既有原始日志的最小消费契约

既有记录保持原样，允许包含其他现有字段；“禁止额外字段”只约束本阶段新增的第 4.1 节结构。F02/F08 对现有日志只依赖以下明确字段，不要求下游 Agent 猜测现有实现：

| 原件 | 必需消费字段与含义 |
|---|---|
| samples.jsonl | monotonic（秒）；首条之后有 sample_start_monotonic、sample_interval_seconds；sched_ext；processes 的 server/client/background/controller/native 角色为 null 或含 pid、start_ticks、cpu_percent、rss_mib、context_switches_per_sec。首条无差分不是损坏；只合并完全落在目标区间内且身份一致的差分样本 |
| background-intervals.jsonl | elapsed_seconds、events_per_sec、received_monotonic；elapsed 必须严格递增，EPS 有限非负。origin=min(received_monotonic-elapsed_seconds)，每条代表前一 elapsed（首条为 0）到当前 elapsed 的完整报告区间 |
| driver-events.jsonl | event=process_started 含 monotonic、pid、start_ticks、boot_id、role、command；process_stopped 含 monotonic、pid、role、returncode。用保存的角色和实例关联开始/结束，不能凭 PID 数值跨实例合并 |
| events.jsonl | 通用 monotonic/event；agent_started 含 agent_version/mode；policy_applied 含 before/after/revision/action_started_monotonic/action_finished_monotonic；backend_stopped 含 pid/returncode/sched_ext；agent_stopped 含 sched_ext。after 中 SCX 状态含 policy_id、slice_us、pid、map_id、generation、wakeup_preempt；默认状态至少含 policy_id、slice_us、sched_ext |
| native-PID.log | OE_ADAPTIVE_CONFIG / OE_ADAPTIVE_STATS JSON，实际 slice_ns、wakeup_preempt、accounting_errors；正常用户卸载文本 EXIT: unregistered from user space，配对 backend_stopped 事件。未使用 SCX 的条件没有该文件是正常现象 |

进程退出要求：client 必须自然返回 0；由 ProcessManager 停止的 nginx/sysbench 允许约定信号返回码，但必须有匹配 process_stopped、无 cleanup_errors；native 必须有匹配 backend_stopped 且返回 0。TrialRecord.process_returncodes 的键使用 `role:pid:start_ticks`，不能用一个 role 覆盖多个实例。controller 是线程，退出由 agent_stopped、显式 join 和错误记录共同确认，不伪装成进程 returncode。

WindowAudit.workload_phase：完全位于后台启动区间之前或停止区间之后为 service；完全位于启动 finished_ns 与停止 started_ns 之间为 mixed；与任一实际动作区间/边界重叠为 transition。mixed steady 的后台在 Ready 前已经启动，所有 Ready 后窗口按 mixed 解释。使用半开区间 [start_ns,end_ns)，边界恰好相等不算重叠。policy_transition 独立判断窗口是否与 policy_applied 的动作区间重叠或包含 revision 生效边界。CSV transition=workload_transition or policy_transition；不得因策略切换而抹掉负载阶段分类。

原始 backend/driver/Agent 时间是 Linux CLOCK_MONOTONIC 秒，转换为 ns 时只用于比较；允许至多 1 微秒的转换舍入容差。窗口协议自身 ns 连续性不使用该容差。纯网页没有 sysbench 日志时 null 是正常值；混合场景缺原始后台日志是证据缺口。

## 5. 核心流程设计

### 5.1 正常流程

1. 执行者先完成离线实现与测试；单独使用新构建入口构建窗口版 wrk2，不重编 SCX。
2. F12 做一次最小真实客户端验证；确认主/镜像 HDR 一致、默认调度恢复后进入 survey。
3. 宿主创建本地结果目录，记录配置和源码身份；通过 SSH preflight 获取未绑核的 GuestSnapshot。preflight 不长期持锁；每次实际操作还要在取得锁后重验资源状态。
4. 启动 collector，检查存活和样本新鲜度，完成 preflight。将完整 Request 上传来宾并执行。来宾取得 ResourceLock 后创建独立 results/baseline-RUNKEY，发布 owner，执行相应工作负载。
5. 远程运行期间宿主持续检查 collector 和总时限；来宾持续排空客户端输出并采样，窗口完成触发动态负载动作。所有 JSON 元数据定期写入，进程失败也留下部分记录。
6. 来宾停止 controller、关闭自身业务进程、确认默认调度恢复、落 summary 并归档。宿主下载原件并核对归档摘要，等待覆盖结束边界的下一份宿主样本，计算门槛。
7. 执行失败、归档失败或测量损坏停止本次 study；纯宿主质量失败按预算重试。通过后运行独立审计；survey 生成 calibration.json，pilot 消费该文件并冻结其来源。
8. 分析器重新解包所有归档，从原始日志复算，输出所有窗口和汇总；最后确认 collector 退出且本次工作负载无遗留。

### 5.2 异常与边界

- 其他实验持锁、已有 SCX 或端口被占用：拒绝执行，不能停止对方。锁内发生变化以锁内状态为准。
- Ready 超时、半行、序号缺失、时间逆序、Footer 缺失、主/镜像桶不一致、HDR 溢出：measurement_passed=false，保存原件；禁止回退旧聚合数据冒充连续测量。
- 客户端 error/过载但正常退出且记录可复算：作为 performance_flags 和 survey 的不持续结果；不以性能为由重试。异常退出或超时：execution_passed=false。
- 缺后台 coverage：短尾窗只标 null；survey 的三窗合并仍不足则不能认定 sustainable。资源样本或 CPU 身份缺失不能当作客户端未饱和。
- SIGINT/SIGTERM：来宾只清理自己的实例；宿主调用 --stop，然后等待 SSH 子进程结束并尽量下载部分归档。网络断开时记录停止未确认，禁止宣称清理成功。
- --stop 复用既有 run_controlled_cycle.py:stop_owned 的 boot/PID/start_ticks + pidfd 思路，但路径必须指向新 baseline 目录；不能调用它的旧 controlled 路径，也不能调用全局 stop_active。
- owner 尚未发布的短竞态：宿主最多在 5 秒内重查停止一次；仍未发布则明确未确认停止。来宾自身设置总操作上限：survey 1200 秒、pilot 900 秒，清理额外最多 45 秒；宿主对应等待上限为来宾上限再加 90 秒，避免断网后无界运行，也不因 survey 调整时长提前误杀。
- survey 与 pilot 之间 guest boot、执行源码或二进制身份变化：拒绝沿用定档，重新执行 survey；可用内存等波动字段不用于身份拒绝。不能因为提交记录时间变了就否定相同实际代码。
- 既有进程遗留清理失败：保留注册记录并报告，不自动强杀未知任务。

## 6. 数据存储与状态设计

第 4 节 TypedDict 是持久 JSON 的规范结构，无需额外维护重复的 Schema 文件或引入数据库。读取端验证字段、类型、版本和跨记录不变量；未经验证的结果不得参与选档或比较。

Windows 目录：

```text
OUTPUT_ROOT/baseline-{survey|pilot}-UTC-ID/
  manifest.json                 # StudyManifest，原子替换写入
  profile.json                  # 实际完整 BaselineProfile
  host-cpu.jsonl                # HostSample，全程 append+flush
  collector.log
  source-files/                 # 实际宿主执行器/采集器/分析器及配置副本
  attempts/RUNKEY.log
  attempts/RUNKEY.tar.gz
  raw/RUNKEY/                   # 校验归档后解包，不覆盖已有不同原件
  calibration.json             # 仅有效 survey 的 RateSelection
  audit.json                   # StudyAudit，衍生文件
  windows.csv
  trials.csv
  analysis.html
```

来宾目录：

```text
results/baseline-RUNKEY/
  request.json
  owner.json                    # {run_key,boot_id,pid,start_ticks}
  summary.json                  # GuestSummary，含 snapshot 和 trials
  source-files/                 # 本轮实际 Agent/实验/依赖源码副本
  python-cache-audit.json
  trials/TRIAL_ID/
    client.log                  # 原始 stdout/stderr；含连续协议和原有输出
    samples.jsonl               # 既有 Sampler 原始格式
    background.log
    background-intervals.jsonl   # 既有 BackgroundReader 原始格式
    events.jsonl                # 既有 AgentRuntime 格式
    driver-events.jsonl         # 既有 ProcessManager 格式
    native-PID.log              # 若加载后端，既有原生格式
/tmp/oe-baseline-RUNKEY.tar.gz
```

TrialRecord 必须指向确切文件。摘要引用的缺失原件不能忽略。source-files 只保存真实执行源和配置，摘要用于审计实际执行身份；不对每个指标行计算哈希。

状态与所有权：

- StudyManifest 按 running → completed/quality_incomplete/failed/interrupted 转移；completed 需要选中尝试、独立审计通过及 collector 正常退出。collector 退出失败时不得在 finally 后留下 completed。先结束 collector 并保存 returncode，再运行最终审计；审计从原件推导结论，不以 manifest.status 已是 completed 为前提，避免循环依赖。
- Attempt 状态区分正常完成、纯宿主拒绝、执行失败和中断；宿主质量通过不能覆盖执行错误。
- GuestSummary 必须在清理、controller join 和最终状态核对后才能标 completed；采集器/进程管道关闭不能代替 WaitForExit/poll/join。
- 原始文件创建后只追加或完成落盘，不被分析器改写。衍生 audit/CSV/HTML 可重算；不自动删除试验数据、失败尝试或旧研究结果。
- 来宾 owner 发布前必须已取得 ResourceLock；停止仅向核对后的 pidfd 发信号。归档对端路径固定来自已校验 run_key。解包沿用既有拒绝越界路径/链接的边界，不能直接对任意成员 extractall。

CSV 契约：windows.csv 每行至少包含 run_key、trial_id、condition、scenario、rate、seq、start_ns、end_ns、final、requests、latency_samples、p99_ms、rps、completion_fraction、error_count、terminal_exclusions、background_eps、background_coverage、transition。trials.csv 每行包含 run_key、trial_id、condition、scenario、rate、measurement_passed、execution_passed、sustained、client_cpu_limited、transition_coverage、requests、p99_ms、rps、performance_flags。空值写空单元格，不写 0。

报告固定区块：环境与版本、全部尝试及质量判定、选档依据、逐窗口时间线、策略/负载动作、各 trial 的指标与错误、清理状态、限制。使用标准库生成静态 HTML 表格和简单文本时间线即可，不新增图表依赖。单周期只列观察差异，不做 bootstrap 置信区间或正式收益结论。

## 7. 与现有代码的兼容方案

- 旧 `prepare_wrk2.py ARCHIVE` 的产物和 manifest 不变；`--windowed` 使用独立 cache 构建目录和独立 state 文件。旧构建的具体 hash 不要求与新增代码再次构建一致，但旧工具语义和回归测试必须保持。
- 现有 `parse_wrk2`、`business_window`、WINDOW_SCHEMA、SLO 会话和旧分析器不改。新协议使用独立前缀与新 measurement 模块，不把跨策略动态窗口送入旧 metrics.submit。
- `quality.host_gate` 只增加默认值兼容的关键字参数；已有 UTC 门槛测试必须通过。collector 保留旧 CLI、utc、elapsed_seconds、collector_pid、host_cpu_percent、vmware 字段，旧分析器仍能读取。
- 复用 `ResourceLock`、`ProcessManager.start/stop/stop_all`、`Sampler`、`BackgroundReader`、`_prepare_nginx`、`_wait_ready`、`ensure_port_free` 和 `AgentRuntime`。实现者阅读具体签名，不修改 euler-bench；新驱动负责更严格的覆盖检查。来宾入口的 sibling 路径从自身 ROOT.parent 推导，Linux 专属动作延后到执行函数；Windows 离线导入和 --help 不应访问 /proc 或调用 getaffinity。
- Windows 宿主入口在导入 oeagent 前，将 sys.pycache_prefix 指向本地临时目录；现有 oeagent.__init__ 会显式写内容缓存，不能让宿主分析过程在共享 Z 盘上更新来宾使用的缓存。分析器不得为审计导入归档中的任意 Python 代码。
- ProcessManager 通过当前 env 继承环境；启动窗口版 wrk2 时在其 argv 前使用目标机已有 `/usr/bin/env OE_WRK2_WINDOW_MS=5000`，仍由 taskset 包装并 exec 为同一子进程。将 env 的可用性纳入 preflight；不修改父进程 os.environ，不让其他实验客户端无意启用新模式。
- 环境检查可以保存 inspect_environment 的可选项失败，但不得依赖未使用的原始 scx_simple 或 matplotlib 才运行本阶段 pilot；明确区分本阶段必需项与旧通用检查的 ok。
- 文档区分当前 Hyper-V 数据和历史 VMware 数据。旧验证结果保留，阶段一结果使用独立目录，不覆盖 state 中既有验证结论。

## 8. Stage Map：workflow 调度蓝图

以下 S 编号是本阶段内部工作包，与项目整体六阶段不同。代码并行只在文件所有权互斥时进行；任何会构建来宾测量工具或启动负载的步骤必须在同一来宾串行。

| Stage | 目标与文件所有权 | 硬依赖 | 无依赖/并行关系 | 验收关卡 |
|---|---|---|---|---|
| S0 | 只读环境确认、检查工作树/文件差异、记录执行路径；不改源码 | 无 | 最先单独完成，避免其他 Agent 开始改文件后核对不清 | SSH 确认 systemd-detect-virt=microsoft、内核和三目录；读取已有验证；确认无活动实验，发现未知修改时保留并报告 |
| S1 | 实现共享契约/解析/审计/选档与单调时间门槛；F02/F03/F04/F09 | S0 | 与 S2 并行；S2 直接按第 4 节契约开发，不等待函数实现 | `python -m unittest discover -s tests -p test_measurement.py -v`，包含 §4.3 精确示例、主镜像不一致、尾窗/空样本、缺 coverage、选档、UTC/QPC 门槛 |
| S2 | 实现隔离的 wrk2 窗口构建和 live checker；F01/F12 | S0 | 与 S1/S3 并行；此时只实现和离线补丁检查，不在共享来宾编译/跑负载 | `python -m py_compile tools/prepare_wrk2.py experiments/check_wrk2_windows.py`；对锁定归档做只读解包/补丁锚点测试，旧 instrument 不变；新增模式缺参数/多线程拒绝有验证方案 |
| S3 | 更新宿主 collector 并实现独立分析器；F07/F08 | S1 | 与 S2 或已就绪的 S4 并行；分析器 import/帮助关卡需要 F02 已落地 | pwsh Parser.ParseFile 检查 F07 语法无错误；F08 import 无副作用，`python Z:/research/analyze_baseline.py --help` 成功；真实数据留 S6/S7 |
| S4 | 实现来宾执行器及假进程测试；F05/F10 | S1、S2 | 可与 S5 并行，不能修改其文件 | `python -m unittest discover -s tests -p test_baseline_guest.py -v`；验证 Ready 超时、单客户端动态动作、锁冲突、source/boot 变化拒绝、清理失败不报 completed、停止身份不匹配 |
| S5 | 实现宿主编排与联合离线分析测试；F06/F11 | S1、S3 | 可与 S4 并行 | `python -m unittest discover -s tests -p test_baseline_host.py -v`；fake SSH/SCP/collector 验证首次质量通过选择、整操作重试、性能差不重试、下载/collector 失败、截断归档和审计重算；`--help` 无系统副作用 |
| S6 | 集成回归、来宾构建、最小真实客户端检查；不再并行改源码 | S4、S5 | 共享来宾独占；需集成全部前序文件 | 来宾 `python3 -m unittest discover -s tests -v` 全量通过；构建命令及 F12 命令见下文；旧 state/wrk2-build.json 不变；默认调度恢复 |
| S7 | 实际运行 survey、独立审计后运行 pilot；生成原始及衍生结果 | S6 | 严格串行；同机不运行其他性能/资源实验 | Windows 两条 run 命令及对应 analyze 命令；audit passed、calibration selected；全部 6 个 pilot trial 可复算、单客户端动态链路完整、退出清理确认；收益数字不作通过门槛 |
| S8 | 整理复现步骤与阶段一交接；F13 | S7 | 独立数据复核可交给另一 Agent，但只读原件 | 按 F13 重跑 analyze 得到相同指标；报告原始路径、限制及未触发动作；列出阶段二需要解决的问题，不改算法 |

S2 的 live checker 依赖 S1 的最终接口，所以开发时可以用契约联调，实际运行统一延后到 S6。S3/S5 的分析测试由 F11 汇总，S3 不另开重复测试文件。若前序离线关卡不通过，不得直接跑真实负载绕过。

S6 来宾命令（在 `/root/oe-resource-agent`）：

```bash
python3 -m unittest discover -s tests -v
python3 tools/prepare_wrk2.py /root/vendor/wrk2-upstream-44a94c17-lf.tar.gz --windowed
python3 experiments/check_wrk2_windows.py --duration-seconds 35
cat /sys/kernel/sched_ext/state
```

S7 Windows 命令示意，HOST 和目录必须替换成 S0 的实际值；默认 SSH 身份可用时无需 --key：

```powershell
python Z:/research/run_baseline.py --host HOST --guest-root /root/oe-resource-agent --output-root C:/oe-baseline-output --mode survey
python Z:/research/analyze_baseline.py C:/oe-baseline-output/实际survey目录
python Z:/research/run_baseline.py --host HOST --guest-root /root/oe-resource-agent --output-root C:/oe-baseline-output --mode pilot --calibration-dir C:/oe-baseline-output/实际survey目录
python Z:/research/analyze_baseline.py C:/oe-baseline-output/实际pilot目录
```

S7 的具体关卡：宿主 preflight/运行门槛通过；主/镜像计数与桶完全一致；常规窗口采集连续、实际持续时间为正且最长不超过 8 秒；客户端没有校准重启；mixed steady 聚合 EPS 覆盖至少 80%；动态负载的启动/退出区间和策略事件均可定位；每个 trial 的 Footer 导出耗时汇总及 client CPU 告警可见；最终 sched_ext disabled、controller/native/业务进程均已确认退出。未触发 adaptive 切换是明确的限制，不是伪造执行失败或改阈值重试的理由。

质量始终不合格则 S7 输出 quality_incomplete，保留全部结果，不进入“阶段一通过”；允许交付实现和失败诊断，明确尚缺哪项实测证据。无法完成有效选档时不生成伪 calibration，也不进入 pilot。

### 8.1 已有实现与下一轮执行入口

S0–S6、collector 本地副本修复、合法重试成功后的状态聚合和完整时长预算均已交付；最新宿主 17 项测试通过。当前仍无合格 survey/pilot。

下一轮按 stage-1-baseline-followup.md 执行：只将 baseline-profile.json 中 preflight_cpu_limit 与 during_cpu_limit 设为 100，调整受默认值影响的测试 fixture；随后使用现有命令新建 survey，独立审计和选档通过后运行六 trial pilot。没有新模式、接口或记录版本。

宿主 CPU 仍记录，但不按占用高低筛选；30 秒预检用于取得完整有效样本，最大采样间隔仍为 8 秒。数据、身份、归档、测量、进程和清理失败仍阻止验收。测试中需要模拟 CPU 超线拒绝时，使用显式低阈值 fixture，不删除覆盖。

配置先冻结再实验，旧 study 的 profile/manifest 和当时审计状态不改。任务锁以非阻塞 flock 判定，正式窗口不并行构建、测试或分析。每种操作本轮最多一个 study，内部固定重试预算不变。新结果通过只代表默认环境测量和行为对照可用，不能称为低干扰基准或正式性能收益。

## 9. 执行 Agent 约束与决策权限

### 9.1 架构决策：必须遵守

- 以本文第 4 节作为跨 Agent 唯一接口契约。发现矛盾先向主 Agent 报告，由主 Agent 统一修订并广播，禁止各自改变字段。
- 只修改第 3 节白名单中的本人负责文件；共享来宾实验不得并发，代码编写按 Stage Map 并行。没有 Git 元数据时使用文件 diff 交付，不擅自初始化仓库或创建分支。
- 依赖限于 Python 标准库、项目已有 Python 模块、OpenSSH、pwsh，以及目标机已有 nginx/sysbench/taskset/wrk2 构建依赖。不要引入 pytest、pandas、第三方 SSH SDK、Web 框架或新图表库。
- 本阶段不修改调度算法、SLO 反馈规则、SCX 后端、cgroup/网络插件，不修改系统电源计划、关闭用户应用或修改 Hyper-V 配置。
- 保留原有身份/阈值/失败回退检查；只停止本次所属进程。不得用 pkill、全局清理命令或卸载未知调度器取得空闲环境。
- 质量失败与性能结果分开；不删除失败样本，不在正式采集过程中调整门槛，不将推断的计划完成差额称为真实队列积压。
- 不发布 PR、不向外部发送结果、不改内存指导文件；本任务授权是实施和本地验证。

### 9.2 实现决策：执行者自主

私有帮助函数拆分、局部变量名、标准库序列化/原子写入方式、mock 写法、HTML 样式和补丁内部组织由执行者决定；遵循当前代码风格。无需给每个显然分支写样板测试，测试围绕契约、计数、时轴、重试和清理等真实失败模式。

### 9.3 主 Agent 的集成责任

每个子任务提供负责文件、完整共享契约、前置产物位置、禁止修改范围与 verify 命令。前序源码变更在依赖节点开始前集成；同一文件只有一个写入 owner。阶段结果至少报告修改文件、通过关卡、原始证据路径和未解决问题，不能只回“完成”。

### 9.4 本阶段的取舍

选用现有行为规则 Agent 做连续动态 pilot，避免为了测量先改造 SLO 窗口契约；SLO 连续指标适配属于阶段二。选择镜像 HDR，避免重置主 HDR 破坏独立守恒检查。选择新增专用入口，避免把旧固定窗口、旧动态和旧抢占报告强行统一成大重构。选择小规模单周期验证，正式五周期和完整策略矩阵留给后续性能评价阶段。
