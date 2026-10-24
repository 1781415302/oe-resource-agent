# cpu-adapt

本文件是项目自身的 Skill 能力说明，由 `skill.json` 声明的 `agent.run` 执行。它不依赖外部大模型或在线服务。

## 支持的业务配置（Profiles）

系统预置并完整验证了 4 类运行 Profile，用户或上层调用者可通过 `--profile <name>` 灵活选择：

1. **`nginx-cpu`（基础 CPU 行为自适应路由）**：
   - 依赖 `procfs` 调度排队等待、CPU 占用率与上下文切换率采样；
   - 自动识别同核 `latency`（Nginx）与 `batch`（sysbench）争抢，满足最短驻留时间后自动切入 `scx.slice`，争抢平息后无感回退 `kernel.default`。
2. **`nginx-slo`（业务延迟 SLO 连续反馈闭环）**：
   - 依赖客户端提交的真实 `wrk2` 连续时间窗口 HDR 延迟直方图指标；
   - 以默认调度收集稳定基线，在基线违反 SLO 时切入 1ms 探测，根据多重硬约束（完成率、吞吐、p99 改善）决定保持或回退冷却。
3. **`nginx-wakeup-slo`（SLO 闭环 + 微秒级唤醒抢占优化）**：
   - 在 `nginx-slo` 的基础上，动态激活 `scx_oe_adaptive` 的唤醒抢占置顶信用机制（`wakeup_preempt = true`）；
   - 短睡眠突发任务获得 `vtime = vtime_now - slice` 置顶并触发 `SCX_KICK_PREEMPT`，实现微秒级唤醒抢占。
4. **`nginx-multi-auto`（四维多资源协同自治模式）**：
   - 由 `AutoPluginCoordinator` 统一编排调度器（SCX）、控制组（Cgroup v1）、网络钩子（eBPF TC）与安全策略（LSM）；
   - 混部时自动切入 `scx.slice (1ms + wakeup_preempt)`、自动创建 `4096:1024` Cgroup 隔离任务、自动挂载 eBPF TC 监控并支持突发洪泛自动丢包熔断、自动审计并对后台任务下发 `nice=15` 优先级压制。

## 执行流程

1. 探测 Linux 内核、权限、`sched_ext`、BTF、Cgroup v1 及活跃 LSM 栈能力；
2. 获取实验/Agent 文件锁（`flock`），严格防止并发冲突；
3. 按窗口持续采样受管任务，校验 Boot ID、PID 与启动时间戳，防止 PID 复用误伤；
4. 结合规则引擎与多插件协调器，评估状态并下发多维优化动作；
5. 通过 BPF Map 零重启热更新时间片与抢占参数，核对 `map_id`、`generation` 并内置看门狗心跳；
6. 记录观察、决策原因与动作日志；
7. 任务结束或异常中断时安全回退 `kernel.default`，释放受管 Cgroup 并卸载网络钩子。
