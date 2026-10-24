# openEuler 自适应资源管控 Agent 课程设计总结报告

## 1. 项目背景与总体目标

在云原生混部（Co-location）场景下，延迟敏感型在线服务（如 Nginx Web 服务）与计算密集型离线批处理任务（如 sysbench 科学计算/批处理）往往需要共享同一物理核或虚拟核以提高资源利用率。Linux 默认 CFS/EEVDF 调度器采用较长的时间片与通用公平策略，当在线服务线程从网络 I/O 睡眠中被唤醒时，常因批处理任务占用 CPU 而产生较高的排队等待（Runqueue Wait）和尾延迟（p99 Latency）。

本项目基于 **openEuler 24.03 LTS-SP4（Linux `6.6.0-scx` 内核）** 实现了一套**可观测、可解释、可在线调参、可故障自愈回退的自适应资源管控 Agent（`oe-resource-agent`）**，集成了：
1. **eBPF `sched_ext` (SCX) 自适应内核调度后端**（支持 BPF Map 在线调整时间片与短睡眠任务唤醒抢占 `SCX_KICK_PREEMPT`）；
2. **多源工作负载感知与双模式控制平面**（基于 `procfs`/`schedstats` 的行为规则路由 + 基于连续窗口 HDR 直方图的在线业务 SLO 反馈闭环）；
3. **受管 Cgroup 进程归组与 CPU 权重隔离插件**（`resource.cgroup`）；
4. **eBPF TC 网络流量统计与端口级隔离 Hook 插件**（`network.ebpf_tc`）及 **Linux LSM 安全上下文审计插件**（`security.lsm`）；
5. **标准化的 JSON Schema 工具链、持久 JSON-Lines 控制会话与可执行 Skill**。

---

## 2. 系统总体架构

```text
+-----------------------------------------------------------------------------------+
|                         用户接口层 (CLI / JSON-Lines / Skills)                     |
|   ./agent run   |   ./agent serve   |   ./agent tool <name>   |   ./agent skills  |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                         Python 控制平面与决策运行时 (oeagent/)                      |
|  +--------------------+  +-----------------------+  +--------------------------+  |
|  |  WorkloadObserver  |  |  Router (行为规则路由) |  | SloGuard (在线SLO反馈闭环)|  |
|  | - procfs CPU/切换   |  | - 连续窗口防抖确认    |  | - 多窗口基线中位数确认   |  |
|  | - schedstats 等待   |  | - 最短驻留时间保护    |  | - 积压追赶(Backlog)过滤  |  |
|  | - PID+start_ticks  |  | - 单服务回退默认调度  |  | - 吞吐/后台预算与自动回退|  |
|  +--------------------+  +-----------------------+  +--------------------------+  |
+-----------------------------------------------------------------------------------+
        |                              |                             |
        v                              v                             v
+-----------------------+  +-----------------------+  +-----------------------------+
|  CPU 调度后端 (SCX)   |  |  Cgroup 资源隔离插件   |  |  eBPF 网络与 LSM 安全插件    |
| scx_oe_adaptive.bpf.c |  |    resource.cgroup    |  | network.ebpf_tc / lsm       |
| - 虚拟时间加权队列    |  | - 受管组创建与旧值核对|  | - oe_net_hook.bpf.c (TC)    |
| - BPF Map 在线改片    |  | - 进程身份核对与迁入  |  | - 端口包/字节计数与丢包隔离 |
| - 唤醒抢占 200us 限频 |  | - CPU shares 争用隔离 |  | - BPF LSM 与 Capability 审计|
| - 心跳租约与失联自愈  |  | - 退出安全迁回根组    |  | - 卸载自动恢复通信          |
+-----------------------+  +-----------------------+  +-----------------------------+
```

---

## 3. 核心模块设计与实现亮点

