# 阶段一基线测量交接

本文保留阶段一原始 study、失败记录与修复过程。最新结果见第 15 节：新 survey 与 pilot 独立审计通过，阶段一默认环境测量与小规模对照验收通过。宿主 CPU 未用于筛选，未排除宿主后台干扰；本结果不是低干扰基准或正式性能收益。

## 1. 指定 survey 的历史结论

阶段一没有通过。最新状态见第 13 节：L1–L2 两项限定流程修复及 17 项宿主测试完成；宿主诊断仍超 CPU 门槛，L3 survey 未启动，S7 实测证据仍待补齐。

| 项 | 本次事实 |
|---|---|
| 指定 survey | `C:/oe-baseline-output/baseline-survey-20261022T045103Z-0e9f60` |
| 指定 pilot | 无。S7 的 pilot 目录为空，没有创建，也没有分析 |
| S8 重跑 | `python Z:/research/analyze_baseline.py` 指向上述 survey。标准输出 `status=quality_incomplete`，`selected_run_key=null`，`issues` 只有 `baseline-survey-20261022T045103Z-0e9f60-a1:host_quality_rejected`。进程退出码 2 |
| 定档 | `audit.calibration` 为 null。目录中没有 `calibration.json`。HTML 选档节为“没有可写入的有效定档。” |
| 来宾尝试 | 只有 `a1`。来宾 `summary.status=completed`，`final_sched_ext=disabled`，`cleanup_errors` 为空，guest/fetch 返回码都是 0。宿主运行门槛拒绝了这次尝试 |
| manifest | `status=quality_incomplete`，`error=host preflight unavailable before retry`，`collector_returncode=0` |
| 能否进入 pilot | 不能。没有通过审计的定档，禁止用本目录启动 pilot，也禁止用同输出根下更早的目录补定档 |

`quality_incomplete` 表示测量原件仍要保留，但宿主质量或定档证据不足。它不是“阶段一通过”，也不是把来宾执行判成损坏。分析器退出码 2 与 argparse 用法错误的 2 相同；必须以标准输出里的 `status` 和 `audit.json` 为准。

通过条件如果将来出现，也只说明原件可复算且宿主门槛满足。`passed` 仍然不是性能收益结论。本目录不是 `passed`。

## 2. 本次 Hyper-V 与历史 VMware

两套数据不能合并，不能用一边的 p99 或 RPS 解释另一边。

| 项 | 本次阶段一 Hyper-V | 历史 VMware |
|---|---|---|
| 时间 | 2026-10-22，study `baseline-survey-20261022T045103Z-0e9f60` | 2026-10-15 宿主快照，以及更早的 quick、dynamic、wakeup 记录 |
| 虚拟化 | 来宾 `virtualization=microsoft`。来宾 lscpu 的 Hypervisor vendor 为 Microsoft | `Z:/oe-resource-agent/repro/host-snapshot.json` 写明 `virtualization=VMware` |
| CPU | 来宾看到 Intel Core i7-14650HX，4 个 vCPU，允许 CPU 0–3。Windows 宿主 `platform=Windows-10-10.0.26300-SP0`，`machine=AMD64`，计算机名出现在 Hyper-V 权限错误里，为 `LQL` | 宿主 AMD Ryzen 9 9955HX，16 核 32 线程；来宾 4 vCPU。见 `docs/reproduce-openeuler.md` 与 `docs/progress-20261015.md` |
| 电源方案 | manifest 中可核对的 GUID 是 `8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c`。`powercfg` 中文名因驱动按 UTF-8 解码已被替换，不能从损坏字符串读名称。本次没有改电源方案 | 快照为 silent，GUID `64a64f24-65b9-4b56-befd-5ec1eaced9b3` |
| 宿主 CPU | 本目录 `host-cpu.jsonl` 161 条。门槛使用 QPC `host_monotonic_seconds`，不是两台机器的 UTC | 历史快照写明宿主 CPU 未连续采样 |
| `vmware` 字段 | 161 条都是空数组。空数组不是 0% 负载 | 旧采集器的 VMware 行留在该字段。本 study 的宿主不是那套 VMware |
| `hyperv` 字段 | 161 条均为 null。每条 `hyperv_error` 都是“你没有完成此任务所需的权限。请与计算机“LQL”授权策略的管理员联系。”权限不足不计入宿主 CPU，也不写成 0% | 历史文档不使用这组 Hyper-V 诊断 |
| 证据位置 | 第 4 节的 Windows study 目录 | `repro/host-snapshot.json`、`docs/reproduce-openeuler.md`、`docs/dynamic-benchmark.md`、`docs/progress-20261015.md`、`docs/wakeup-preemption.md`。旧 `state/` 与 `validation/` 保留，本阶段不覆盖 |

本目录审计限制原文有一条：`historical VMware rows stay in vmware; this study's host is not described as that VMware setup`。

同输出根下还有更早目录。它们不是 S7 指定的 study。本文不引用它们的指标，禁止拿来填补本次缺失的 calibration 或 pilot：

- `C:/oe-baseline-output/baseline-survey-20261022T032044Z-95e9a1`（S7 明确未复用）
- `C:/oe-baseline-output/baseline-survey-20261022T033050Z-c817ad`
- `C:/oe-baseline-output/baseline-survey-20261022T034047Z-619419`
- `C:/oe-baseline-output/baseline-survey-20261022T035039Z-c4c2b5`
- `C:/oe-baseline-output/baseline-survey-20261022T040658Z-c1b86a`
- `C:/oe-baseline-output/baseline-pilot-20261022T041950Z-8e7e25`
- `C:/oe-baseline-output/baseline-pilot-20261022T044455Z-a2984c`

## 3. 重做时使用的命令

工作区：Windows `Z:/oe-resource-agent`、`Z:/euler-bench`、`Z:/research`；来宾 `/root/oe-resource-agent`、`/root/euler-bench`、`/root/research`。Z: 是 `\\172.26.1.3\root` 的映射，和来宾路径是同一份文件，不是另一次实验。地址以当时探测为准。S7 使用 `172.26.1.3`，OpenSSH 默认身份，没有 `--key`。

