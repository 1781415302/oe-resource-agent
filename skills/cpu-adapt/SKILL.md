# cpu-adapt

本文件是项目自身的 Skill 能力说明，由 skill.json 声明的 agent.run 执行。它不依赖 Codex 或外部模型服务。

## 输入

注册的业务配置、运行时长、是否只观察。业务配置声明 latency/batch 角色与进程名或 PID，选择器只提供角色线索，决策还需要实际 CPU 与上下文切换观测。

## 流程

1. 探测 Linux、权限、sched_ext、BTF 和本机匹配的后端。
2. 获取实验/Agent 控制锁，拒绝接管其他运行中的实验。
3. 按窗口采样 workload，核对 boot ID、PID 和进程启动时间。
4. 连续确认负载变化，满足最短驻留时间后选择内核默认或 SCX 时间片策略。
5. 通过已编译后端的 map 接口更新参数，核对 map ID、预期版本与实际回读。
6. 记录观察、原因、动作和后端日志；监督失联或错误时停止所属后端。
7. 结束后确认 sched_ext disabled。

## 性能评价

需要相同业务负载下的真实 p99、吞吐与后台进度。CPU 使用率、等待和上下文切换只能诊断，不能替代业务性能。当前Skill使用行为规则，并已在本机三周期动态对照中测得延迟改善及吞吐代价；范围限定于该闭环VM场景。业务反馈使用单独nginx-slo会话，见docs/business-feedback.md。完整结果见validation/dynamic-quick.json和工作区动态报告。
