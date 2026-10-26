# 演示部署与核验

当前工作区 `Z:\oe-resource-agent` 对应 Hyper-V 来宾 `172.26.1.3` 的 `/root/oe-resource-agent`；来宾运行 openEuler 24.03 LTS-SP4、`6.6.0-scx`，配置 4 个 vCPU。网页依赖兄弟目录 `/root/euler-bench`，其中 `eulerbench.runner` 提供 Nginx 配置和就绪探测。

网页默认使用[相对加权 Agent](weighted-agent.md)，延迟/吞吐权重为60%/40%。场景选择只重建负载；控制器根据实测数据试用、评分并保留或回退配置。原有 `./agent run` 继续使用 CLI 的 Router/SLO/Profile 机制。

## 来宾部署

新来宾先按[环境复现指南](reproduce-openeuler.md)安装依赖，再运行 `bash build.sh` 并构建连续窗口 wrk2。`state/build.json`、`state/wrk2-window-build.json` 必须描述当前来宾的实际产物。`repro/kernel.config` 为队友 VMware 环境的参考配置；迁移时核对当前内核和 BTF，不能复制另一台机器的构建清单或二进制来替代本机构建。

安装服务前检查现有实例和端口：

```bash
pgrep -af 'python3 .*experiments/web_dashboard.py'
ss -lntp | grep ':8899'
```

若已有手动启动的网页，先结束当前实验，再向核对后的网页 PID 发送 `SIGINT`，等待进程退出，确认 `cat /sys/kernel/sched_ext/state` 返回 `disabled`，并确认演示负载端口18091/18095释放。不要同时启动两个实例；启动过程会清理这些固定端口的残留负载。旧版进程若未清理完整，应先核对它自己的临时目录与子进程身份，再停止对应残留进程。

在来宾中安装并启动服务：

```bash
cd /root/oe-resource-agent
install -m 644 scripts/oe-live-dashboard.service /etc/systemd/system/oe-live-dashboard.service
systemctl daemon-reload
systemctl start oe-live-dashboard.service
systemctl status oe-live-dashboard.service
```

服务只监听来宾 `127.0.0.1:8899`，控制线程使用CPU2。业务/后台负载使用CPU0，wrk2使用CPU1，突发负载与独立扩展实验使用CPU3。服务正常停止命令为 `systemctl stop oe-live-dashboard.service`；日志使用 `journalctl -u oe-live-dashboard.service -n 50 --no-pager`。

## Windows 访问

在项目目录使用 PowerShell 7：

```powershell
pwsh -NoProfile -File .\scripts\start-dashboard.ps1
```

默认连接 `root@172.26.1.3`，沿用 OpenSSH 已配置的认证。其他来宾或显式密钥可以指定：

```powershell
pwsh -NoProfile -File .\scripts\start-dashboard.ps1 -GuestHost 192.168.200.130 -KeyPath C:\path\to\key -LocalPort 8899
```

脚本启动已安装的来宾服务，建立隐藏 SSH 隧道，并探测 API 就绪；浏览器访问 `http://127.0.0.1:8899/`。需要本机 OpenSSH 已信任来宾主机密钥、认证成功，且来宾允许转发到其 `127.0.0.1:8899`。单独建立隧道可运行：

```powershell
ssh -N -L 127.0.0.1:8899:127.0.0.1:8899 root@172.26.1.3
```

90秒没有页面轮询时负载会暂停，重新访问后恢复。切场景无需刷新，保留前后曲线；完整 HDR 数据需等待校准和完整窗口，快速探测单独显示，缺测不会作为零值或复用上一场景数值。

## 原始核验与实验数据

网页的“内核原始核验”区显示机器身份、Boot ID、Agent PID、Map参数和只读命令的退出码与输出。独立旁证直接通过 SSH 读取内核，不调用页面 API：

```powershell
pwsh -NoProfile -File .\scripts\show-kernel-proof.ps1 -Once
```

也可在来宾运行 `bash /root/oe-resource-agent/scripts/watch-demo.sh --once`。正式两轮数据、统计、图和复算命令见[实验数据](../experiments-data/README.md)；可选四配置对照见[演示说明](demo-four-strategy.md)。

## 测试

```bash
cd /root/oe-resource-agent
python3 -m unittest discover -s tests -v
```

当前 Hyper-V 工作区全量172项测试通过。主机侧基线测试还依赖兄弟目录 `../research/analyze_baseline.py` 和 `../research/run_baseline.py`；它们属于既有宿主研究工具，不在本仓库中。新机器缺少这两项时，完整发现会报告导入错误，不能将选定模块通过写作全量通过。

新版独立动态压力和反馈丢失验证脚本已提供，尚无本机实跑结论；两轮正式统计来自队友的 VMware 环境，不能当作本机复测结果。