任何会 import `oeagent` 的 Windows Python 都必须先把字节码缓存指到本机临时目录，避免写共享 Z: 上的 `__pycache__`。

### 3.1 已执行的 Windows 命令

S7 实际 survey：

```powershell
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'oe-stage1-pyc-s7b'
python Z:/research/run_baseline.py --host 172.26.1.3 --guest-root /root/oe-resource-agent --output-root C:/oe-baseline-output --mode survey
```

该命令退出码 2，是质量不合格的正常返回，不是用法错误。它新建了 `baseline-survey-20261022T045103Z-0e9f60`，没有复用旧目录。

S7 随后分析了一次。S8 按同一目录又分析了一次：

```powershell
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'oe-stage1-pyc-s8b'
python Z:/research/analyze_baseline.py C:/oe-baseline-output/baseline-survey-20261022T045103Z-0e9f60
```

S8 这次的标准输出是：

```json
{
  "status": "quality_incomplete",
  "selected_run_key": null,
  "issues": [
    "baseline-survey-20261022T045103Z-0e9f60-a1:host_quality_rejected"
  ]
}
```

进程退出码 2。下面第 6 节的指标就是这次重跑写入的 `audit.json` / `trials.csv` / `windows.csv` / `analysis.html`。分析器不改写 manifest、`host-cpu.jsonl`、attempts 归档和 raw 原件；它会原子替换上述四个衍生文件。没有 `calibration.json`，是因为没有选中的通过尝试，不是漏写。

S7 没有执行 pilot。下面这条只说明驱动接口，禁止对当前目录执行：

```powershell
python Z:/research/run_baseline.py --host 172.26.1.3 --guest-root /root/oe-resource-agent --output-root C:/oe-baseline-output --mode pilot --calibration-dir C:/oe-baseline-output/实际已通过的survey目录
python Z:/research/analyze_baseline.py C:/oe-baseline-output/实际pilot目录
```

只有新的 survey 被分析成 `passed`，且该目录自己的 `calibration.json` 存在并且 `status=selected` 时，才允许把 `--calibration-dir` 指到那个新目录。

### 3.2 驱动实际发给来宾的命令

不要把 Request JSON 拼进 shell。宿主用参数数组调用 OpenSSH，远程 argv 由 `shlex.join` 串成一条命令。未提供 `--key` 时没有 `-i`。

预检和正式负载都是上传 `/tmp/oe-baseline-<RUNKEY>.json` 后执行：

```text
ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes root@172.26.1.3 python3 /root/oe-resource-agent/experiments/run_baseline.py --request /tmp/oe-baseline-<RUNKEY>.json
```

SCP 使用同一组 `-o`。预检请求的 `operation` 是 `preflight`，只打印 GuestSnapshot，不创建负载、owner 或归档。survey 请求的 `operation` 是 `survey`。来宾最后一条机器结果必须是 `{run_key,status,archive,archive_sha256}`；正常完成时 archive 为 `/tmp/oe-baseline-<RUNKEY>.tar.gz`。普通日志不是完成记录。

停止只针对已核对的本次 run_key：

```text
ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes root@172.26.1.3 python3 /root/oe-resource-agent/experiments/run_baseline.py --stop <RUNKEY>
```

宿主采集器由驱动隐藏启动，不要对同一 `host-cpu.jsonl` 再手动起一个：

```text
pwsh -NoProfile -File Z:/research/collect_host_cpu.ps1 -OutputPath <study>\host-cpu.jsonl -DurationSeconds <驱动按等待上限计算> -IntervalSeconds 5 -StopPath <study>\collector.stop
```

间隔 5 秒来自冻结的 `host_quality.sample_seconds`。分析使用实际 QPC 间隔，不把目标间隔当成样本时间。

### 3.3 来宾上核对环境的命令

重做前在来宾 root 执行。S8 没有重新跑负载，也没有重新编译。本次预检已经记录这些检查通过；重做时必须重新看，不能沿用本文的 boot_id。

```bash
systemd-detect-virt
cat /sys/kernel/sched_ext/state
lslocks -o PID,COMMAND,PATH
```

期望虚拟化为 `microsoft`、调度状态为 `disabled`；lslocks 用于辅助观察是否已有持锁者，最终以现有 ResourceLock 的非阻塞 flock 获取结果为准。`/run/euler-bench.lock` 和 `/run/oeagent.lock` 在释放锁后可以继续存在，不能用文件存在性判断占用，也不能删除文件来绕过锁。已有别的实验持锁或 sched_ext 已启用时拒绝执行，不能 pkill，也不能卸载不属于本次的调度器。

窗口版 wrk2 若尚未构建，才在来宾、且 sched_ext 为 disabled 时执行既有准备命令。本次预检的 `wrk2_window_identity` 已通过，S8 没有重跑：

```bash
cd /root/oe-resource-agent
python3 -m unittest discover -s tests -v
python3 tools/prepare_wrk2.py /root/vendor/wrk2-upstream-44a94c17-lf.tar.gz --windowed
python3 experiments/check_wrk2_windows.py --duration-seconds 35
cat /sys/kernel/sched_ext/state
```

检查器使用端口 18088 和 ResourceLock，不启动 sysbench，不加载 SCX。做完后调度状态仍应是 disabled。

### 3.4 重做顺序

以下命令接口保持。两项宿主流程修复和 17 项测试已完成；下一轮先按 stage-1-baseline-followup.md 调整两个 CPU 上限及测试，再新建结果，不用旧记录补定档。

1. 先按当前续跑方案将两个宿主 CPU 上限设为 100，并通过对应测试。再确认来宾是 Hyper-V、sched_ext disabled、flock 可获取且没有另一套资源实验。算法、SCX、电源与 Hyper-V 配置不改，不自行关闭用户程序。
2. 按第 3.1 节设置 `PYTHONPYCACHEPREFIX`，再跑 `--mode survey`。驱动总会新建 `C:/oe-baseline-output/baseline-survey-<UTC>-<id>/`。不要覆盖 `0e9f60`。
3. 对该新目录运行 `analyze_baseline.py`。
4. 仅当退出码 0、`status=passed`、`selected_run_key` 非空、且新目录写出 `calibration.json` 时，才用该新目录做 `--mode pilot`，然后再分析 pilot 目录。
5. 若仍是 `quality_incomplete` 或发生执行失败，保留整个目录并结束本轮。一次续跑最多新建一个 survey study 和条件满足时的一个 pilot study；每个操作的内部尝试由现有驱动控制。没有新环境信息或已确认的环境变化时，不连续另开目录重试。不要改门槛，不要从 `sustained=true` 的行手写 calibration，不要只补跑某一个策略。
6. pilot 里如果 `transition_coverage=not_triggered`，把实际观测写入限制。不要为了让 SCX 出现而修改规则阈值。

