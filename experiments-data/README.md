# 两轮实验数据

数据来自2026-10-26队友交接快照，在其 VMware/openEuler `6.6.0-scx` 环境采集。在线机制见[加权 Agent](../docs/weighted-agent.md)，部署见[演示部署](../docs/demo-setup.md)。这些数据不是当前 Hyper-V 来宾重新运行的实验。

| 版本 | 正式试运行 | 完整窗口 | HTTP窗口 / 后台对齐窗口 |
|---|---:|---:|---:|
| 旧阈值版和固定配置 baseline-v1 | 8场景×5配置×2轮＝80 | 160 | 140 / 140 |
| 加权版 weighted-v2 | 8场景×默认/Agent×2轮＝32 | 96 | 84 / 84 |

混部窗口同时具有HTTP与后台数据，因此总窗口为256，不能把224个HTTP窗口与224个后台窗口相加为独立样本数。

## 文件

- `raw/formal-two-rounds/baseline-v1/`、`weighted-v2/`：原始block记录、`study.json`及对应源码归档。
- `output/formal-two-rounds/baseline-v1/`、`weighted-v2/`：`analysis.json`、`audit.json`、`summary.csv`、`repeats.csv`、中文实验报告及300DPI PNG/可缩放SVG图。
- `raw/baseline-v1.tar.gz`：旧版完整三轮120次的父研究。
- `raw/weighted-v2-all.tar.gz`：新版中断时35次的父研究，含第三轮已完成3次。

正式研究只取最早两个完整块，筛选为 `block<=2`，未按收益挑选。`study.json.derived_subset` 保留父研究状态、轮数及摘要，父归档保留额外第三轮；原始文件中的绝对路径与机器身份保留采集时含义。各版本随附源码用于核对历史 `source_hashes`，不能用当前代码替代历史版本。

## 独立复算

在仓库根目录运行，输出到被忽略的 `results/`，不会覆盖归档结果：

```bash
python3 experiments/analyze_live_agent_study.py experiments-data/raw/formal-two-rounds/baseline-v1 results/recomputed-baseline-v1
python3 experiments/analyze_live_agent_study.py experiments-data/raw/formal-two-rounds/weighted-v2 results/recomputed-weighted-v2
```

分析器独立重算原始HDR p99、实际RPS、按时间重叠对齐的后台EPS，并检查原始摘要、完整窗口、同块PID、固定组内核回读与全局偏好一致性。两组共112次试运行均通过数据审计；数据来源与计算通过不代表每组性能都改善。

## 解释范围

每轮先对窗口取中位数，再对两轮计算均值和样本SD。窗口p99的均值不是全部请求合并后的总体p99；两轮只作描述性解释，不声称统计显著。两版本每次试运行窗口数不同，版本间差异不能直接归因为算法。

加权版延迟优先混部p99为17.561±14.354ms，同轮默认为9.667±0.198ms；请求内计算为826.677±1162.430ms，默认也有明显波动。完整CSV保留坏结果及每轮顺序。报表对照分按同轮默认重算，和Agent现场早期基线是两个不同参照。

新版独立动态压力与业务反馈丢失复测尚未运行；旧阈值版的功能证据不能替代新版验证。相关检查入口为 `experiments/check_agent_study_dynamics.py`，若另行运行两轮应指定 `--repeats 2`。
