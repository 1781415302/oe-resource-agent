# 业务窗口与SLO控制

0.1.2增加业务反馈模式。旧nginx-cpu配置继续按进程行为选择策略；nginx-slo配置等待真实业务窗口，先取得默认基线，再决定是否探测1ms。

## 工具与运行

```bash
./agent serve --profile nginx-slo
```

持久JSON会话新增metrics.submit和metrics.status，policy.status同时返回profile_id、boot_id、policy_since_monotonic与revision。SLO会话由自动控制器拥有调参权；手动policy.apply返回AUTOMATIC_CONTROL_ACTIVE，policy.rollback仍可恢复默认。普通nginx-cpu会话保留原有手动控制。

metrics.submit是动作工具，可能触发SCX。来源是本机受信业务采集器；source_sha256用于追溯原始日志，不构成对不可信来源的认证。窗口必须包含：

- 版本、唯一window_id、业务配置与本次启动boot_id、实际策略revision。
- Linux CLOCK_MONOTONIC起止；区间完整位于策略生效之后，时长、数据年龄和顺序满足配置。
- 固定负载描述workload_key、采集器类型和原始日志SHA256。
- 同窗口p99、完成请求数、请求错误、计划RPS、后台EPS及后台区间覆盖。

前台RPS由完成数/时长计算，不接收另一个可能矛盾的RPS值。框架还用自己的procfs历史检查PID/启动时间、角色、线程数、CPU亲和性和cgroup：窗口内变化、观察不完整或覆盖不足时拒绝。基线和候选还必须有相同观察身份、负载key、工具类型和计划速率。

## 控制状态

1. 默认调度收集基线。已经符合p99与完成率目标时保持默认。
2. 基线违反业务目标时加载1ms探测。实际动作回读、map ID与generation机制沿用原生后端。
3. 下一完整窗口与基线比较：请求完成率、错误、前台吞吐比例、后台比例、延迟改善均有约束。
4. 不符合硬约束则回退并冷却；符合则按目标模式保留。
5. 业务反馈超时、原始基线过期或混合负载消失时恢复默认。

### 目标模式

- require_slo=true：p99达标也是硬约束，不达标回退。
- require_slo=false：允许在其他硬约束满足且p99改善时保留，状态是mitigating_slo_unmet，slo_met仍为false；不宣称达标。

探索配置nginx-slo采用改善模式，目标3ms、完成率至少97%、前台吞吐和后台EPS各至少基线90%、p99至少改善5%。目标在profile中预先冻结。系统尚不能保证所有 workload 达标，也不能把一次窗口比较当作严格因果证明。

## 固定速率采集

```bash
python3 tools/prepare_wrk2.py /tmp/wrk2-upstream-44a94c17-lf.tar.gz
python3 experiments/run_fixed_rate.py --mode survey
python3 experiments/run_fixed_rate.py --mode quick
```

需要已部署/root/euler-bench 0.1.1、匹配原生后端与固定提交wrk2源码包。客户端使用独立路径，不覆盖系统wrk。准备命令的源码、基线包SHA、补丁、编译器与二进制指纹写入state/wrk2-build.json。

上游提交44a94c17d8e6a0bac8559b53da76848e430cb7a7在校准时清空HDR，却保留总完成数。本项目补丁在校准与事件循环结束记录单调时钟及计数快照，限定一个客户端线程；计划发送和HDR算法保持原样。结束边界响应在上游会先计完成数再跳过HDR，本项目独立计数terminal_exclusions并核对差额，不补造延迟样本。

每个30秒窗口校准约10秒，前台和后台指标对齐到校准后的区间。因此这组数据用于固定负载探索，不能替代0.1.1中包含切换过程的动态测试。完成速率高于计划可能反映追赶积压，不能仅据平均RPS断言稳定。最高窗口与每个原始窗口均保留。

参考上游：[实现](https://github.com/giltene/wrk2/blob/44a94c17d8e6a0bac8559b53da76848e430cb7a7/src/wrk.c)、[测量说明](https://github.com/giltene/wrk2#readme)。

## 验证与当前范围

```bash
python3 -m unittest discover -s tests
python3 experiments/check_feedback_session.py
python3 experiments/check_feedback.py
```

43项确定性检查覆盖窗口版本、年龄、覆盖、重复/重叠、身份变化、预算、零基线、不可比较状态、两种目标模式及内容缓存。公开会话另验证批量请求、旧版本拒绝、手动控制冲突、嵌套控制拒绝和SIGTERM。真实负载故障检查用预先声明的不可达1us目标验证严格拒绝，另外延迟工作负载回退确认来单独验证业务反馈失联；它们不是生产性能收益证据。故障窗口使用20秒，使校准后的完整后台报告覆盖可满足80%门槛。

目前每个探测只用一个完整默认窗口作基线。需要后续增加基线稳定性确认、积压变化识别、更广速率和独立评价。硬约束满足只说明所比较的这些窗口，不能证明未来持续满足。