### 3.1 原生 `sched_ext` 自适应调度与唤醒抢占（`native/`）
- **在线无感调参**：通过 BPF Hash Map（[`oe_control`](../native/oe_adaptive.h)）在不卸载调度器的情况下动态更新 `slice_ns`（`100 ~ 20000 µs`）和 `policy_flags`，每次更新严格核对 `map_id` 与递增 `generation` 并回读验证。
- **短睡眠唤醒抢占（`OE_POLICY_WAKEUP_PREEMPT`）**：当延迟敏感任务从睡眠唤醒、上次突发运行时间不超过时间片一半，且目标 CPU 正被连续运行两个周期以上的批处理任务占用时，触发 `SCX_KICK_PREEMPT`（每 CPU 200µs 限频保护），并在 `enqueue` 中通过 `scx_bpf_task_cpu(p)` 准确处理单核绑定的任务。
- **心跳租约与失联自愈**：用户态守护进程监控 `heartbeat_ns`，一旦 Python Agent 异常退出或超时未续租，自动卸载 SCX 恢复内核默认调度（`kernel.default`）。

### 3.2 多维感知与双模式控制（`oeagent/`）
- **真实调度等待感知（`schedstats`）**：启用 `kernel.sched_schedstats=1`，[`WorkloadObserver`](../oeagent/observe.py) 直接读取 `/proc/<pid>/task/<tid>/schedstat` 计算 `run_wait_fraction`（运行队列排队等待占比）与上下文切换速率。
- **行为规则模式（`nginx-cpu`）**：当同时存在高频自愿切换/高调度等待的 `latency` 任务与高 CPU 占用的 `batch` 任务时，经连续窗口确认切入 `1ms` SCX；当批处理退出只剩单服务时，自动回退 `kernel.default`，避免纯服务场景下的额外上下文切换开销。
- **阶段二连续窗口 SLO 反馈模式（`nginx-slo` / `nginx-wakeup-slo`）**：
  - 配合定制版 `wrk2`（`--windowed`），在不重启客户端连接的情况下每 5 秒流式输出校准后的 HDR 延迟分布（`wrk2.hdr.window`）。
  - 实现 [`align_background_intervals`](../oeagent/business.py) 边界裁剪对齐算法，消除 5 秒窗口跨越 1 秒 `sysbench` 报告边界导致的覆盖率损失。
  - [`SloGuard`](../oeagent/feedback.py) 支持**多窗口基线稳定性确认（`baseline_confirmations`）与中位数聚合**、**排队积压追赶过滤（`baseline_backlog_draining`）**，以及探测后对前台完成率、吞吐比例、后台 EPS 比例的硬约束校验与不达标自动回退冷却。

### 3.3 受管 Cgroup 归组与 eBPF 网络/安全扩展插件（`oeagent/plugins.py`）
- **`resource.cgroup` 插件**：支持创建受管 cgroup v1 组、按预期旧值修改 `cpu.shares`、核对 `(pid, start_ticks)` 身份后将进程迁入受管组（`resource.cgroup.attach`）以及安全迁回根组（`resource.cgroup.detach`）。
- **`network.ebpf_tc` 插件**：基于 [oe_net_hook.bpf.c](../native/oe_net_hook.bpf.c) 实现内核 `sched_cls` eBPF 分类器，挂载于 `tc clsact egress`，支持目标端口实时包数/字节统计与在线开启端口级丢包隔离（`TC_ACT_SHOT`）。
- **`security.lsm` 插件**：自动发现内核活跃 LSM 模块栈（`lockdown, capability, yama, apparmor, bpf`），审计目标进程的 Capability 位图（`CAP_SYS_ADMIN` / `CAP_NET_ADMIN`）、`NoNewPrivs`、`Seccomp` 与 cgroup 边界。

---

## 4. 实验验证与实测结果汇总