## 4. 结果目录与语义

分析入口只认 Windows study 目录：

`C:/oe-baseline-output/baseline-survey-20261022T045103Z-0e9f60`

| 路径 | 语义 |
|---|---|
| `manifest.json` | StudyManifest。本次 `mode=survey`，`host=172.26.1.3`，`guest_root=/root/oe-resource-agent`，`status=quality_incomplete`，`error=host preflight unavailable before retry`，`collector_returncode=0`，`calibration=null`。原始事实，分析器不改写 |
| `profile.json` | 实际冻结的 BaselineProfile，必须与 manifest 内嵌 profile 一致 |
| `host-cpu.jsonl` | 全程追加的 HostSample。`error` 只表示宿主 CPU 样本失败；本次没有这种 error。`vmware` 兼容旧流程；`hyperv` / `hyperv_error` 是 Hyper-V 诊断 |
| `collector.log`、`collector.stop` | 采集器输出和停止标记 |
| `source-files/` | 本次宿主执行器、采集器、分析器和配置副本 |
| `attempts/<run_key>.request.json` | 发给来宾的请求 |
| `attempts/<run_key>.log` | 来宾标准输出和标准错误 |
| `attempts/<run_key>.tar.gz` | 下载的归档。本次只有 a1，sha256 `2d7de9405257b673fe7415451bc68666548af52a5e51ec6f1af891adc06830d6` |
| `attempts/<run_key>-preflight.*` | 预检请求和日志，不是负载尝试 |
| `raw/<run_key>/` | 按归档解包的来宾树。分析从这里的原始日志复算 |
| `audit.json` | StudyAudit。衍生文件，以最近一次 analyze 为准 |
| `trials.csv` | 由 audit 导出的 trial 表。空单元格表示 null 或空列表，不是 0 |
| `windows.csv` | 由 audit 导出的窗口表。`transition` 为真表示负载切换或策略切换 |
| `analysis.html` | 静态报告。状态句写明质量不足时不能写成阶段通过 |
| `calibration.json` | 仅 survey 审计 `passed` 且定档 `selected` 时才出现。本次缺失是预期结果 |

来宾原始树有两份视图，内容相同，都不是第二个 study：

- 来宾：`/root/oe-resource-agent/results/baseline-baseline-survey-20261022T045103Z-0e9f60-a1`
- 映射：`Z:/oe-resource-agent/results/baseline-baseline-survey-20261022T045103Z-0e9f60-a1`
- 已解包副本：`C:/oe-baseline-output/baseline-survey-20261022T045103Z-0e9f60/raw/baseline-survey-20261022T045103Z-0e9f60-a1/baseline-baseline-survey-20261022T045103Z-0e9f60-a1`

来宾树包含 `request.json`、`owner.json`、`summary.json`、`source-files/`、`python-cache-audit.json` 和 `trials/<trial_id>/`。每个 trial 的 `client.log` 是窗口协议原件，`samples.jsonl`、`background-intervals.jsonl`、`events.jsonl`、`driver-events.jsonl` 保持既有格式。默认调度没有 `native-*.log` 是正常现象。Footer 的 `export_duration_ns` 在 `client.log`，不是 StudyAudit 字段；本文不另算一套导出耗时。

尝试状态：

| 值 | 含义 |
|---|---|
| `host_rejected` | 来宾可以已经跑完，但宿主 preflight 或运行区间不合格。本次 a1 是这种 |
| `quality_incomplete` | study 级：没有选中的合格尝试，或定档证据不足。退出码 2 |
| `passed` | 有选中的完整尝试，独立审计通过，collector 正常退出。退出码 0。仍不是收益结论 |
| `failed` | 执行失败、归档失败或测量结构损坏。退出码 1 |
| `interrupted` | 中断。停止未确认时不能宣称清理成功 |

宿主只允许因为完整尝试的宿主负载或采样质量不合格而重试整次操作，最多 `maximum_attempts=2`。不允许因为 p99 或完成率差而重试，也不允许只替换其中一个条件。preflight 最多等 120 秒。

## 5. 本次尝试发生了什么

来宾预检必需项全部通过，包括 Linux、root、CPU 含 0/1/2、sched_ext disabled、bpffs、内核/BTF/后端身份、窗口版 wrk2、nginx、sysbench、taskset、`/usr/bin/env`、端口 18088 空闲、`virtualization=microsoft`、控制器亲和性和源码哈希。boot_id 是 `5084334e-b2b1-4b90-b8a4-f377b583ca0f`。内核 `6.6.0-scx`，来宾 Python 3.11.6，发行版 openEuler 24.03 LTS-SP4，MemTotal 3748872 kB。程序版本 0.1.1，`program_sha256=e4990bfc4d8badf8ee553c8a31f42c5bf469665b6c72cc7d7b213c7855989d59`。

预检限制，不是失败：来宾无法确认 Hyper-V 宿主管理权限；PSI 不可用；schedstats 不可用。

宿主 Python 3.11.15。study 记录的宿主源码摘要：

| 文件 | sha256 |
|---|---|
| `research/run_baseline.py` | `30cb88d1a23ae32f62a78b4b912d638de07c87c574fd6f1793aba9ca79df80d6` |
| `research/collect_host_cpu.ps1` | `9447624afc01a228e9e27ecf8a09ae4bb85b5733b854192344587e1d30aba4b0` |
| `research/analyze_baseline.py` | `f9d7d334044199405d30b81194f9fbd74a7a898940551f6dc02bf4f6aa8eb922` |
| `oe-resource-agent/experiments/baseline-profile.json` | `a5e59fece9f21aa18942fefe675914745cd1509833bbd8622868b8281a107051` |
| `oe-resource-agent/oeagent/measurement.py` | `8bc13c2cad0951d0452ce98075186bb8c4bcacd0832020f2def36ff4adcfb9f8` |
| `oe-resource-agent/oeagent/quality.py` | `4ab37a58e79d1deb2ab02a77e97db7a3b6d8c0d2428608513bff22067329b27e` |

