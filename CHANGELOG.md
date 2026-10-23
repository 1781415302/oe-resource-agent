# 变更记录

## 0.1.3+ (Stage 1 & Stage 2 完结版) — 2026-10-23

- 完成 Hyper-V 阶段一连续窗口 `wrk2`（`--windowed`）、跨端测量契约 `oeagent/measurement.py`、默认调度 Survey 定档（mixed 64,000 RPS）与 Pilot 六组对照（57,600 RPS 下 `adaptive` mixed p99 `3.309 ms`、dynamic p99 `3.169 ms`）。
- 完成阶段二连续窗口在线业务 SLO 反馈闭环（`experiments/check_slo_continuous.py`）：支持 `wrk2.hdr.window`、多窗口基线中位数确认（`baseline_confirmations`）、积压追赶过滤（`baseline_backlog_draining`）与 1s 后台报告边界裁剪对齐（`align_background_intervals`）。25,600 RPS 混部下切入 1ms SCX + 唤醒抢占后 p99 从 `3.505 ms` 降至 `1.987 ms`（-43.3%），控制线程 CPU 仅 `0.342s`。
- 启用内核 `kernel.sched_schedstats=1`，`WorkloadObserver` 采集真实 `run_wait_fraction` 调度等待占比。
- 补齐 `resource.cgroup` 进程迁入/迁出（`attach`/`detach` 核对 PID 与 `start_ticks`）及 1:4 CPU 权重争用隔离实测（实测比例 `3.96:1`）。
- 新增 `native/oe_net_hook.bpf.c` 与 `network.ebpf_tc` 插件（TC `sched_cls` 端口包/字节统计与 `TC_ACT_SHOT` 丢包隔离）以及 `security.lsm` 插件（BPF LSM 栈发现与进程 Capability 审计）。
- 新增一键演示入口 `experiments/run_demo.py` 与完整课程设计报告 `docs/course-project-report.md`；101 项确定性单元测试与来宾全量验收通过。

## 0.1.2 — 2026-10-15

- 新增版本、boot、任务身份、时间新鲜度及同策略区间校验的metrics.submit/status接口。
- 新增默认基线、1ms探测、约束评价、冷却与业务反馈超时回退；区分严格达标与保留改善但SLO仍未达标。
- 固定wrk2提交，导出校准后同窗口计数和时间，显式核对上游结束边界的HDR排除，不修改延迟分布或发压算法。
- 新增40000 RPS默认/固定1ms/业务反馈Agent的冻结对照，保存执行源码副本、实际补丁和原始区间。
- 43项确定性检查、原生8项、5项公开SLO会话检查、真实严格拒绝与约20秒业务指标失联回退通过。性能冻结时为40项，收尾增加零基线、不可比较及缓存边界；执行源码独立保存。
- 修复确定性tar时间戳与同大小模块导致的旧Python缓存复用，使用PEP552内容校验并审计实际代码/源码；运行事件与能力输出记录真实加载版本。修复前发现版本模块缓存陈旧，其余当时模块匹配。
- 固定速率9次/27窗口审计通过，全部未达到3ms；宿主有87% CPU占用采样、约122秒未覆盖，不将本轮作为独立收益归因。三次探测一次保留、两次拒绝并冷却。
- 通用环境快照改为控制器绑CPU之前采集；持久协议支持批量输入与空闲输入期间信号退出。

## 0.1.1 — 2026-10-15

- 新增同版后端的默认、固定1ms、固定20ms及Agent三阶段动态比较，记录冻结排程、源码及依赖指纹。
- 导出wrk真实报告直方图，合并计数重算阶段百分位，记录客户端间隙、后台区间及策略动作重叠。
- 加入固定参考模式、策略快照锁、动作起止时间、原生退出审计和控制线程异常检查。
- Windows及来宾18项确定性检查、原生8项和公开工具/负载闭环/cgroup参数回归通过。
- 动态quick 12/12有效、144窗口，9份原生生命周期日志独立复核；三次加入/退出均完成切换。当前混合阶段配对中位p99 -26.36%、网页RPS -1.27%、后台EPS -6.65%。
- 报告含原始日志、CSV、逐窗口交互图、CPU/RSS/上下文切换、宿主CPU采样及明确的测量范围。3次重复与闭环VM结果仅供探索。
- 已知快照在控制器绑CPU后采集，通用多CPU检查记录的是绑定后掩码；保留历史原件并在报告说明，后续修正采集时机。

## 0.1.0 — 2026-10-15

- 实现procfs workload观察、PID/启动时间核对、声明角色与实际行为结合的规则路由。
- 采用已验证时间片核心派生scx_oe_adaptive，增加BPF hash map在线控制、map身份/版本/参数边界校验与心跳租约。
- 实现内核默认/SCX生命周期、最短驻留、故障和正常退出回退。
- 提供JSON工具注册表、Schema、持久工具会话、可执行cpu-adapt Skill、插件接口和v1空受管cgroup权重配置。
- 在实际openEuler通过原生8项检查、真实35窗口负载闭环、公开工具会话、Skills入口和cgroup参数回读；15项确定性检查在Windows与来宾通过。
- 保存3个固定专家54轮quick的真实原始结果及独立分析；动态Agent性能仍待独立实验。
- 网络/安全hook、在线业务SLO反馈、动态性能与完整自动环境准备继续迭代。