### 4.1 确定性单元测试与原生/插件功能验证
| 验证项 | 验证脚本 / 入口 | 核心结果 |
|---|---|---|
| 确定性单元测试 | `python3 -m unittest discover -s tests -v` | **101 项测试全部通过**（协议、路由、直方图、SLO 守卫、多窗口基线、积压过滤、缓存、宿主/来宾基线契约） |
| 原生 SCX 生命周期 | `python3 experiments/check_native.py` | **8/8 通过**（加载回读、在线改片、旧版本拒绝、越界拒绝、错误 Map ID 拒绝、心跳、停止、失联自动卸载） |
| 唤醒抢占专家验证 | `python3 experiments/check_wakeup.py` | **通过**（真实 Nginx+sysbench 混部下在线关闭→开启→关闭抢占，`kick_preempt` 触发计数与回退核对一致） |
| Cgroup 归组与争用隔离 | `python3 experiments/check_cgroup_plugin.py` | **通过**（PID/启动时间核对迁入；同核 1024 vs 4096 权重争用实测 CPU ticks 比例 **3.96 : 1**，理论 4.0 : 1） |
| eBPF 网络与 LSM Hook | `python3 experiments/check_ebpf_hooks.py` | **通过**（观测模式准确计数 60 包/5255 字节；隔离模式 `TC_ACT_SHOT` 拦截目标端口且不影响旁路端口；卸载立即恢复） |

### 4.2 Hyper-V 阶段一：默认调度定档（Survey）与小规模对照（Pilot）
在 Hyper-V 来宾（CPU0 绑定 Nginx + sysbench，CPU1 绑定窗口版 wrk2，CPU2 绑定 Agent 控制器）完成 Survey 定档（mixed 参考容量 64,000 RPS，high 档 **57,600 RPS**）及 6 组 Pilot 对照（详见 [baseline-measurement.md](baseline-measurement.md)）：

| 策略条件 | 场景 | 计划速率 (RPS) | 实测吞吐 (RPS) | 尾延迟 p99 (ms) | 状态转移与行为观测 |
|---|---|---|---|---|---|
| `default` | Mixed 稳态 | 57,600 | 57,604.1 | 10.807 ms | 保持默认调度 |
| `default` | Dynamic 三阶段 | 57,600 | 57,577.7 | 580.607 ms | 混合阶段发生排队积压（`completion_out_of_range`） |
| `fixed-1000` | Mixed 稳态 | 57,600 | 55,116.0 | 10477.567 ms | 固定 1ms 稳态出现积压 |
| `fixed-1000` | Dynamic 三阶段 | 57,600 | 57,599.7 | 4.839 ms | 全程固定 `scx.slice` 1000 µs |
| **`adaptive` (Agent)** | **Mixed 稳态** | **57,600** | **57,600.2** | **3.309 ms (-69.4%)** | **自动检测争用并切入 `1000 µs` SCX，无积压** |
| **`adaptive` (Agent)** | **Dynamic 三阶段** | **57,600** | **57,600.2** | **3.169 ms (-99.5%)** | **后台加入切入 `1000 µs`，后台退出自动切回 `kernel.default`** |

### 4.3 阶段二：连续窗口在线业务 SLO 反馈闭环（`check_slo_continuous.py`）
在 25,600 RPS 连续窗口 `wrk2` 流式输入下，验证了 `nginx-wakeup-slo`（2 窗口基线确认 + 边界裁剪后台对齐 + `1ms` SCX 唤醒抢占探测，目标 p99 ≤ 2.5ms）的完整在线闭环（原始证据位于 `state/slo-continuous-validation.json`）：

| 连续窗口序号 | 当前策略 (Revision) | 前台 RPS | 前台 p99 (ms) | 后台 EPS | 控制器决策与状态转移 |
|---|---|---|---|---|---|
| Window #1 | `kernel.default` (rev 0) | 25,600.3 | 3.499 ms | 3,262.2 | `hold` (`confirming_baseline_stability`, 1/2) |
| Window #2 | `kernel.default` (rev 0) | 25,600.6 | **3.505 ms** | 3,256.2 | `apply` (`baseline_business_goal_violated` → **切入 `scx.slice` 1000µs + 唤醒抢占**) |
| Window #3 | 跨越切换边界 | — | — | — | `skipped_policy_transition`（自动过滤跨策略动作窗口） |
| Window #4 | `scx.slice` (rev 1) | 25,601.4 | **1.987 ms (-43.3%)** | 3,234.4 (-0.76%) | `hold` (`business_guard_accepted_candidate`, **SLO 达标 `accepted`**) |
| Window #5 | `scx.slice` (rev 1) | 25,599.5 | **1.984 ms (-43.3%)** | 3,252.8 (-0.19%) | `hold` (`business_guard_accepted_candidate`, `accepted`) |
| Window #6 | `scx.slice` (rev 1) | 25,598.6 | **1.964 ms (-43.9%)** | 3,262.3 (+0.10%) | `hold` (`business_guard_accepted_candidate`, `accepted`) |