冻结门槛：preflight 30 秒，宿主总 CPU 不超过 20%；运行区间不超过 30%；样本间隔不超过 8 秒；preflight 最长等待 120 秒；最多 2 次尝试。客户端 CPU 告警线 85%（100% 表示一个 CPU）。完成率持续区间 [0.97, 1.03]。这些值在看结果之前已经冻结。

a1 的 manifest 门槛，与 S8 重跑后 HTML 尝试行一致：

| 区间 | QPC 秒 | 样本数 | 最高宿主 CPU | 门槛 | 最大间隔 | 结果 |
|---|---|---|---|---|---|---|
| preflight | 69246.515606–69276.515606 | 5 | 19.0 | 20 | 5.5488845000072615 秒 | 通过 |
| 运行 | 69277.2217472–69982.5311249 | 128 | 47.0 | 30 | 6.435963199997786 秒 | `host_cpu_over_limit` |

运行区间约 705.31 秒。间隔没有超过 8 秒。拒绝原因只是最高 47%>30%。HTML 把这次重算为 `kind=quality`：来宾执行和测量结构通过，宿主质量不通过。尝试行里的 `run_rejected` 是这个意思，不是来宾崩溃。study 的 `issues` 只有 `a1:host_quality_rejected`。

整份 `host-cpu.jsonl` 的最高值是 53，但它在 preflight 窗口之前的 6 条样本里，不是运行门槛使用的 47，也不是 preflight 的 19。`host_end` 之后还有 22 条，最高 40，高于 20% 的安静 preflight 线。驱动因此没有启动 a2：重试前的安静 preflight 不可用，`quality_stop`，manifest error 为 `host preflight unavailable before retry`。attempts 数组长度是 1。这 22 条是原始样本的区间说明，不是 audit 里的第二条 issue。

a1 来宾摘要：`status=completed`，`error=null`，`final_sched_ext=disabled`，`cleanup_errors=[]`。14 个 trial 的清理错误也为空。条件只有 `default` 的 `service` 和 `mixed`。没有 fixed-1000，没有 adaptive。

## 6. 与本次 audit 一致的指标

这些数字来自 S8 重跑后的 `audit.json`。14 个 trial 全部 `measurement_passed=true`、`execution_passed=true`、`client_cpu_limited=false`、`transition_coverage=not_applicable`、`issues=[]`。它们属于被宿主拒绝的单次尝试，`selected_run_key` 为 null，所以没有公共参考速率，没有 low/mid/high，没有 `capacity_bracketed`。不要把 `sustained=true` 抄成正式容量，也不要和 fixed 或 adaptive 比较。

`service`，计划速率全部 sustained：

| rate | requests | latency_samples | duration_seconds | rps | completion_fraction | p50_ms | p95_ms | p99_ms | sustained | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| 2000 | 79682 | 79680 | 39.840227033 | 2000.0388033431316 | 1.0000194016715658 | 0.734 | 1.323 | 1.641 | true | 无 |
| 4000 | 159363 | 159360 | 39.840715666 | 4000.0034471268323 | 1.000000861781708 | 0.996 | 1.557 | 1.736 | true | 无 |
| 8000 | 318719 | 318718 | 39.839774055 | 8000.02027019528 | 1.00000253377441 | 0.876 | 1.631 | 1.878 | true | 无 |
| 16000 | 637460 | 637457 | 39.840986888 | 16000.105664852425 | 1.0000066040532765 | 0.808 | 1.468 | 1.619 | true | 无 |
| 32000 | 1274929 | 1274924 | 39.841470796 | 32000.04855563716 | 1.0000015173636612 | 1.067 | 1.9 | 2.067 | true | 无 |
| 64000 | 2549816 | 2549811 | 39.840947029 | 63999.88429351349 | 0.9999981920861483 | 1.181 | 2.151 | 2.411 | true | 无 |
| 128000 | 5099492 | 5099491 | 39.840080606 | 127999.03821560054 | 0.9999924860593792 | 1.055 | 2.141 | 10.511 | true | 无 |

`mixed`。只有 128000 的 sustained 为 false：

| rate | requests | latency_samples | duration_seconds | rps | completion_fraction | p50_ms | p95_ms | p99_ms | sustained | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| 2000 | 79691 | 79685 | 39.843562548 | 2000.0972529500928 | 1.0000486264750463 | 2.022 | 3.933 | 4.003 | true | 无 |
| 4000 | 159380 | 159372 | 39.841933507 | 4000.3078658820073 | 1.0000769664705018 | 1.945 | 3.825 | 3.945 | true | 无 |
| 8000 | 318705 | 318704 | 39.839619785 | 7999.699839504882 | 0.9999624799381103 | 1.913 | 3.029 | 3.877 | true | 无 |
| 16000 | 637438 | 637430 | 39.839296232 | 16000.23244105383 | 1.0000145275658643 | 1.765 | 3.005 | 3.165 | true | 无 |
| 32000 | 1274875 | 1274870 | 39.840176299 | 31999.73289355147 | 0.9999916529234835 | 1.404 | 2.891 | 3.433 | true | 无 |
| 64000 | 2549830 | 2549829 | 39.840351838 | 64001.191815980776 | 1.0000186221246996 | 2.097 | 5.683 | 7.583 | true | 无 |
| 128000 | 3255947 | 3255946 | 39.842106414 | 81721.25655625232 | 0.6384473168457213 | 10928.127 | 17481.727 | 18038.783 | false | `completion_out_of_range` |

`trials.csv` 用 12 位有效数字显示同一组 requests、p99 和 rps。空 flags 单元格表示空列表。

窗口表同样来自这次重跑的 `windows.csv`：112 行；每个 trial 8 行；`final=true` 14 行，`final=false` 98 行；`transition=true` 0 行；`error_count` 非 0 为 0 行。由 `end_ns-start_ns` 得到的时长约 4.836207193 秒到 5.001120819 秒。非尾窗没有超过 8 秒，study issues 里也没有 `window_longer_than_8s`。

