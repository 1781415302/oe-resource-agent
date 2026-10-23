# 阶段一开发设计评审裁决

日期：2026-10-21。

对象：[阶段一开发设计](Z:/oe-resource-agent/docs/stage-1-development-plan.md)。范围仅限 Hyper-V 基准测量、默认调度定档及小规模对照；本次没有实现源码、编译工具或启动新的负载实验。

两名独立上下文审查者分别检查测量正确性和 workflow 可执行性。主 Agent 对每条问题重新阅读文档、核对相关源码后裁决；同一问题合并记录。以下定位均指最终文档，初稿中的问题已直接修正，不在最终设计中保留旧方案。

| 编号 | 意见与核对证据 | 裁决及理由 | 最终定位 |
|---|---|---|---|
| R1 | 两位审查者都指出：当前 Window 无法精确包含该行自身完成 flush 后才知道的完整导出耗时 | **采纳问题。** Window 移除该字段，Footer 汇总已经完成的 Ready/Window 导出耗时，明确不计 Footer 自身。**拒绝额外增加每窗 timing 记录的实现建议**：阶段一只需展示导出总成本，新增消息种类和配对状态没有必要；Footer 已消除矛盾 | [Footer 契约](Z:/oe-resource-agent/docs/stage-1-development-plan.md:169)、[导出耗时定义](Z:/oe-resource-agent/docs/stage-1-development-plan.md:377) |
| R2 | 测量审查指出：仅声明一个后台线程，未锁定它与 nginx 争用同一 CPU。核对 `experiments/run_fixed_rate.py:55`，现有实现明确 sysbench 绑定 service_cpus，并使用固定 prime/report 参数 | **采纳。** 明确 nginx/sysbench 绑定 CPU0，客户端 CPU1，控制与后端 CPU2；冻结后台参数及 nginx 单 worker，并保存实际亲和性。该要求是实验输入契约，不是限制普通实现写法 | [负载与 CPU 约定](Z:/oe-resource-agent/docs/stage-1-development-plan.md:435) |
| R3 | 测量审查指出：35 秒总运行、25 秒校准上限、三个 5 秒完整窗口无法同时保证。核对锁定 wrk.c:131，stop_at 在校准前确定；wrk.c:376 允许校准重试 | **采纳。** survey 改为 50 秒，覆盖 25 秒校准预算、三个最多 8 秒窗口和 1 秒余量；完整窗口/采样/覆盖前提不足用 sustained=null，不能作为容量上界。相应提高 survey 总超时并明确 pilot 时长公式 | [冻结时长](Z:/oe-resource-agent/docs/stage-1-development-plan.md:410)、§4.5、§5.2 |
| R4 | workflow 审查指出：网络探测前要创建 manifest，但 guest_preflight/snapshot 原为不可空，首次 SSH/环境探测失败无法合法保存 | **采纳。** 初始化时快照允许 null，成功后补齐；completed 禁止空快照。补齐早期无归档时的结果形式和 CLI 返回规则，不增加另一套失败框架 | [GuestSummary](Z:/oe-resource-agent/docs/stage-1-development-plan.md:299)、[StudyManifest](Z:/oe-resource-agent/docs/stage-1-development-plan.md:328)、[初始化规则](Z:/oe-resource-agent/docs/stage-1-development-plan.md:470) |
| R5 | workflow 审查指出：F02 负责 EPS 和动作归属，但 F08 所需逐窗结果没有公开接口。核对 `eulerbench/metrics.py:177`，EPS 包含推断时间原点、完整区间筛选和时长加权，不能让两个 Agent 各写一套 | **采纳。** 增加 WindowAudit、audit_windows 和 TrialAudit.windows；F02 统一计算，F08 消费结果。补充既有原始日志的最小消费字段和区间规则 | [WindowAudit](Z:/oe-resource-agent/docs/stage-1-development-plan.md:266)、[共享函数](Z:/oe-resource-agent/docs/stage-1-development-plan.md:388)、§4.7 |

主 Agent 另行完成的集成复核：

- S3 的 import/--help 关卡依赖 F02 实际落地，因此将 S1 列为硬依赖；S1/S2、S4/S5 仍可分别并行。
- 明确 survey/pilot 之间二进制身份核对、早期失败的可空归档字段、进程退出码和所属实例的关联方式。
- 查阅现有 oeagent.__init__ 的显式缓存写入行为，要求 Windows 分析入口使用本地 pycache，避免影响共享来宾目录。
- 本机只读核对 Python perf_counter 与 .NET Stopwatch：Python 报告 QueryPerformanceCounter；读数位于前后 Stopwatch 边界内。正式执行器仍须按设计检查该关系。

复核结果：两名审查者在修订后均确认其所提问题已解决，未发现指定范围内由修订引入的确定新矛盾或阻塞项。该结论是设计评审通过，不代表尚未实现的采集链路已经通过运行验证；运行证据由 Stage Map 的 S6/S7 提供。

## 宿主续跑流程复核

日期：2026-10-23。当前执行范围为 docs/stage-1-baseline-followup.md，继续使用原有单一数据协议及 schema_version=1；当前配置调整见续跑方案。以下两项是审查中已核对源码和测试的实际流程缺陷，限定修复后再进行新 survey/pilot。

| 编号 | 证据与裁决 | 验收 |
|---|---|---|
| H1 | analyze_baseline.py 的全局 quality 列表会使第一轮质量拒绝、第二轮合格的 study 仍为 quality_incomplete；对应宿主测试也断言这一旧结果。采纳修复：首次完整合格 attempt 应能完成整 study，被拒绝尝试保留原件与逐项说明，不参与定档；执行或测量硬失败仍阻断 | 测试同时检查 selected_run_key、audit passed、manifest completed、退出码 0、第一轮拒绝说明和归档保留 |
| H2 | collector 原时长只覆盖一次运行附近预算，遗漏 guest preflight 的上传/SSH、重查、每轮传输和第二轮合法执行。采纳修复：按已有显式等待关键路径求有限上限，正常用 StopPath 提前结束 | fake-clock 包含慢但未超时的传输、重查与两轮运行；不得用无限采集掩盖预算问题 |

历史 study 不批量重算或覆盖；本轮使用修复后新建的数据验收。该记录是设计及源码复核，不代表这些修复已经实施或新实测已经通过。