- **控制与测量开销剥离**：全程 48 秒运行期间，Python Agent 控制线程累计 CPU 耗时仅 **`0.342 s`（约占单核 0.7%）**；`wrk2` 全部窗口直方图流式导出总耗时仅 **`2.048 ms`（`2,048,206 ns`）**。

### 4.4 对标开题报告第 10 页：完整 6 策略 × 4 场景消融对照实验（`run_ppt_matrix.py`）
严格按照开题答辩 PPT 第 10 页（《最终性能对照实验设计》）定义的 **6 个对照组**（默认调度、原始 `scx_simple`、固定 `20ms`、固定 `1ms` 无抢占、固定 `1ms + 唤醒抢占`、自适应 `Agent`）与 **4 类测试场景**（纯网页、网页加后台混部、动态负载切换、纯后台计算），在 `25,600 RPS` 连续窗口 `wrk2`（5s/窗）与 `CPU0` 同核混部下完成统一对比（原始数据保存在 [`state/ppt-matrix-validation.json`](../state/ppt-matrix-validation.json)）：

| 策略对照组 | 场景 1：纯网页 (`service_only`)<br>`p99` / 排队占比 / 激活策略 | 场景 2：网页加后台 (`mixed`)<br>`p99` / 单次唤醒等待 / 后台 `EPS` | 场景 3：动态负载 (`dynamic`)<br>混部前 `p99` → 混部稳态 `p99` → 恢复过渡 `p99` → 恢复后 `p99` | 场景 4：纯后台 (`batch_only`, 2线程)<br>后台 `EPS` (保留率) / 非自愿切换 `/s` / 激活策略 |
|---|---|---|---|---|
| **1. 默认调度 (`default`)** | `1.919 ms`<br>排队 `0.2%` (`disabled`) | `3.509 ms` *(基准)*<br>等待 `1535.2 µs` \| `3187.6 EPS` | `1.919 ms` → `3.509 ms` → `2.479 ms` → `1.934 ms`<br>*(全程 `kernel.default`)* | `4005.1 EPS` (`100.0%`)<br>切换 `333.7 /s` (`disabled`) |
| **2. 原始 `scx_simple`** | `1.904 ms`<br>排队 `0.6%` (`enabled`) | `43.103 ms` *(+1128% 劣化)*<br>等待 `19461.9 µs` \| `3188.3 EPS` | `1.904 ms` → `43.103 ms` → `20.367 ms` → `1.928 ms`<br>*(20ms 时间片导致严重积压)* | `3976.9 EPS` (`99.3%`)<br>切换 `47.9 /s` (`enabled`) |
| **3. 固定 SCX (`20ms`)** | `1.936 ms`<br>排队 `0.8%` (`enabled`) | `42.367 ms` *(+1107% 劣化)*<br>等待 `19339.0 µs` \| `3175.6 EPS` | `1.936 ms` → `42.367 ms` → `20.991 ms` → `1.943 ms`<br>*(与 `scx_simple` 表现一致)* | `3866.9 EPS` (`96.6%`)<br>切换 `48.2 /s` (`enabled`) |
| **4. 固定 SCX (`1ms` 无抢占)** | `1.953 ms`<br>排队 `0.8%` (`enabled`) | `2.717 ms` *(-22.6%)*<br>等待 `637.4 µs` \| `3153.0 EPS` | `1.953 ms` → `2.717 ms` → `2.045 ms` → `1.938 ms`<br>*(缩短时间片但无唤醒抢占)* | `3982.2 EPS` (`99.4%`)<br>切换 **`534.7 /s` (+60%)** (`enabled`) |
| **5. 固定 SCX (`1ms+唤醒抢占`)** | `1.922 ms`<br>排队 `1.1%` (`enabled`) | **`2.010 ms` (-42.7%)**<br>等待 **`18.8 µs`** \| `3160.4 EPS` | `1.922 ms` → `2.010 ms` → `1.912 ms` → `1.918 ms`<br>*(混部优秀，但单负载下常驻 SCX)* | `3943.1 EPS` (`98.5%`)<br>切换 **`532.9 /s` (+60%)** (`enabled`) |
| **6. 自适应 Agent (`adaptive`)** | **`1.942 ms`**<br>排队 **`0.2%` (`disabled`)** | **`1.982 ms` (-43.5% 全场最低)**<br>等待 **`23.3 µs`** \| `3098.8 EPS (97.2%)` | **`1.942 ms` → `1.982 ms` → `1.929 ms` → `1.932 ms`**<br>*(切入 `1.08s`，回退 `0.69s`，自动恢复 `disabled`)* | `3897.5 EPS` (`97.3%`)<br>切换 **`333.4 /s` (零额外切换)** (`disabled`) |