audit 的窗口对象另有逐窗 issue，没有升到 trial.issues，也没有进入 study issues：`background_coverage_insufficient` 共 36 个，全部在 mixed。分 trial 为 2000 有 4、4000 有 5、8000 有 5、16000 有 7、32000 有 4、64000 有 6、128000 有 5，各自 8 个窗口。service 为 0。持续判断用的是评估窗合并后的覆盖，所以这些逐窗标记可以和 `sustained=true` 同时存在。这不是把覆盖门槛改掉的理由。

负载动作只有 mixed 的后台 sysbench：7 次 `background_start` 和 7 次 `background_stop`。没有 `policy_applied`。HTML 中 `policy revision=`、`not_triggered` 和 `adaptive` 都是 0 次。`workload_transition` 与 `policy_transition` 都没有出现。

## 7. 限制，以及没有触发的 adaptive 动作

本次 `audit.limitations` 原文如下，顺序与重跑结果一致：

1. `single cycle lists observed differences only; no confidence interval and no performance-benefit claim`
2. `host gate covers sampled Windows total CPU only and does not prove absence of all interference`
3. `Hyper-V diagnostics are separate from host CPU percent; missing permission is not 0% load`
4. `historical VMware rows stay in vmware; this study's host is not described as that VMware setup`
5. `hyperv_unavailable:你没有完成此任务所需的权限。请与计算机“LQL”授权策略的管理员联系。`
6. `Hyper-V host management permission cannot be confirmed from the guest`
7. `PSI unavailable`
8. `schedstats unavailable`

没有 `adaptive not_triggered:<trial_id>`。那条限制只在某个 trial 的 `transition_coverage` 为 `not_triggered` 时出现。本次 14 个 trial 都是 `not_applicable`，因为 survey 只跑默认调度。

adaptive 没有运行，不是“运行了但规则没切入”：

- profile 虽列出 `default`、`fixed-1000`、`adaptive`，survey 只执行 default。
- pilot 的 6 个 trial（三种条件，各做 mixed steady 和 dynamic）没有目录、没有归档、没有 audit。
- 没有 SCX 加载证据，没有 map 参数，没有 `native-*.log`，结束时 `final_sched_ext=disabled`。
- 因此也不存在切入或回退后的 `transition_coverage=observed`。

未触发不是失败理由，更不是把阈值改低再重试的理由。本次连“未触发”这个观测都还没有；阶段二不能把缺记录写成 Agent 已证明无效或已证明有效。

其他不能外推的点：客户端 CPU 告警没有出现，这不证明客户端没有瓶颈。宿主门槛只覆盖采样到的 Windows 总 CPU。单周期没有置信区间。mixed 128000 的完成率 0.6384473168457213 和 p99 18038.783 ms 只是被拒绝尝试中的观察，不是选定的容量上界。

## 8. 失败后如何恢复

保留 `C:/oe-baseline-output/baseline-survey-20261022T045103Z-0e9f60` 的全部原件和衍生报告。不要删除 a1，不要把 manifest 手改成 `passed`，不要补写 `calibration.json`。

按现象处理：

| 现象 | 本次是否发生 | 恢复 |
|---|---|---|
| 运行区间宿主 CPU 超过 30 | 是，最高 47.0 | 此次按当时冻结门槛拒绝，原件与结论保留；下一轮使用第 14 节的新配置，不重判本次数据 |
| 重试前安静 preflight 不可用 | 是，没有 a2 | 这是质量停止。等宿主安静后新开 study。不要在同一 manifest 里补一条假尝试 |
| 来宾执行失败、归档失败、测量损坏 | 否。guest/fetch 为 0，14 个 trial 测量和执行都通过 | 若将来发生，停止该 study，退出码应为 1。不要当成质量重试 |
| collector 退出非 0 | 否，返回码 0 | 若发生，不能在 finally 之后写成 completed |
| 锁占用或 sched_ext 已启用 | 本次预检不是这种 | 拒绝执行，只停止本次已经发布 owner 的进程 |
| Ready 超时、窗口结构损坏、主镜像不一致 | 本次 study issues 没有这些 | 保存原件，`measurement_passed=false`。不要回退旧聚合结果冒充连续窗口 |
| 完成率或请求错误，但进程正常且可复算 | 仅 mixed 128000 有 `completion_out_of_range` | 这是性能观察。不要为此重试 |
| Ctrl+C 或断网 | 本次不是 | 来宾只清理自己的实例；宿主调用 `--stop`。停止未确认就不要写清理成功 |
| boot、源码或二进制身份变化 | 本次没有进入 pilot，无从沿用定档 | 变化后必须重新 survey。内存等波动字段不构成身份变化 |
| Hyper-V 权限不足 | 是 | 只记限制。取得权限可以改善诊断，但不能把历史 null 改成 0%，也不能因此重判这次 CPU 门槛 |

分析器退出码：`passed` 为 0，`quality_incomplete` 为 2，数据或执行失败为 1。用法错误也是 2，所以要看 JSON。宿主 survey 本次同样以 2 结束，与“质量不合格的正常返回”一致。

## 9. 后续工作：补齐阶段一，再进入阶段二

以下第 1–5 项属于阶段一 S7 仍待补齐的证据，第 6–12 项是后续完善和解释范围；不能因 S8 文档已经交付而视为阶段一实测通过。不要修改算法来追认这份数据。

