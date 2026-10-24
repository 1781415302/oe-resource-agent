# openEuler 自适应资源管控 Agent

版本 0.1.3，GPL-2.0。面向 **openEuler 24.03 LTS-SP4（x86_64，自建内核 `6.6.0-scx`）** 的多维内核资源自治管控系统，协同控制 **`sched_ext` 可编程 CPU 调度器**、**Cgroup v1 CPU 权重隔离**、**eBPF TC 网络流量观测与熔断** 以及 **BPF LSM 进程安全审计**。

---

## 核心能力

- **真实内核与业务双源感知**：采集受控进程真实 CPU 使用率、自愿/非自愿上下文切换率、CPU 亲和性、`/proc/<pid>/schedstat` 调度排队等待占比（`run_wait_fraction`），以及 `wrk2` 连续时间窗口 HDR 延迟直方图（`p50/p95/p99`）与 `sysbench` 边界对齐后台吞吐（`EPS`）。
- **自适应 `sched_ext` 调度与唤醒抢占优化**：
  - 在检测到 `latency`（如 Nginx）与 `batch`（如 sysbench）同核争抢时自动切入 `scx.slice`，在单负载（纯网页或纯后台）时保持或自动回退 `kernel.default`（零额外调度开销）；
  - 运行期通过 BPF Map（`oe_adaptive_ctl_map`）零重启热更新时间片与唤醒抢占参数，核对 `map_id`、`generation` 并内置 15s 看门狗心跳失联自动卸载；
  - 针对短突发唤醒任务实现 **`vtime` 队列置顶信用补偿（`vtime = vtime_now - slice`）**、**解耦突发判定窗口（`>= 750µs`）**、**即时忙态标记（`busy_runs >= 1`）** 与 **`50µs` 抢占节流间隔**，将混部下 Nginx 单次唤醒排队等待从 `1535.2 µs` 压降至 **`18.8 ~ 23.3 µs`（降低 66 倍）**。
- **多维插件自动闭环协同（`AutoPluginCoordinator`）**：
  - **`security.lsm` 自动安全审计**：发现新业务进程 `(pid, start_ticks)` 时自动核验内核 LSM 栈（`lockdown, capability, yama, apparmor, bpf`）、进程 Capability 掩码与 Seccomp 状态；
  - **`resource.cgroup` 自动分级隔离**：检测到混合算力争抢时自动创建受管组 `auto_latency`（`cpu.shares=4096`）与 `auto_batch`（`cpu.shares=1024`）并安全迁入进程（实测 CFS 下单核算力分配达 **`3.97 : 1` / `79.9% vs 20.1%`**，SCX 下保留组归属），争抢消除后自动迁回根 Cgroup；
  - **`network.ebpf_tc` 自动流量护航与端口熔断**：基于 `native/oe_net_hook.bpf.c` 在 `lo` egress 挂载 `sched_cls` 分类器实时统计端口包速率，检测到异常高频流量冲击（`observed_pps >= drop_threshold_pps`）时**自动下发 `drop_target=true`（内核态 `TC_ACT_SHOT` 零拷贝丢包熔断）**，流量恢复并度过冷却窗口后自动恢复 `TC_ACT_OK` 放行。
- **版本化 JSON 工具、Skills 与真机实时交互沙盒**：提供标准化 Schema 校验工具、持久 JSON 行控制会话（`./agent serve`）以及带双通道实时示波器的 Web 交互大屏（`experiments/web_dashboard.py`）。

---

## 实验结果汇总

完整实验设计与数据详见 **[课程设计总结报告](docs/course-project-report.md)**：

### 1. 开题报告第 10 页：6 策略 × 4 场景完整消融矩阵（`experiments/run_ppt_matrix.py`）
在 `25,600 RPS` 连续窗口 `wrk2` 与 `CPU0` 同核混部下实测（原始数据：`state/ppt-matrix-validation.json`）：

| 对照组策略 | 场景 1：纯网页 (`service_only`) | 场景 2：网页加后台 (`mixed`) | 场景 3：动态负载 (`dynamic` 四阶段 `p99`) | 场景 4：纯后台 (`batch_only`, 2线程) |
|---|---|---|---|---|
| **1. 默认调度 (`default`)** | `1.919 ms` \| 排队 `0.2%` (`disabled`) | `3.509 ms` \| 等待 `1535.2 µs` \| `3187.6 EPS` | `1.919 → 3.509 → 2.479 → 1.934 ms` | `4005.1 EPS` \| 切换 `333.7/s` (`disabled`) |
| **2. 原始 `scx_simple`** | `1.904 ms` \| 排队 `0.6%` (`enabled`) | `43.103 ms (+1128%)` \| 等待 `19461.9 µs` | `1.904 → 43.103 → 20.367 → 1.928 ms` | `3976.9 EPS` \| 切换 `47.9/s` (`enabled`) |
| **3. 固定 SCX (`20ms`)** | `1.936 ms` \| 排队 `0.8%` (`enabled`) | `42.367 ms (+1107%)` \| 等待 `19339.0 µs` | `1.936 → 42.367 → 20.991 → 1.943 ms` | `3866.9 EPS` \| 切换 `48.2/s` (`enabled`) |
| **4. 固定 SCX (`1ms` 无抢占)** | `1.953 ms` \| 排队 `0.8%` (`enabled`) | `2.717 ms (-22.6%)` \| 等待 `637.4 µs` | `1.953 → 2.717 → 2.045 → 1.938 ms` | `3982.2 EPS` \| **切换 `534.7/s (+60%)`** |
| **5. 固定 SCX (`1ms+唤醒抢占`)** | `1.922 ms` \| 排队 `1.1%` (`enabled`) | **`2.010 ms (-42.7%)`** \| 等待 **`18.8 µs`** | `1.922 → 2.010 → 1.912 → 1.918 ms` | `3943.1 EPS` \| **切换 `532.9/s (+60%)`** |
| **6. 自适应 Agent (`adaptive`)** | **`1.942 ms` \| 排队 `0.2%` (`disabled`)** | **`1.982 ms (-43.5% 最优)`** \| 等待 **`23.3 µs`** \| `97.2% EPS` | **`1.942 → 1.982 → 1.929 → 1.932 ms`** (切入`1.08s`/回退`0.69s`) | **`3897.5 EPS` \| 切换 `333.4/s` (`disabled`)** |

