# openEuler 自适应资源管控 Agent

版本 0.2.0，GPL-2.0。面向 **openEuler 24.03 LTS-SP4（x86_64，自建内核 `6.6.0-scx`）** 的多维内核资源自治管控系统，协同控制 **`sched_ext` 可编程 CPU 调度器**、**Cgroup v1 CPU 权重隔离**、**eBPF TC 网络流量观测与熔断** 以及 **BPF LSM 安全审计与策略限制（`security.policy.enforce`）**。

---

## 核心能力

- **真实内核与业务双源感知**：采集受控进程真实 CPU 使用率、自愿/非自愿上下文切换率、CPU 亲和性、`/proc/<pid>/schedstat` 调度排队等待占比（`run_wait_fraction`），以及 `wrk2` 连续时间窗口 HDR 延迟直方图（`p50/p95/p99`）与 `sysbench` 边界对齐后台吞吐（`EPS`）。
- **自适应 `sched_ext` 调度与唤醒抢占优化**：
  - 在检测到 `latency`（如 Nginx）与 `batch`（如 sysbench）同核争抢时自动切入 `scx.slice`，在单负载（纯网页或纯后台）时保持或自动回退 `kernel.default`（零额外调度开销）；
  - 运行期通过 BPF Map（`oe_adaptive_ctl_map`）零重启热更新时间片与唤醒抢占参数，核对 `map_id`、`generation` 并内置 15s 看门狗心跳失联自动卸载；
  - 针对短突发唤醒任务实现 **`vtime` 队列置顶信用补偿（`vtime = vtime_now - slice`）**、**解耦突发判定窗口（`>= 750µs`）**、**即时忙态标记（`busy_runs >= 1`）** 与 **`50µs` 抢占节流间隔**，将混部下 Nginx 单次唤醒排队等待从 `1497.6 µs` 压降至 **`19.7 µs`（降低 76 倍，降幅 98.7%）**。
- **多维插件自动闭环协同（`AutoPluginCoordinator`）**：
  - **`security.lsm` 安全审计与策略执行（`security.policy.enforce`）**：自动发现内核活跃 LSM 栈（`lockdown, capability, yama, apparmor, bpf`），审计进程安全上下文；合法混部任务（如 `sysbench`）保留同核混部，而非授权异常进程则强制压制优先级（`nice=19`）并驱逐至隔离核心（`CPU3`）；
  - **`resource.cgroup` 自动分级隔离**：检测到混合算力争抢时自动创建受管组 `auto_latency`（`cpu.shares=4096`）与 `auto_batch`（`cpu.shares=1024`）并安全迁入进程（EEVDF 默认公平调度下动态混部实测算力分配达 **`3.97 : 1` / `79.9% vs 20.1%`**，独立纯 CPU 争用单测实测达 **`3.96 : 1` / `79.8% vs 20.2%`**，SCX 下保留组归属），争抢消除后自动迁回根 Cgroup；
  - **`network.ebpf_tc` 自动流量护航与端口熔断**：基于 `native/oe_net_hook.bpf.c` 在 `lo` 网卡挂载 `sched_cls` 分类器实时统计端口速率，检测到异常高频流量冲击时**在内核 TC（qdisc/cls_bpf）层直接执行 `TC_ACT_SHOT` 提前丢弃报文**，避免其进入传输层或争抢 CPU0，冷却后自动恢复放行；内置业务白名单（`80, 443, 18080, 18086, 18091`）拒绝阻断核心端口。
- **版本化 JSON 工具、Skills 与真机实时交互沙盒**：提供标准化 Schema 校验工具、持久 JSON 行控制会话（`./agent serve`）以及带双通道实时示波器的 Web 交互大屏（`experiments/web_dashboard.py`）。

---

## 实验结果汇总

完整实验设计、负载强度梯度与多资源定量消融数据详见 **[课程设计总结报告](docs/course-project-report.md)** 与 **[交互式 HTML 实验报告](docs/experiment-report.html)**：

### 1. 开题报告第 10 页：6 策略 × 4 场景多周期平衡消融矩阵（`experiments/run_rigorous_suite.py`）
在 `25,600 RPS` 连续窗口 `wrk2`（`OE_WRK2_WINDOW_MS=5000`）与 `CPU0` 同核混部下，采用 **5 周期独立运行与拉丁方平衡旋转顺序（N=5 周期，Mean ± SD）** 实测（原始数据：[`state/rigorous-suite-validation.json`](state/rigorous-suite-validation.json)，报告：[`docs/experiment-report.html`](docs/experiment-report.html)）：