1. 两项宿主流程修复已完成。下一轮按默认环境续跑方案将两个 CPU 上限设为 100，再取得新的 survey；采样间隔和数据硬条件不改，历史尝试不追认定档。
2. 新 survey 独立审计和选档通过后，用该来源的 high 档跑完整 pilot：`default`、`fixed-1000`、`adaptive`，各做 mixed steady 和 dynamic，共 6 个 trial；全部独立审计通过且清理完成后验收阶段一。
3. 记录 adaptive 的实际动作。若观测不满足现有 `nginx-cpu` 规则，结果应是 `not_triggered` 加原始观测，而不是修改阈值。本次连这个观测都没有。
4. 确认策略动作、map 参数和退出状态一致；结束后 sched_ext 必须回到 disabled。本次只确认了默认调度 survey 的退出状态。
5. 不要把单次 pilot 的 p99 差异写成正式收益。正式重复、置信区间和完整策略矩阵仍在后续性能评价，不在本次补记。
6. SLO 连续窗口适配属于阶段二，本次没有改 `nginx-slo`。不要把这份默认调度 survey 当成 SLO 已闭环。
7. PSI 和 schedstats 仍然不可用，调度等待没有测量。阶段二要么取得这些观测，要么继续把缺失标成缺失。
8. Hyper-V 管理权限不足，虚拟机级 CPU 诊断是空的。修复权限是环境问题；修复前不能把 `hyperv=null` 当成本次没有宿主干扰。
9. 宿主门槛只看 Windows 总 CPU。本次运行中 47%、结束后样本最高 40%，说明未满足预声明的运行/安静门槛，但这些总 CPU 样本不足以辨别峰值来自虚拟机、驱动还是其他应用。门槛失败期间的来宾 p99 不能归因给调度器；后续先做只读进程增量诊断，不直接假定根因是外部后台程序。
10. mixed 逐窗 `background_coverage_insufficient` 很常见，但合并评估仍可能 sustained。阶段二要在原始 `background-intervals.jsonl` 上解释对齐，不要悄悄改 0.8 覆盖门槛。
11. 历史 VMware、Ryzen 9 9955HX、silent 电源方案的数据继续分开存放。阶段二报告必须再次写明虚拟化和电源 GUID，不能沿用 2026-10-15 的 silent 快照描述这次机器。
12. Footer 导出耗时若要进入阶段二开销，从各 trial 的 `client.log` Footer 读取。不要用 p99 反推，也不要把它补进本次已冻结的 audit 字段。

阶段一因此停在可复算、但质量不合格的 survey。实现和失败诊断可以交接；性能收益和 adaptive 行为都还没有证据。

## 10. 2026-10-22 上一次续跑检查点（历史）

**实现已交付，S7 实测证据待补齐。** 本节记录 045103 之后的共享输出状态及本轮续跑。045103 原件完整保留；没有从其中或其他旧目录手工取行定档。

### 源码、构建与来宾状态

当前研究源码与 060018 study 保存的源码快照一致；wrk2-window 和 SCX 实际二进制 SHA256 分别与 `state/wrk2-window-build.json`、`state/build.json` 的身份记录一致，因此没有重编译。来宾只读检查得到 `sched_ext=disabled`，无相关负载/Agent 进程；按 ResourceLock 顺序同时对 `/run/euler-bench.lock` 和 `/run/oeagent.lock` 取得非阻塞 flock 后释放。锁文件保留。

### 宿主只读诊断

诊断原件位于 `C:/oe-baseline-output/host-diagnostics/20261022T113118Z/`。2026-10-22 11:31:19–11:32:19 UTC 共 12 个样本，实际持续 60.636 秒，宿主有 24 个逻辑 CPU。总 CPU 样本为 20、10、2、7、19、13、21、17、12、8、15、10%，平均 12.833%，最高 21%；CPU 采样无错误，最大相邻间隔 6.84 秒。`samples.jsonl` 逐样本保存时间、PID、进程名、CPU 时间增量和按实际间隔及 24 个逻辑 CPU 归一化的百分比；摘要列出的较高进程增量为 ChatGPT PID 31356 峰值 1.944%、Grok Bot PID 27048 峰值 1.219%、msedgewebview2 PID 51168 峰值 0.826%。这些进程增量不能解释宿主总 CPU 的来源。Hyper-V 管理诊断仍受权限限制，不能据此把旧 47% 或后续峰值归因给某个外部应用。

### 本轮 survey 与 pilot

唯一新建的 survey 目录为 `C:/oe-baseline-output/baseline-survey-20261022T113340Z-17597a`。驱动退出码为 1，manifest 状态 `failed`，`collector_returncode=1`，没有尝试记录。collector 在加载 UNC 脚本 `\\172.26.1.3\root\research\collect_host_cpu.ps1` 时被 PowerShell 执行策略拒绝：脚本未签名；本机 `LocalMachine` 策略为 `RemoteSigned`。因此 guest 负载前预检没有启动，`guest_preflight_missing`，没有宿主采样，也没有 `calibration.json`。独立分析器退出码为 1，audit 状态 `failed`，issues 为 `collector_exit`、`host_samples_missing`、`no_attempts`、`guest_preflight_missing`。原件和衍生 audit 均保留在该目录。

本轮没有启动 pilot，没有 default/fixed-1000/adaptive 的有效六 trial，也没有本轮策略动作或 `not_triggered` 观测。输出根目录中预先存在的其他 pilot 不能充当本轮结果：`baseline-pilot-20261022T041950Z-8e7e25` 有六条 trial 记录，但 audit 为 `failed`（运行宿主 CPU 峰值 37%、归档摘要不符，三个 dynamic trial 均 `pilot_window_short`）；`baseline-pilot-20261022T044455Z-a2984c` 在执行前因 guest source identity 与其旧定档不匹配而失败，没有 trial。两者均未作为本轮定档或 pilot 输入。

输出根目录中还预先存在较新的 `baseline-survey-20261022T060018Z-efcb30`：audit 为 `failed`、`selected_run_key=null`、无定档。其 a1 完成来宾 survey 但运行 CPU 峰值 38% 被拒；a2 运行峰值 44%，guest 结果为 `interrupted`，StudyAudit 失败。两次尝试均不能用于本轮定档。该目录的存在解释了为何共享工作区实际状态晚于 045103 交接记录。

### 清理与续跑条件

本轮 collector 在 guest 负载前失败，未发布 guest owner，也未启动业务负载。失败后 sched_ext 仍为 disabled；当前驱动已退出，未发现本轮 collector、wrk、sysbench 或 nginx 进程；两把 ResourceLock 再次可非阻塞获取。没有加载 SCX，也没有需要恢复的 Agent 所属任务。

采集器启动问题已由第 11 节的本地冻结副本修复解决。该轮 20%/30% 门槛下的失败事实保留；当前下一轮配置和执行方式见第 14 节。只有新 study 独立审计通过并生成有效 calibration，才可启动 pilot，不借用历史失败记录补定档。