### 2. 多维资源协同自治闭环实验（`experiments/check_multi_resource_auto.py`）
使用 [`profiles/nginx-multi-auto.json`](profiles/nginx-multi-auto.json) 验证 Agent 根据运行态势自动控制全部子系统（原始数据：`state/multi-resource-auto-validation.json`）：
- **阶段 1（纯前台服务）**：保持 `kernel.default` 与 Cgroup 未隔离，**自动审计 Nginx 进程**并**自动在 `lo` 挂载 eBPF TC 观测模式**（`18095` 端口探测 `200 OK`）；
- **阶段 2（注入后台算力争抢）**：**自动审计新进程 `sysbench`**、**自动创建并迁入 `4096:1024` 受管 Cgroup**、**自动切入 `scx.slice (1ms + 唤醒抢占)`**；
- **阶段 3（受控端口突发洪泛）**：检测到 `18095` 端口速率达 `315.3 pkt/s >= 20 pkt/s`，**自动切换 `drop_target=true`（`TC_ACT_SHOT`）阻断异常端口请求**，同时前台 Nginx（`18086`）`200 OK` 零受影响；
- **阶段 4（洪泛平息与争抢退出）**：冷却 2 窗后**自动恢复端口放行（`TC_ACT_OK`）**；`sysbench` 退出后**自动回退 `kernel.default` 并自动将 Nginx 迁回根 Cgroup**，控制线程 CPU 占比仅 **`0.52%`**；
- **阶段 5（CFS 默认调度下 Cgroup 自动赋权）**：在 `mode=default` 下自动识别同核争抢并下发 `4096:1024` 权重，实测前台算力占比提升至 **`79.9% vs 20.1%`（`3.97 : 1`）**。

---

## 快速开始与部署

在 openEuler 24.03 LTS-SP4（`6.6.0-scx`）环境中：

```bash
cd /root/oe-resource-agent
bash build.sh
./agent capabilities
./agent tools list
```

### 运行多维资源自动闭环 Agent

```bash
# 仅 CPU 调度自适应模式
./agent run --profile nginx-cpu --duration 60

# CPU (SCX) + Cgroup + eBPF TC + LSM 多维协同自动闭环模式
./agent run --profile nginx-multi-auto --duration 60

# 业务 SLO 连续窗口反馈闭环模式
./agent run --profile nginx-wakeup-slo --duration 60
```

### 启动真机实时交互沙盒与双通道示波器大屏

```bash
python3 experiments/web_dashboard.py --host 0.0.0.0 --port 8899
```

浏览器访问 `http://<虚拟机IP>:8899/`（如 `http://172.26.1.3:8899/`），即可在线操控真实 `Nginx + wrk2 (25.6k RPS)`、`sysbench` 干扰注入、`SCX` BPF Map 热更新、25 秒一键全自动实况剧本、Cgroup 权重争用测试以及 eBPF 端口突发洪泛自动熔断。

---

## 验证与实验脚本清单

```bash
# 1. 离线确定性协议、策略、度量与多维自动协调器单元测试（103 项）
python3 -m unittest discover -s tests -v

# 2. 各子系统与端到端闭环真机验证
python3 experiments/check_native.py
python3 experiments/check_workload_loop.py
python3 experiments/check_control_session.py
python3 experiments/check_cgroup_plugin.py
python3 experiments/check_ebpf_hooks.py
python3 experiments/check_slo_continuous.py
python3 experiments/check_multi_resource_auto.py

# 3. 开题报告 6 策略 × 4 场景完整消融矩阵实验
python3 experiments/run_ppt_matrix.py

# 4. 课程答辩命令行一键全流程演示
python3 experiments/run_demo.py
```

---

## 相关文档导航

- [课程设计总结报告（含架构、优化细节、6×4 矩阵与多维闭环实测）](docs/course-project-report.md)
- [真机实时交互沙盒与可视化大屏](docs/demo-dashboard.html)
- [唤醒抢占专家与 BPF 调度优化说明](docs/wakeup-preemption.md)
- [在线业务 SLO 反馈机制说明](docs/business-feedback.md)
- [连续窗口基线测量规范](docs/baseline-measurement.md)
- [openEuler 6.6.0-scx 环境复现指南](docs/reproduce-openeuler.md)