**6 × 4 矩阵核心结论**：
1. **为什么原始 `scx_simple` 与 `fixed-20ms` 无法用于混部？** 在 20ms 默认时间片下，Nginx 一旦睡眠让出 CPU，必须等待后台 `sysbench` 跑完近 20ms（实测单次唤醒等待 `19.4 ms`），导致混部 `p99` 飙升至 **`43.1 ms`**。
2. **为什么仅靠 `fixed-1ms` 还不够？** 将时间片缩至 `1ms`（无抢占）可将单次唤醒等待从 `1535 µs` 压至 `637 µs`、`p99` 降至 `2.717 ms`，但仍无法突破 `2.5 ms` SLO；加上**短睡眠唤醒抢占与 `vtime` 置顶信用（`1ms + 唤醒抢占`）**后，单次唤醒等待骤降至 **`18.8 ~ 23.3 µs`（降低 66 倍）**，`p99` 降至 **`1.982 ~ 2.010 ms`（基本抹平混部干扰，接近纯网页无干扰时的 `1.919 ms`！）**。
3. **为什么需要 `adaptive` 自适应 Agent 而不是永久开启 `fixed-1ms-preempt`？** 在 **场景 1（纯网页）** 和 **场景 4（纯后台）** 中，`fixed-1ms-preempt` 依然常驻内核，导致纯后台双线程 `sysbench` 每秒多发生 **60% 的非自愿抢占上下文切换（`532.9 /s` vs `333.4 /s`）**；而 **`adaptive` Agent 在纯网页与纯后台下保持 `kernel.default`（`sched_ext=disabled`），仅在混部冲突发生后 `1.08 秒` 内自动切入 `1ms + 唤醒抢占`，并在后台退出后 `0.69 秒` 内自动卸载回退**，兼顾了混部极致低延迟与单负载零额外损耗。

### 4.5 多维内核资源协同自治闭环实验（`check_multi_resource_auto.py`）
为验证 Agent 不仅能自动调节 CPU 调度策略，还能根据实时运行态势**自动协同控制 Cgroup 资源隔离、eBPF TC 网络钩子与 LSM 安全审计**，我们在 [`oeagent/plugins.py`](../oeagent/plugins.py) 中实现了 `AutoPluginCoordinator` 并将其集成至 [`AgentRuntime.run()`](../oeagent/runtime.py)，使用配置 [`profiles/nginx-multi-auto.json`](../profiles/nginx-multi-auto.json) 进行了 5 阶段全自动态势切换实验（完整证据保存在 [`state/multi-resource-auto-validation.json`](../state/multi-resource-auto-validation.json)）：