## 11. 2026-10-22 采集器启动修复与续跑检查点（历史）

**采集器启动修复完成，S7 实测证据仍待补齐。** 本次没有通过正式 survey 预检，因此没有新定档或 pilot；阶段一实测仍未通过。

### 启动路径修复

`research/run_baseline.py` 的 `_open_collector()` 现在启动 study 中 `source-files/research/collect_host_cpu.ps1` 的本地副本。启动前检查文件存在，并将实际 SHA256 与同一 study manifest 的 `host_source_sha256` 对应值比较；摘要缺失、格式无效或不匹配均显式失败，且不会调用 UNC 源脚本。采样输出路径、时长、间隔、StopPath、隐藏启动和退出等待保持不变。没有修改采集逻辑、门槛或执行策略。

`tests/test_baseline_host.py` 新增本地冻结副本 argv、文件缺失和摘要不符时不启动子进程的覆盖。指定命令 `python -m unittest discover -s tests -p test_baseline_host.py -v` 通过，15 项测试全部通过，退出码 0；既有 collector 失败和退出等待测试也通过。

### 本地真实短采样

短采样目录：`C:/oe-baseline-output/collector-smoke/20261022T144953Z/`。命令为 `pwsh -NoLogo -NoProfile -NonInteractive -File C:/oe-baseline-output/collector-smoke/20261022T144953Z/collect_host_cpu.ps1 -OutputPath C:/oe-baseline-output/collector-smoke/20261022T144953Z/host-cpu.jsonl -DurationSeconds 20 -IntervalSeconds 5 -StopPath ''`，退出码 0。本地脚本与共享源字节的 SHA256 均为 `9447624afc01a228e9e27ecf8a09ae4bb85b5733b854192344587e1d30aba4b0`；采样文件有 3 条有效 CPU/QPC 样本，`host_cpu_percent` 为 38%、37%、41%，QPC 值分别为 105150.8623527、105158.7760654、105164.6268169；Hyper-V 诊断因权限不可用。该检查没有启动 nginx、sysbench 或 SCX，不替代正式预检。

### 宿主诊断与实测门槛

只读诊断原件位于 `C:/oe-baseline-output/host-diagnostics/20261022T145036Z/`。采样时间为 2026-10-22 14:50:37–14:51:36 UTC，持续 60.359 秒，共 13 个宿主样本，24 个逻辑 CPU；总 CPU 平均 32.846%，峰值 50%，无 CPU 或进程枚举错误。可访问进程中最大归一化增量为 DeltaForceClient-Win64-Shipping PID 53164，平均 15.560%、峰值 22.295%；这不足以解释宿主总 CPU 的全部来源，也没有关闭任何应用。该观测已超过运行门槛，故本轮没有启动 survey。

SSH 复核确认来宾为 openEuler 24.03 LTS-SP4、内核 `6.6.0-scx`，`sched_ext=disabled`；没有相关实验进程。`/run/euler-bench.lock` 与 `/run/oeagent.lock` 均成功取得并释放非阻塞 flock，锁文件保留。

### Survey、定档与 pilot

本轮未创建新的 survey 目录，故无 survey 驱动/分析器退出码或 audit；`calibration.json` 未生成。本轮未创建 pilot，没有六个有效 trial、策略动作或 `not_triggered` 观测。先前失败目录及原件保持不变。

下一轮按第 14 节的默认环境方案新建 survey，独立审计和选档通过后再进入六 trial pilot。单周期对照仍不构成正式性能收益结论。

## 12. 当前续跑流程

两项宿主流程修复和指定测试已经完成，实际结果见第 13 节。当前执行入口为 docs/stage-1-baseline-followup.md；下一轮只调整现有两个 CPU 上限配置及受默认值影响的测试，再运行新 survey/pilot。详细配置和结果口径见第 14 节。

复用既有构建与有效 collector 短采样，运行时仍核对身份和资源占用。历史原件保留；正式窗口不并行构建、测试或大报告解析，不擅自关闭用户应用。

## 13. 2026-10-23 低负载续跑记录（历史）

**采集器启动修复完成，S7 实测证据仍待补齐。** 本轮 L1–L2 流程修复及测试通过；宿主未满足低负载条件，在 L3 前停止，没有创建 survey 或 pilot。

### 两项流程修复

`research/analyze_baseline.py` 将可重试的宿主质量拒绝保留在 attempt 诊断中。若后续合法 attempt 通过并被选中，已拒绝 attempt 作为 `retryable_host_quality_rejected:<run_key>` 写入 limitations，不再阻止 StudyAudit 通过；没有合格 attempt 时拒绝仍进入 audit issues 并保持 `quality_incomplete`。执行、身份、归档和测量错误仍然阻断，成功后再追加 attempt 仍导致失败。没有增加持久字段或改变 schema。

`research/run_baseline.py` 按 `2*Q + 120 + 120 + A*(Q + 120 + G + 120 + S + 1) + 180 + 3*S` 计算有限 collector 时长，其中 Q/A/S 取冻结 profile，G 沿用 `_host_wait(mode)`，120 秒远端超时集中为 `_REMOTE_TIMEOUT`。StopPath 正常提前停止采集器；采集失败和退出等待行为未改。

指定 Windows 命令 `python -m unittest discover -s tests -p test_baseline_host.py -v` 通过，17 项全部通过，退出码 0。测试覆盖：首轮被拒、第二轮通过后 audit/manifest/驱动均通过；首轮原件、报告诊断和拒绝说明保留；定档只引用第二轮；第二轮前无合格 attempt 时仍 quality_incomplete；额外 attempt、执行失败仍拒绝；以及接近现有超时的上传、预检 SSH、重查、两轮运行和下载总时长超过旧 collector 预算时，collector 仍存活并正常退出。

现有本地采集器短采样 `C:/oe-baseline-output/collector-smoke/20261022T144953Z/` 继续有效：采集器字节和启动方式未改，本轮按续跑计划复用，不重复采样。

### 构建、来宾与宿主状态

