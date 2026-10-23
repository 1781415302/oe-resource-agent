# 当前 openEuler 环境复现

## 已验证环境

| 项目 | 实际记录 |
|---|---|
| 操作系统 | openEuler 24.03 LTS-SP4 |
| 发行版原始内核 | 6.6.0-159.4.3.154.oe2403sp4.x86_64 |
| 自建内核运行名 | 6.6.0-scx |
| 架构 | x86_64 |
| 虚拟化 | Windows + VMware，4个来宾vCPU |
| CPU宿主 | AMD Ryzen9 9955HX，16核32线程 |
| 编译器 | GCC12.3.1，Clang17.0.6 |
| Python | 3.11.6；代码保持3.9语法 |
| cgroup | v1，CPU/CPUacct联合挂载 |
| BTF | /sys/kernel/btf/vmlinux，哈希见validation/guest-build.json |
| HZ | CONFIG_HZ=1000 |
| 内核默认SCX时间片 | 实际BTF解析20000000ns |
| PSI/调度等待 | PSI不可用；sched_schedstats运行时关闭，等待指标明确标缺失 |

repro/kernel.config是实际运行内核的完整配置。包含CONFIG_SCHED_CLASS_EXT=y、BPF_SYSCALL=y、BPF_JIT=y、DEBUG_INFO_BTF=y和LOCALVERSION=-scx。配置中的证书字段为构建路径；构建签名材料在本机准备，不在项目包内分发私钥。

## 先准备能启动的内核

使用openEuler SP4 ISO安装完整虚拟机。检查uname、内核配置、BTF与/sys/kernel/sched_ext/state。出厂配置不一定启用sched_ext；当前用户以对应SP4源码RPM的准备树启用该配置、构建并安装独立-scx内核，再从该内核启动。

发行版补丁与SCX ABI必须与目标运行内核匹配。不能将上游6.6原版源码当作此回移植内核。当前准备树路径为/root/rpmbuild/BUILD/kernel-6.6.0/linux-6.6.0-159.4.3.154.x86_64，源包准备过程与之前的内核安装记录保存在工作区research/openeuler-agent-plan.md。

这里已保存完整配置和构建哈希，尚未把整个内核源码RPM与补丁集合打入源码包；自动获取和准备准确源包仍为后续交付工作。因此当前框架的一条构建命令以“已经具备匹配内核准备树”为前提。

## 构建框架

依赖是Python3、GNU make、GCC、Clang BPF、elfutils-libelf开发文件、zlib开发文件。工具链复用内核tools/sched_ext构建的libbpf与bpftool。当前不需要Rust、Python第三方包或在线模型服务。

```bash
uname -r
cat /sys/kernel/sched_ext/state
test -r /sys/kernel/btf/vmlinux
cd /root/oe-resource-agent
bash build.sh /实际准备后的匹配内核源码目录
./agent capabilities
```

构建必须在其他实验结束、sched_ext disabled时进行。构建脚本对齐运行内核、SCX枚举兼容接口与已有文件，保留旧版源码备份；二进制、BTF和源码哈希写入state/build.json。

0.1.2采用PEP552 checked-hash模块缓存：确定性tar的时间戳为0，旧的时间戳/文件大小缓存可能复用同大小旧代码。包初始化先核对并转换自己的模块缓存，之后由Python按内容失效；升级首次需要可写缓存目录。`python3 tools/audit_python_cache.py`会在不先导入Agent的情况下核对缓存代码、源码及hash模式。版本和agent_started事件同时记录实际加载版本。

收尾审计发现旧缓存仅使__init__.py中的版本仍显示0.1.0，其余当时模块与源码一致。修复前后审计均保存；正式重测应使用内容校验后的版本，不能把历史源文件指纹等同于对历史缓存的直接证明。

## 验证顺序

1. 确定性协议与规则检查。
2. 原生加载、在线参数更新、旧版本/身份/边界拒绝、监督失联回退。
3. 真实nginx/wrk/sysbench负载加入与退出的闭环。
4. 公开工具会话、Skills入口和受管cgroup参数回读。
5. 动态负载下与默认、固定策略的性能对照；0.1.1已完成四策略各3次、144窗口探索，详见docs/dynamic-benchmark.md与validation/dynamic-quick.json。最终独立评价与固定速率实验继续迭代。

固定性能基准euler-bench 0.1.1需安装nginx、wrk、sysbench、taskset和报告绘图库。其setup.sh、完整profiles与环境记录在独立基准目录。Agent功能检查不要求报告绘图库。

## 性能复现的边界

当前固定用例32连接、4KiB页面、CPU0服务与后台、CPU1客户端、CPU2控制器。每个quick候选18轮，10秒预热+30秒测量，每场景3次重复。原始ZIP、冻结参数、哈希、日志和图表均已归档。

Windows电源方案在2026-10-15读取为silent；宿主CPU负载未连续监测。候选按批次顺序测试。后续最终评价应冻结宿主条件、增加重复并交错候选，独立于参数探索数据。虚拟机数据只直接代表当前环境，物理缓存拓扑、NUMA、ARM与其他发行版需另做实际验证。
