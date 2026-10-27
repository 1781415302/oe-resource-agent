# 本机演示配置

当前演示虚拟机：`192.168.200.130`，VMware，4 个 vCPU，openEuler `6.6.0-scx`。当前内核 BTF 与仓库历史构建记录不同，需要在该虚拟机重新执行 `bash build.sh`，不能直接复用其他虚拟机的构建清单。

## Windows 启动

在项目目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-dashboard.ps1
```

浏览器访问 `http://127.0.0.1:8899/`。脚本使用工作区 `.vm-access/openeuler_ed25519` 中已有的 SSH 密钥启动虚拟机服务并建立本地端口转发。IP、密钥或本地端口变化时可以指定 `-GuestHost`、`-KeyPath`、`-LocalPort`；加 `-OpenBrowser` 可打开系统浏览器。

本地 `docs/demo-dashboard.html` 默认也通过 `http://127.0.0.1:8899` 读取实时数据。使用其他本地端口时，优先直接打开对应 HTTP 地址。

## 虚拟机服务

服务模板为 `scripts/oe-live-dashboard.service`，安装到 `/etc/systemd/system/oe-live-dashboard.service`。服务只监听虚拟机回环地址，通过 SSH 隧道访问，不需要开放防火墙端口。

该虚拟机原配置禁止 SSH 端口转发。现已为来自 Windows 主机 `192.168.200.1` 的 root 连接设置例外，仅允许本地转发到 `127.0.0.1:8899`；原 SSH 配置与旧项目备份保存在 `/root/oe-agent-backups/`。宿主地址改变时需相应调整该例外。

```bash
systemctl start oe-live-dashboard
systemctl status oe-live-dashboard
journalctl -u oe-live-dashboard -n 50 --no-pager
systemctl stop oe-live-dashboard
```

启动服务后开始前台负载；90 秒没有页面轮询时会自动暂停负载，重新访问页面后恢复。正在执行的四配置或场景套件不会因页面暂时离开而中断。

当前页面默认运行独立实时加权 Agent：下拉选择只改变负载，Agent 根据真实数据试用候选，按完整HDR p99与实际吞吐的相对分数决定保留或回退，不读取场景名称或历史赢家。默认延迟/吞吐权重60%/40%，没有p99和EPS性能达标线。权重独立设置，切场景不改变权重。完整四配置对照与历史报告放在默认折叠区域，日常演示无需运行。当前机制见[加权评分说明](weighted-agent.md)；历史实验见[演示说明](demo-four-strategy.md)。短探测快速更新，HDR仍等待校准和完整窗口，缺测不复用旧值；切换无需刷新，曲线保留前后记录。

当前配置用于演示启动，不替代独立重复实验。原有实验结果保留为历史数据。

## 本次配置验证（2026-10-25）

- 在当前虚拟机重新编译 0.2.0 SCX 后端和 eBPF TC 程序，并从已有 wrk2 源码编译连续 HDR 窗口版本。wrk2 二进制哈希与仓库既有记录一致。
- 87 项来宾与通用离线测试通过；主机侧测试依赖兄弟目录 `research/analyze_baseline.py` 和 `research/run_baseline.py`，当前材料未包含这两个文件，完整测试发现仍会报告该模块导入错误。
- SCX 加载、参数热更新、错误身份/版本拒绝、正常退出与失联回退通过。
- 完整实况剧本通过：观察到 1000µs → 500µs、同一 map ID，结束后恢复 `kernel.default`，后台负载退出，错误日志为 0。
- 面板 eBPF 观测、丢包和恢复通过；独立 Cgroup 检查测得 4.00:1，面板短时实验测得 3.91:1。

本地证据在项目上级目录 `deployment-20261025/`，包括 `dashboard-session.json`、`dashboard-plugins.json` 和 `guest-evidence/state/`。初次演示失败记录保存在 `dashboard-session-before-fix.json`。补回的 `repro/kernel.config` 来自当前运行内核的 `/boot/config-6.6.0-scx`；兄弟目录 `euler-bench/` 源码来自当前虚拟机。

## 网页与 SSH 交叉核验

页面新增机器身份、Boot ID、Agent PID、采样新鲜度和“内核原始核验”区。核验区只运行预先定义的只读命令，显示真实退出码和输出；不接收任意 shell 命令。可查看机器、进程、SCX/BPF Map、Cgroup 和 TC 统计，或导出当前状态、原始计数、窗口元数据与命令输出。

在独立 PowerShell 窗口运行以下脚本，使用真正的 SSH 连接每两秒读取内核状态：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\show-kernel-proof.ps1
```

已登录虚拟机 root 终端时，也可以直接运行 `bash /root/oe-resource-agent/scripts/watch-demo.sh`。加 `--once` 只读取一次。该脚本直接使用 `uname`、`ps`、`/proc`、`bpftool` 与 `tc`，不会调用网页 API。录屏时可以把网页和这个 SSH 窗口并排展示，对照 Boot ID、进程 PID、Map ID、时间片和内核丢包数。

新增 wrk2 实际完成 RPS、HDR p99、HTTP 状态错误和 socket 错误计数，来自校准后的完整 5 秒窗口。初次启动需要等待校准及首个窗口；暂停后历史窗口不作为实时吞吐显示。短周期 HTTP 探测仍单独标注样本数和时长，平均调度等待按调度次数计算。Cgroup 在本会话首次实验前显示“待测”，历史实验矩阵默认折叠并注明历史属性。断线或采样过期时会标记冻结状态。

本次界面改进已通过 95 项来宾与通用测试。浏览器检查覆盖原始窗口重算 RPS、原始记录 SHA256、Map 热更新、证据下载、Cgroup 1:2 测量、断线与恢复；独立 SSH 同时核对了 Boot ID、负载 PID、Map ID 和时间片。记录位于上级 `deployment-20261025/evidence-ui-qa.json`、`evidence-ui-export.json`、`ssh-proof-live.txt` 与 `evidence-ui-live.png`。这些是功能与数据来源核验，不作为独立重复的性能统计。