没有重编译。wrk2-window 与 SCX 实际二进制 SHA256 分别仍与 `state/wrk2-window-build.json`、`state/build.json` 一致；冻结 profile 摘要仍为 `a5e59fece9f21aa18942fefe675914745cd1509833bbd8622868b8281a107051`。SSH 复核为 openEuler 24.03 LTS-SP4、内核 `6.6.0-scx`，`sched_ext=disabled`；没有相关 workload/Agent 进程。`/run/euler-bench.lock` 和 `/run/oeagent.lock` 均成功取得并释放非阻塞 flock，锁文件保留。

宿主诊断原件位于 `C:/oe-baseline-output/host-diagnostics/20261023T060524Z/`。2026-10-23 06:05:24–06:06:24 UTC 采样 60.331 秒，共 13 个样本，24 个逻辑 CPU；总 CPU 平均 21%、峰值 45%，CPU 采样及进程枚举均无错误。最近观测到的 30 秒区间 CPU 峰值为 45%，超过 20% 预检门槛；因此没有启动正式 survey。可访问进程增量未能解释总 CPU 峰值来源，Hyper-V 管理诊断仍不可用。

### S7 状态

本轮未新建 survey 目录，因此没有 survey 退出码、StudyAudit 或 calibration；未启动 pilot，没有六个 trial、策略动作或 `not_triggered` 观测。历史 survey/pilot 与其 audit、归档保持原样。

该轮按当时的 20%/30% 门槛未启动正式负载。下一轮执行第 14 节的默认环境方案，仍须新 survey 独立审计和定档通过后才进入 pilot；本次测量不构成正式收益。
## 14. 2026-10-23 默认环境续跑方案（profile 变更前记录）

本节记录 profile 变更前的方案状态：当时实际值为 preflight_cpu_limit=20、during_cpu_limit=30。按用户授权的 100/100 配置及实测结果见第 15 节。续跑使用 docs/stage-1-baseline-followup.md，不新增模式、接口或 schema 版本。

17 项宿主测试和两项流程修复已完成，不重复实施。配置调整并通过对应测试后，使用现有 CLI 新建一个 survey、独立分析、有效定档后新建六 trial pilot，再分析并更新交接。不因当前宿主 CPU 高而等待，不自动关闭用户程序。

CPU 仍采样并展示。采样缺失/无效、最大间隔、数据/HDR/窗口、身份、归档、进程异常和清理仍是硬条件。历史原件不改；新结果只能说明当前宿主环境下测量链路和行为对照是否通过，不能称为低干扰参考或正式性能收益。

## 15. 2026-10-23 默认环境测量结果

**阶段一默认环境测量与小规模对照验收通过。宿主 CPU 未用于筛选，未排除宿主后台干扰。** 结果验证测量链路和现有策略行为，不作为低干扰基准或正式性能收益结论。

### 配置与验证

`experiments/baseline-profile.json` 的 `host_quality.preflight_cpu_limit` 与 `host_quality.during_cpu_limit` 均为 100；其他字段保持原值，schema_version 仍为 1。需要模拟 CPU 超线的测试使用显式 20/30 fixture，保留原拒绝和重试覆盖；新增 100% 样本可通过的默认配置回归。指定命令 `python -m unittest discover -s tests -p test_baseline_host.py -v` 通过，18 项全部通过，退出码 0。collector 启动方式未变，复用既有有效短采样。

执行前核对 openEuler 24.03 LTS-SP4、内核 `6.6.0-scx`，survey/pilot boot_id 相同；wrk2 与 SCX 二进制摘要与构建记录匹配，无需重编。survey 和 pilot 独立 audit 均通过，pilot 定档来源指向本次新 survey。

### Survey 与定档

- 目录：`C:/oe-baseline-output/baseline-survey-20261023T063024Z-9839a5`
- 驱动退出码 0，manifest `completed`；独立分析器退出码 0，audit `passed`，selected run 为 `baseline-survey-20261023T063024Z-9839a5-a1`。
- `calibration.json` 状态 `selected`。service reference 为 128,000 RPS，mixed reference 为 64,000 RPS；low/mid/high 为 25,600/44,800/57,600 RPS。`capacity_bracketed`：service=false，mixed=true。
- 宿主 CPU 样本保留在 study：preflight 峰值 28%，运行峰值 54%；门槛配置均为 100，因此宿主 CPU 未用于筛选。最大样本间隔低于 8 秒。

### Pilot

- 目录：`C:/oe-baseline-output/baseline-pilot-20261023T064422Z-188d9c`
- 驱动退出码 0，manifest `completed`；独立分析器退出码 0，audit `passed`，selected run 为 `baseline-pilot-20261023T064422Z-188d9c-a1`。六个 trial 的测量与执行均通过，audit issues 为空。
- 宿主 CPU preflight 峰值 29%，运行峰值 98%；记录保留，未参与筛选。

| 条件 | Mixed steady | Dynamic | 策略观测 |
|---|---|---|---|
| default | 测量/执行通过 | 测量/执行通过；`completion_out_of_range` | 未加载 SCX，最终 default |
| fixed-1000 | 测量/执行通过；`completion_out_of_range` | 测量/执行通过 | 切换到 `scx.slice`、slice=1000 µs；map_id 194/206；backend returncode=0 |
| adaptive | 测量/执行通过 | 测量/执行通过 | 两个 trial 均 `transition_coverage=observed`；mixed map_id 218 切入 1000 µs，dynamic map_id 230 切入后回到 `kernel.default`；backend returncode=0 |

Adaptive 没有 `not_triggered` trial。四个 native Ready 记录的 map_id 与策略事件一致；所有六个 trial 均 `cleanup_errors=[]`。汇总 `final_sched_ext=disabled`，后台 sysbench 按 stop 动作退出（returncode=-2），wrk2/nginx returncode=0。pilot 后 SSH 确认 `sched_ext=disabled`，没有本轮相关进程遗留。

### 限制

两个 `completion_out_of_range` 是性能标记，不是执行或测量错误；它们不阻止 study audit 通过。单周期结果没有置信区间，不能作正式收益主张。Hyper-V 管理诊断、PSI 和 schedstats 不可用；survey 的 service 速率到达测试表上限仍 sustainable，因此只是保守档位，不是测得的最大服务能力；mixed 在可比速率处不持续。历史目录和审计未修改。