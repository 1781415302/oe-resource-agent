# 唤醒抢占专家：0.1.3

## 目的与实现

同样是1ms时间片，刚唤醒的服务任务仍可能等待正在执行的批处理任务用完预算。候选保留原有加权虚拟时间队列与实际CPU运行时间扣账，增加有条件的重新调度请求。

仅当开关开启、用户任务唤醒、任务上次运行后进入睡眠且上次CPU突发不超过时间片一半、目标CPU正在执行连续两个运行段仍可运行的用户任务时，才请求 `SCX_KICK_PREEMPT`。每CPU两次请求至少相隔200微秒。队列顺序、CPU亲和性和实时调度类保持原有语义。

统计分列唤醒入队、短突发睡眠任务、合格竞争、抢占请求和限频抑制。抢占计数表示调用内核请求接口，不能声称每次请求都已引起实际上下文切换。

初次真实验证暴露了问题：绑定单CPU的任务跳过 `select_cpu` 回调，依赖该回调记录目标CPU使候选完全不触发。现已在 `enqueue` 中用 `scx_bpf_task_cpu(p)` 读取实际关联CPU。修复经网页+后台负载的关闭/开启/关闭验证。

## 控制接口

默认关闭。普通持久会话可在线启停，保持相同后端PID和map：

```json
{"id":1,"tool":"policy.apply","arguments":{"policy_id":"scx.slice","slice_us":1000,"wakeup_preempt":true,"expected_revision":0}}
```

先从 `policy.status` 获取实际revision，不能固定假定为0。结果回读开关、限频、PID、map ID和generation。失联回退继续由原生监督负责。新控制map值为48字节，旧32字节map被拒绝。

`nginx-wakeup-slo` 为显式实验配置，业务反馈探测时才启用新开关；默认 `nginx-slo` 继续使用原有1ms候选。默认升级需要更广负载和独立评价。

## 同版对照与宿主质量门槛

Windows入口为工作区 `research/run_host_controlled.py`，来宾入口为 `experiments/run_controlled_cycle.py`。源码包不包含SSH私钥。预先声明的配置见 `experiments/wakeup-profile.json`：

- 默认、同版1ms关闭抢占、同版1ms开启抢占三组，同周期随机顺序，固定种子。
- quick为3周期，每组两个30秒窗口；每窗口由独立wrk2进程校准约10秒。
- 40k计划RPS、32连接、4KiB网页及单线程sysbench争用CPU0，客户端CPU1，驱动CPU2。
- 开始前连续30秒宿主CPU采样不超过20%；运行中不超过30%。每5秒采样，最大间隔8秒。
- 仅宿主负载或采样质量失败允许整周期重试，每周期最多两次，保持相同顺序，保留全部尝试。
- 请求错误、延迟大、吞吐下降和无收益不会触发择优重试；执行失败保留并停止研究。
- 采样门槛不能证明消除所有Windows后台、电源频率、VMware暂停、内存和I/O干扰。

## 指标与原始证据

每尝试保存冻结配置、顺序、源码SHA256、原生后端及wrk2指纹、实际Agent版本、内容校验缓存审计、完整日志、CPU/RSS/切换采样、全部业务窗口和退出状态。宿主保存全程采样、门槛判定、执行起止及归档SHA256。

分析重算请求/HDR计数、结束边界排除数、后台完整区间、策略版本、真实原生退出、开关、触发计数和宿主门槛。wrk2的p99按计划发出时刻修正，与旧闭环wrk结果口径不同。

汇总窗口p99中位及最高窗口p99、吞吐、后台进度、资源代价和同周期配对变化。窗口相关，不能当成更多独立重复，不能把窗口p99的中位称为全程合并p99。3ms为探索目标，未达明确显示。驱动进程包含采样和控制，不能全部称作独立Agent开销。

## 复现

来宾先完成 `bash build.sh`，准备 `/root/euler-bench` 0.1.1与带对齐导出的wrk2。Windows需要Python3.9以上和OpenSSH，密钥由用户配置。

```powershell
python research/run_host_controlled.py --host 192.168.200.130 --key E:/ddemo/.vm-access/openeuler_ed25519 --mode smoke
python research/run_host_controlled.py --host 192.168.200.130 --key E:/ddemo/.vm-access/openeuler_ed25519 --mode quick
python research/analyze_controlled.py output/实际controlled目录
```

IP随DHCP可能变化，需填写实际地址。实验互斥，运行期间不要升级来宾代码。异常中断时仅向同run key、boot ID、PID/start_ticks对应实例发SIGTERM，使用pidfd避免PID复用；该实验负责退出清理。网络断开时即时停止可能失败，单周期仍有有限执行时长，恢复连接后应核对原始记录。

当前只验证了该x86_64 openEuler虚拟机；ARM、多NUMA与更多发行版尚未验证。
