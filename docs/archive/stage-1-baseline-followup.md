# 阶段一默认环境测量续跑

## 1. 当前任务

按用户决定，直接使用当前宿主环境继续阶段一，不因宿主总 CPU 占用高而等待或停止。CPU 仍采样并写入报告；结果只验证测量链路与小规模行为对照，不宣称已排除宿主干扰或取得正式性能收益。

S0–S6 已交付。本地冻结 collector 启动修复、合法重试成功后的状态聚合和完整 collector 时长预算均已实施，17 项宿主测试已通过。无须再次实现这些修复或重编 wrk2/SCX。

下一轮只调整现有配置及受默认值影响的测试，然后运行一个新的 survey，独立审计并定档通过后运行一个新的 pilot。沿用现有 CLI 和 schema_version=1，不增加模式、接口、记录字段或依赖。

## 2. 限定修改

| 文件 | 允许修改 |
|---|---|
| `oe-resource-agent/experiments/baseline-profile.json` | 只将 host_quality.preflight_cpu_limit、host_quality.during_cpu_limit 设为 100 |
| `oe-resource-agent/tests/test_baseline_host.py` | 需要模拟 CPU 超线拒绝/重试的 fixture 显式使用 20/30；补充默认配置允许合法高 CPU 样本继续的回归 |
| `oe-resource-agent/docs/baseline-measurement.md` | 更新当前入口与新结果，保留历史原件和当时结论 |

100 是现有合法 CPU 百分比范围的上限，利用现有配置实现“不按 CPU 高低筛选”，不删除 host_gate，也不把 100 改成无穷或接受非法样本。两个配置调整前，当前运行配置仍是 20/30；本设计不代表已完成配置变更。

保持 sample_seconds=5、preflight_seconds=30、maximum_sample_gap_seconds=8、maximum_preflight_wait_seconds=120、maximum_attempts=2。预检仍等待完整有效采样，不再等待 CPU 降到旧参考线；collector 异常退出、样本缺失或无效、时轴/身份/归档/HDR/窗口错误、进程异常和清理失败仍阻止验收。

测试不得依赖生产默认值来模拟低阈值情形，也不得删除原有质量拒绝和重试测试。已有接口支持 0–100 的配置，无需修改 quality.py、measurement.py、宿主驱动或分析器源码。若发现这些文件必须变更，先报告具体证据，不顺手扩展范围。

## 3. 环境和验证

- Windows 用 pwsh；目录为 `\\172.26.1.3\root\oe-resource-agent`、`\\172.26.1.3\root\euler-bench`、`\\172.26.1.3\root\research`，Z: 可用时指向同一工作区。
- Linux 操作通过 SSH 在当前 `root@172.26.1.3` 执行，先核对连接和实例；目录为 `/root` 下三个并列目录。
- 结果放 `C:/oe-baseline-output`；Windows Python 缓存放本机临时目录，不写共享来宾缓存。
- 修改配置后运行指定宿主 unittest；确认测试 fixture 的独立阈值与冻结 profile/manifest 相符，不伪造旧配置。

```powershell
python -m unittest discover -s tests -p test_baseline_host.py -v
```

现有 collector 字节和启动方式未变时复用已有真实短采样，不重复运行。正式实验使用已验证的本地冻结副本和摘要检查。源码/配置先冻结再实验；旧 study 的 profile、manifest 和审计结果不改、不追认。

## 4. 实测顺序

1. 来宾 sched_ext=disabled、没有其他资源实验，现有 ResourceLock 可非阻塞获取；锁文件可保留，不能删除或以文件存在性判断占用。
2. 不因宿主总 CPU 高而停止，也不自行关闭用户应用。正式窗口不并行构建、测试、大报告解析或其他资源实验；业务内部客户端瓶颈、完成率和 EPS 条件仍按原计划评价。
3. 创建一个新 survey 并独立分析；原件可复算且 audit passed、selected_run_key 非空、calibration selected 时才执行 pilot。
4. 使用该新定档运行六 trial pilot，再独立分析；实际动作及 not_triggered 如实报告，默认调度和所属进程清理必须确认。
5. 每种操作本轮最多一个 study，内部固定重试预算不变。数据/执行错误保存证据并停止，不为取得结果无限开新目录。

```powershell
python research/run_baseline.py --host 172.26.1.3 --guest-root /root/oe-resource-agent --output-root C:/oe-baseline-output --mode survey
python research/analyze_baseline.py C:/oe-baseline-output/实际新survey目录
python research/run_baseline.py --host 172.26.1.3 --guest-root /root/oe-resource-agent --output-root C:/oe-baseline-output --mode pilot --calibration-dir C:/oe-baseline-output/实际已通过survey目录
python research/analyze_baseline.py C:/oe-baseline-output/实际新pilot目录
```

网络映射盘不可用时替换为 UNC 脚本路径，不改变 collector 的本地副本启动方式。

## 5. 完成条件

survey/pilot 独立通过、定档来源可追溯、六 trial 和测量完整性合格、退出清理完成后，可报告“阶段一默认环境测量与小规模对照验收通过”。报告必须标明“宿主 CPU 未用于筛选，未排除宿主后台干扰”，展示实际采样，不称为低干扰基准或正式收益。

后续如要定量主张算法收益，另行设计受控且独立重复的性能评价；本次先完成已有测量链路和 Agent 行为验证。