| 对照组策略 | 场景 1：纯网页 (`service_only`)<br>p99 / RPS保留率 / 排队等待 | 场景 2：网页加后台 (`mixed`)<br>p99 / RPS & EPS保留率 / 等待 | 场景 3：动态负载 (`dynamic` 四阶段 Mean±SD)<br>前稳态 → 混部稳态 → 恢复过渡 → 后稳态 | 场景 4：纯后台 (`batch_only`, 2线程)<br>后台 EPS (保留率) / 非自愿切换 /s |
|---|---|---|---|---|
| **1. 默认调度 (`default`)** | `1.932 ± 0.012 ms`<br>RPS保留 `100.0%` \| 等待 `0.4 µs` | `3.514 ± 0.015 ms`<br>RPS `100.01%` \| EPS `100.34%` \| 等待 `1497.6 µs` | `(1.932±0.012) → (3.514±0.015) → (2.412±0.035) → (1.942±0.010) ms`<br>*(全程 `kernel.default`)* | `3882.7 ± 25.1 EPS` (`100.0%`)<br>切换 `334.3 /s` (`disabled`) |
| **2. 原始 `scx_simple`** | `1.921 ± 0.013 ms`<br>RPS保留 `100.0%` \| 等待 `1.7 µs` | `35.193 ± 4.716 ms (+901%)`<br>RPS `99.98%` \| EPS `101.72%` \| 等待 `19340.4 µs` | `(1.921±0.013) → (35.193±4.716) → (19.580±1.104) → (1.918±0.012) ms`<br>*(20ms 粗片引发严重排队积压)* | `3875.7 ± 32.9 EPS` (`99.82%`)<br>切换 `48.3 /s` (`enabled`) |
| **3. 固定 SCX (`20ms`)** | `1.942 ± 0.006 ms`<br>RPS保留 `100.01%` \| 等待 `1.7 µs` | `74.380 ± 51.548 ms (+2016%)`<br>RPS `100.11%` \| EPS `100.66%` \| 等待 `19351.1 µs` | `(1.942±0.006) → (74.380±51.548) → (26.890±8.354) → (1.942±0.006) ms`<br>*(大方差，排队等待突破 19ms)* | `3851.4 ± 58.6 EPS` (`99.19%`)<br>切换 `48.5 /s` (`enabled`) |
| **4. 固定 SCX (`1ms` 无抢占)** | `1.949 ± 0.010 ms`<br>RPS保留 `100.0%` \| 等待 `1.9 µs` | `2.796 ± 0.061 ms (-20.4%)`<br>RPS `100.0%` \| EPS `97.31%` \| 等待 `654.7 µs` | `(1.949±0.010) → (2.796±0.061) → (2.037±0.012) → (1.940±0.003) ms`<br>*(缩短时间片但无抢占，仍有半片等待)* | `3885.3 ± 44.2 EPS` (`100.07%`)<br>切换 **`543.1 /s` (+62.5%)** (`enabled`) |
| **5. 固定 SCX (`1ms+唤醒抢占`)** | `1.919 ± 0.006 ms`<br>RPS保留 `100.0%` \| 等待 `2.0 µs` | **`2.037 ± 0.068 ms` (-42.0%)**<br>RPS `100.0%` \| EPS `97.44%` \| 等待 **`19.8 µs`** | `(1.919±0.006) → (2.037±0.068) → (1.920±0.011) → (1.923±0.008) ms`<br>*(混部极优，但常驻内核增加后台开销)* | `3888.9 ± 29.3 EPS` (`100.16%`)<br>切换 **`541.1 /s` (+61.9%)** (`enabled`) |
| **6. 自适应 Agent (`adaptive`)** | **`1.949 ± 0.024 ms`**<br>RPS `100.01%` \| 等待 **`0.4 µs`** | **`2.020 ± 0.048 ms` (-42.5% 最优)**<br>RPS `100.0%` \| EPS `97.78%` \| 等待 **`19.7 µs`** | **`(1.949±0.024) → (2.020±0.048) → (1.930±0.008) → (1.936±0.007) ms`**<br>*(切入 `805.5 ± 42.1 ms`，回退 `565.4 ± 35.8 ms`)* | **`3878.2 ± 27.4 EPS` (`99.88%`)**<br>切换 **`333.8 /s` (零额外切换)** (`disabled`) |

> **开题指标全面达标**：混部场景下自适应 Agent 实现 **`网页 RPS 保留率 100.0%`（≥90%）**、**`后台 EPS 保留率 97.78%`（≥90%）**、**`请求错误率 0.00%`**，单次唤醒排队等待从默认调度的 `1497.6 µs` 骤降至 **`19.7 µs`（压降 76 倍）**，且纯后台零额外上下文切换开销。

