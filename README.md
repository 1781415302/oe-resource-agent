# openEuler 自适应资源管控 Agent

版本0.1.3，GPL-2.0。开发中；当前实际兼容目标是用户的 openEuler 24.03 LTS-SP4、x86_64、6.6.0-scx。其他内核、ARM 与更多发行版尚未验证。

## 做什么

- 观察声明的服务与批处理进程，读取真实CPU、上下文切换、亲和性和可用的调度等待数据。
- 在连续窗口确认负载变化后选择内核默认或短时间片SCX；保持最短驻留时间。
- SCX运行期间通过BPF hash map更新时间片，核对map ID、版本和回读；原生后端监控Agent心跳，失联时卸载。
- 提供版本化JSON工具、持久控制会话、可执行Skills和插件能力接口。
- 记录观察、原因、动作、实际参数和故障，不把CPU使用率当成业务p99或性能收益。

当前规则依据固定负载quick的初步结果：混合场景1ms可减少尾延迟，但有吞吐与后台代价。原生后端已经在目标来宾编译、加载、在线更新和失联回退；真实负载切换与公开工具会话也通过。另已完成默认/固定1ms/固定20ms/Agent各3次动态对照，12次、144窗口有效并通过独立审计。混合阶段Agent相对同期默认的配对中位变化为p99 -26.36%、网页RPS -1.27%、后台EPS -6.65%，包含切换过程。这是当前虚拟机闭环场景的探索结果。

## 部署

0.1.3新增默认关闭的唤醒抢占专家、在线开关与触发统计，补充宿主采样门槛和同版开关对照。业务反馈中的服务角色也支持持续占CPU的任务。策略及实验说明见[唤醒抢占](docs/wakeup-preemption.md)。默认业务配置继续使用原有1ms候选。

复制源码包到来宾，root解压到/root。已经有与运行内核一致的源码和构建依赖时：

```bash
cd /root/oe-resource-agent
bash build.sh
./agent capabilities
./agent tools list
./agent run --profile nginx-cpu --duration 60 --dry-run
```

实际控制：

```bash
./agent run --profile nginx-cpu --duration 60
```

业务目标进程需要另行运行。只观察时不会加载SCX；实际运行与基准共享互斥锁。结束或错误时仅停止本会话所属的原生后端，确认默认调度恢复。sysfs已有其他SCX策略时不接管。

原生后端使用/sys/fs/bpf的bpffs。当前来宾已挂载；缺少时需准备该挂载。控制器默认CPU2，配置中可调整，必须属于来宾当前允许的CPU。

## 工具与Skills

```bash
./agent tool system.capabilities --input '{}'
./agent tool workload.observe --input '{"profile":"nginx-cpu","seconds":1}'
./agent skills list
./agent skills run cpu-adapt --input '{"profile":"nginx-cpu","duration_seconds":60,"dry_run":true}'
```

工具描述含inputSchema与mutates，结果统一返回schema_version、request_id、ok、data或error及实际耗时。持久控制会话：

```bash
./agent serve --profile nginx-cpu
```

每行输入JSON，例如{"id":"1","tool":"policy.status","arguments":{}}。会话内可调用policy.apply/rollback，写操作需要expected_revision；map更新还核对map ID和原生generation。EOF结束会话并回退。当前这是项目JSON行协议，尚未宣称MCP兼容。

policy.evaluate用于离线比较可比业务窗口，指标缺失返回inconclusive。旧nginx-cpu模式使用工作负载特征，nginx-slo模式使用下述在线业务反馈。

0.1.2新增nginx-slo业务反馈模式：metrics.submit/status接受带单调时钟、boot、策略revision和原始来源的实际窗口，并核对框架观察到的任务身份。支持默认基线、1ms探测、前台与后台预算、拒绝/冷却和业务指标失联回退。严格模式与改善但SLO仍未达标的模式明确区分；详见docs/business-feedback.md。

固定速率实验入口为`python3 experiments/run_fixed_rate.py --mode quick`。它使用单独构建的wrk2及校准后对齐指标；旧闭环实验的数值不能直接用作固定速率SLO保证。每个探测目前只有一个默认基线窗口，基线稳定性确认仍待增强。