| 实验阶段与动态工况 | `sched_ext` CPU 自动决策 | `resource.cgroup` 自动决策 | `network.ebpf_tc` 自动决策 | `security.lsm` 审计与实测验证结果 |
|---|---|---|---|---|
| **阶段 1：纯前台服务 (`service_only`)**<br>仅 Nginx 运行，无后台争抢，端口流量正常 | 保持 `kernel.default` (`disabled`) | 保持未隔离 (`active=false`，避免单负载额外开销) | **自动在 `lo` 挂载观测模式** (`target_port=18095`, `drop_target=false`) | **自动审计 Nginx 进程** (`lsm_stack=[lockdown,capability,yama,apparmor,bpf]`)；端口探测 `HTTP 200 OK` (`0.484 ms`) |
| **阶段 2：注入后台算力争抢 (`mixed`)**<br>CPU0 启动 `sysbench` 与 Nginx 混部争抢 | **自动切入 `scx.slice`** (`1000µs + wakeup_preempt`) | **自动创建并迁入受管组**<br>`auto_latency=4096` vs `auto_batch=1024` | 持续监测包速 (`0.0 pps < 20.0 pps`，保持放行) | **自动审计新进程 `sysbench`**；在 SCX 运行期核实双组 `cgroup.procs` PID 归属生效 |
| **阶段 3：受控端口突发洪泛 (`port_flood`)**<br>向 `18095` 端口注入突发高频请求 (`315.3 pkt/s`) | 维持 `scx.slice` 保护前台 Nginx | 维持 `4096 : 1024` Cgroup 隔离 | **检测到 `315.3 pps >= 20.0 pps` → 自动下发 `drop_target=true` (`TC_ACT_SHOT`)** | **`18095` 端口探测被内核直接丢包阻断** (`dropped_packets` 递增)，同时前台 Nginx (`18086`) 返回 `200 OK` 零受影响 |
| **阶段 4：洪泛平息 & 争抢退出 (`recovery`)**<br>端口冲击停止 + `sysbench` 退出 + Agent 停止 | **自动卸载回退 `kernel.default`** (`disabled`) | **自动解绑释放**，将 Nginx 迁回根 Cgroup (`procs=[]`) | **冷却 2 窗后自动恢复 `drop_target=false` (`TC_ACT_OK`)**，Agent 退出时自动卸载钩子 | **`18095` 端口自动恢复连通** (`HTTP 200 OK`, `0.435 ms`)；退出后零内核残留状态，控制线程 CPU 占比仅 `0.52%` |
| **阶段 5：默认调度下 Cgroup 自动赋权实测**<br>`mode=default` 同核争抢自动隔离效果 | 固定 `kernel.default` (CFS) | **自动识别同核争抢并下发 `4096 : 1024` 权重** | 自动挂载观测模式 | 实测前台 `latency` 与后台 `batch` CPU 时间占比达 **`79.8% vs 20.2%`（`3.94 : 1`，高度吻合理论 `4.0 : 1`）** |

---

## 5. 一键演示说明（课程答辩 Demo）

在 openEuler 虚拟机 `/root/oe-resource-agent` 目录下执行一条命令即可完成全功能现场演示：

```bash
cd /root/oe-resource-agent
python3 experiments/run_demo.py
```

该脚本自动依次演示并打印：
1. **内核能力、真实 `schedstats` 调度等待与 BPF LSM 安全审计**；
2. **受管 Cgroup 进程身份核对迁入与 1:4 CPU 权重争用隔离实测**；
3. **eBPF TC 网络 Hook 流量统计、在线切换端口丢包隔离与卸载恢复**；
4. **自适应 `sched_ext` 负载感知、在线切入 SCX、BPF Map 无感调参、唤醒抢占与后台退出自动回退默认调度**；
5. **连续窗口业务 SLO 闭环性能与控制开销汇总**（结果同步保存在 `state/demo-report.json`）。