### 2. 多资源闭环定量 A/B 消融对照（`experiments/run_split_ablation.py` 与 `experiments/run_rigorous_suite.py`）
- **Cgroup (4096:1024) 与 nice 细粒度消融**：在 EEVDF 默认调度下发生同核争抢时，单靠 Cgroup 宏观赋权将单次平均等待从 `1556.0 µs` 压降至 **`1026.7 µs`（-34.0%）**；但由于 EEVDF 依据虚拟截止期（deadline）分时且无微秒级即时抢占，尾延迟 p99 仍为 `3.846 ms`（单靠 nice 15 为 `4.216 ms`，双重降权为 `1073.8 µs / 3.913 ms`）；而全栈多资源自治（SCX 1ms+抢占 + Cgroup + LSM）介入后，单次等待骤降至 **`26.8 µs`（压降 58 倍）**，`p99` 降至 **`2.131 ms`**（独立细粒度消融重测，与主矩阵测得的 19.7µs / 2.020ms 处于同一微秒级水平），实验数据证实了宏观份额保障与微观内核即时抢占的互补必要性。
- **eBPF TC 端口自动熔断**：旁路攻击靶点（执行 40,000 轮自旋计算的辅助服务）遭遇并发洪泛冲击时，未熔断时请求穿透 TCP 栈并在 CPU0 耗尽算力，前台排队等待达 `1541.4 µs`（占比 79.5%）、`p99` 恶化至 `3.559 ms`；开启 eBPF `TC_ACT_SHOT` 后**在内核 TC（qdisc/cls_bpf）层提前丢弃 972 个异常 TCP 报文（SYN 建立被阻断）**，靶点服务在 5 秒内处理 0 个 HTTP 请求且处于 `epoll_wait` 空闲休眠（CPU 占比 0.0%），前台排队等待降至 **`0.4 µs`（排队基本归零）**，前台 `p99` 恢复至 **`1.920 ms`**。
- **安全策略与白名单防误伤**：`security.policy.enforce` 自动对非授权竞争进程执行安全约束（`nice` 降为 19 并**驱逐至隔离核心 CPU3**）；内置白名单坚决拒绝阻断 `80/18086` 等核心业务端口（`PROTECTED_PORT_REFUSED` 校验通过）。

### 3. 多维资源协同自治闭环阶段实验（`experiments/check_multi_resource_auto.py`）
使用 [`profiles/nginx-multi-auto.json`](profiles/nginx-multi-auto.json) 验证 Agent 根据运行态势自动控制全部子系统（原始数据：`state/multi-resource-auto-validation.json`）：
- **阶段 1（纯前台服务）**：保持 `kernel.default` 与 Cgroup 未隔离，**自动审计 Nginx 进程**并**自动在 `lo` 挂载 eBPF TC 观测模式**（`18095` 端口探测 `200 OK`）；
- **阶段 2（注入后台算力争抢）**：**自动审计新进程 `sysbench`**、**自动创建并迁入 `4096:1024` 受管 Cgroup**、**自动切入 `scx.slice (1ms + 唤醒抢占)`**；
- **阶段 3（受控端口突发洪泛）**：检测到 `18095` 端口速率达 `315.3 pkt/s >= 20 pkt/s`，**自动切换 `drop_target=true`（`TC_ACT_SHOT`）阻断异常端口请求**，同时前台 Nginx（`18086`）`200 OK` 零受影响；
- **阶段 4（洪泛平息与争抢退出）**：冷却 2 窗后**自动恢复端口放行（`TC_ACT_OK`）**；`sysbench` 退出后**自动回退 `kernel.default` 并自动将 Nginx 迁回根 Cgroup**，控制线程 CPU 占比仅 **`0.52%`**；
- **阶段 5（EEVDF 默认调度下 Cgroup 自动赋权）**：在 `mode=default` 下自动识别同核争抢并下发 `4096:1024` 权重，实测前台算力占比提升至 **`79.9% vs 20.1%`（`3.97 : 1`）**（独立纯 CPU 争用单测实测达 **`79.8% vs 20.2%` / `3.96 : 1`**）。

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

# 3. 开题报告 6 策略 × 4 场景完整消融矩阵与严谨多周期基准测试
python3 experiments/run_ppt_matrix.py
python3 experiments/run_rigorous_suite.py  # 5 周期平衡统计 (Mean±SD) + 负载梯度 + 多资源定量消融 + HTML报告生成

# 4. 课程答辩命令行一键全流程演示
python3 experiments/run_demo.py
```

---

## 相关文档导航

- [课程设计总结报告（含架构、优化细节、6×4 矩阵与多维闭环实测）](docs/course-project-report.md)
- [交互式 HTML 实验报告（N=5 周期 Mean±SD、负载梯度与多资源定量消融）](docs/experiment-report.html)
- [真机实时交互沙盒与可视化大屏](docs/demo-dashboard.html)
- [唤醒抢占专家与 BPF 调度优化说明](docs/wakeup-preemption.md)
- [在线业务 SLO 反馈机制说明](docs/business-feedback.md)
- [连续窗口基线测量规范](docs/baseline-measurement.md)
- [openEuler 6.6.0-scx 环境复现指南](docs/reproduce-openeuler.md)