固定速率探索已执行9次/27窗口，执行审计通过；Agent一次保留改善、两次拒绝并冷却，全部窗口未达到3ms目标。宿主有87% CPU占用采样且开头约122秒未覆盖，不能据此作独立调度收益归因。完整数据见output/fixed-rate-analysis-20261015及validation/fixed-rate-quick.json。

## 扩展与边界

- **resource.cgroup 插件**：自动发现控制器、创建受管 cgroup v1 组、按预期旧值修改 `cpu.shares` 并回读、核对 `(pid, start_ticks)` 身份后将进程迁入受管组（`resource.cgroup.attach`）及安全迁回根组（`resource.cgroup.detach`）。已在来宾验证同核 1024 vs 4096 权重争用下实测 CPU 分配比例 **3.96:1**（理论 4.0:1）。当前 SCX 后端未实现 cgroup 权重语义，SCX 运行中拒绝标记 cgroup 动作生效。
- **network.ebpf_tc 插件**：基于 `native/oe_net_hook.bpf.c` 实现内核 `sched_cls` eBPF TC 分类器（挂载于 `lo` egress），支持目标端口包/字节实时统计与在线切换端口级丢包隔离（`TC_ACT_SHOT`），卸载后自动恢复通信。
- **security.lsm 插件**：发现内核活跃 LSM 模块栈（`lockdown, capability, yama, apparmor, bpf`），审计目标进程的 Capability 位图、`NoNewPrivs`、`Seccomp` 与 cgroup 隔离上下文。
- **阶段二连续窗口业务 SLO 闭环（`check_slo_continuous.py`）**：将 5s 连续窗口 `wrk2`（`wrk2.hdr.window`）与边界裁剪后台区间对齐（`align_background_intervals`）接入 `nginx-wakeup-slo`，支持多窗口基线稳定性确认（`baseline_confirmations=2`）与积压追赶过滤（`baseline_backlog_draining`）。实测 25,600 RPS 混部下默认调度基线 p99 `3.505 ms`，自动触发 1ms SCX + 唤醒抢占后 p99 降至 **`1.987 ms`（-43.3%）**，后台 EPS 仅变化 `-0.76%`，达到 2.5ms 目标并稳定保持 `accepted`。
- **一键全流程演示**：执行 `python3 experiments/run_demo.py` 可在约 12 秒内完整展示能力发现、Cgroup 1:4 争用隔离、eBPF 网络观测与端口阻断、SCX 负载感知切入/BPF 在线调参/自动回退及 SLO 闭环汇总（详见 [课程设计总结报告](docs/course-project-report.md)）。

## 实验与复现

已有固定基准与真实原始数据位于euler-bench及工作区output/scheduler-matrix-20261014。动态对照完整报告、逐窗口CSV和原始归档位于output/dynamic-analysis-20261015；validation/dynamic-quick.json保存摘要。各策略共享观察流程，CPU采样包含驱动，尚不能单独证明完整Agent控制开销。

动态复现入口：`python3 experiments/run_dynamic.py --mode quick`，详见docs/dynamic-benchmark.md。运行前准备匹配原生后端与/root/euler-bench 0.1.1。

详细迭代路线见docs/iteration-plan.md和docs/progress-20261015.md。native源码从已经在目标内核验证的scx_oe_slice 0.1.4派生，保留原作者版权；实际CPU时间扣账、续跑补片、新增map、监督、真实负载切换均已在本机验证。

## 验证与接口文件

```bash
python3 -m unittest discover -s tests -v
python3 experiments/check_native.py
python3 experiments/check_workload_loop.py
python3 experiments/check_control_session.py
python3 experiments/check_cgroup_plugin.py
python3 experiments/check_ebpf_hooks.py
python3 experiments/check_slo_continuous.py
python3 experiments/run_demo.py
```

后四种来宾验证需在其他实验结束后运行；负载闭环复用已部署的/root/euler-bench 0.1.1及nginx、wrk、sysbench。验证原始日志写入results，阶段记录写入state；源码包validation包含归档证据。schemas/tool-catalog.json导出实际工具描述，schemas/tool-result.schema.json定义统一结果结构。
