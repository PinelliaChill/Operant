# B2-2 交接：工程验收通过，最后独立复核待恢复

记录身份：Codex；适用对象：User、原 Codex Reviewer。仅授权 B2-2，未推送、合并或部署；B2-1 不重做，B2-3 未开始。

- 治理根：`/Users/bigo/agentworkspace/codexworkspace/operant`，workflow-20260910.3 与后续 Antigravity 条件静默规则。
- 实施树：`/private/tmp/operant-b2-2`；分支 `codex/b2-2-tasks-host`；基线 `8851a237cc961afa8649cffe57d407472838887f`。
- 最终代码：`518bb3f40a70728d79b743212aee6fec098a941f`。根工作区既有未提交内容保留。

Core 已接任务、首个 Thread、模型/角色配置、Agent 实例、规范历史及取消；Session 的 Event+Item 同事务，Factory 失败不伪造 Agent，后续新轮可恢复。Host 提供安装/认证/两种执行模式、权限与配置约束、运行 fencing、安全生命周期与资源清理。实现范围以 [架构](../../PROJECT_ARCHITECTURE.md) 和 [验收表](task-package.md) 为准。

## 最终证据

| 项目 | 当前结果 / 证据 |
| --- | --- |
| 完整基础门禁 | 847 passed、1 Docker 条件 skip；Ruff format/check、mypy src、lock offline、diff check 全通过，见 [verification.json](verification.json) 与 gate-logs-518bb3f |
| GUI / SDK | GUI 91 项测试、typecheck/build；正式 SDK surface / 生成确定性和旧协议冻结随完整门禁通过 |
| 实际 Host | macOS sandbox-exec 探针、两次授权 RPC、关闭通过；认证进程内真实安装入口通过，见 [隔离](host-isolation-smoke.json)、[进程内](host-inprocess-smoke.json) |
| 实际客户端 | 正式 gpt-5.6-luna/low；工具、审批一次、结果42、历史/分页、取消、断线/重连、宽窄屏/焦点；追加 Factory 受控失败→真实新轮42→审批等待取消，见 [客户端](client-acceptance.md)、[增量](client-review-increment.json) |
| 独立 Reviewer | 指定 Luna/max 已通过 caa2ba8 Session/B2/GUI，逐项关闭先前 Host P1，核到988fb24；最后并发/配置增量尚未独立通过，见 [原报告](review.md)、[状态](review-status.md) |

## 唯一待完成门禁

原 Reviewer `01a08baa-4872-7473-abb1-7ca6fe91517d` 的 turn `01a0906f-d947-7d51-8682-6b04d3ce191c` 再次因 workspace credits 耗尽失败。Codex 已修复其最后并发 P1，并强制维护开关/召回预算；9 种并发组合和6项配置回归及最终完整门禁通过，但不能替代独立复核。

额度恢复后继续同一 Reviewer，只读本交接、[Host 增量](host-review-increment.md)、`988fb24..518bb3f` 的相关源码/测试与最终门禁。源码不变时复用既有通过证据，不重跑 B2-1 或全套客户端。Reviewer 无新增阻断后再更新 AC-11 和总进度，停止 B2-2。工具未提供 Fast 开关，未声称启用。

## 范围限制

- 当前认证为受控本机 issuer 记录；认证进程内属于信任边界，不能物理强杀线程。未认证仅支持本机 macOS 实际沙箱，资源监测允许采样超调；缺沙箱拒绝。
- 普通 Core 默认没有插件；不含 MP-2 记忆引擎、后台调度、默认安装、插件管理 GUI、用户库迁移、后续 Graph/Team 完整交互、打包 dist / 签名 / 公证。Docker skip 不算容器验收。
- Task 页读取首个 API 页；后续任务列表分页未在本批客户端实测覆盖。统一 resume 未虚构：Session 验证显式新轮，Workflow 使用既有 Graph 安全恢复入口，未知写入仍需人工核对。
- 当前客户端与 Host 证据的源码指纹均与最终代码匹配。临时 Core18000/Vite3000 和独立 debug app 均已关闭，用户8000未触碰；数据/证据保留，见 [cleanup.json](cleanup.json)。
- 工程子 Agent 与 Antigravity 均已交还写权；Antigravity 无新视觉工单，保持条件静默。
