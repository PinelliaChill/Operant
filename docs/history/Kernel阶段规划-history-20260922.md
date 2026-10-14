# 项目架构 历史记录

> 归档日期：2026-09-22；记录身份：Agent1；适用对象：所有 Agent。
> 来源：整理前的 `docs/项目架构.md`；以下保留原阶段、作者、日期、验收与限制，只有相对链接按新位置调整。
> 文中的“当前”“未完成”“未合并”均指原记录时点，不作为今天的实现状态。

返回[当前文档](../项目架构.md)。

### 3.1 当前可复用基础

当前 Operant 已经具备：

- `ModelProfile`、`RolePreset` 和不可变 `RoleSnapshot`；
- `Session`、`AgentInstance` 和事件持久化；
- OpenAI-compatible Provider 与流式 Tool Call；
- 单 Agent Tool Calling Loop；
- Workspace 文件读取、搜索、补丁、命令和 Git Diff；
- Host/Docker Runner；
- Tool Policy 双层校验；
- 高风险命令审批、取消和超时；
- Planner → Explorer(s) → Coder → Reviewer → Main 固定 Workflow；
- 有界并行 Explorer、唯一可写 Coder 和有限返工；
- Workflow Event、阶段检查点、基础恢复与人工核对；
- Working、Episodic、Project Memory 与 SQLite FTS5；
- Trace、CLI、FastAPI、SSE 和本地 Web 工作台。



### 当前更新：Operant Beta 2.0（OPERANT-BETA2-20260909）

记录身份：Codex。适用对象：所有 Agent。B2-1 已交付，后续进度统一见 memory/current.md。
本轮将 GUI-L0～L4/LR 和完整记忆 MP-0～MP-6 合并编排，交付一套 React + Tauri 桌面界面、
真实任务/协作入口与可替换记忆引擎；TUI 保留兼容并补必要新操作，已有 PWA 不新增产品投入。

统一批次、并行依赖与联合验收维护在
[Operant Beta 2.0 更新计划](../design/Operant-Beta-2.0更新计划.md)。该文件是本文的交付附件，
不改变三层架构权威。GUI 模块范围仍在客户端第 16.1 节，MP 详细子任务与状态仍在记忆草案第 13 节。

按七个主批次组织：基线/契约 → 基础任务/Host → 生命周期/项目 → 召回/协作 → 整理治理 →
经验技能/共享 → 综合验收。Agent1 持续负责实现、正式接入、交互/客户端测试和集成交付；
Agent2 按 UI 小项快速完成界面呈现、外观 bug 与视觉优化。七批是交付里程碑，不要求每批
重新启动两工具或同步等待。UI 代码实名交接后，Agent1 集成、启动指定 Reviewer 并负责真实验收；
在启动提示词明确授权的范围内，可按验收门继续下一里程碑。沟通与上下文规则见 memory/communication/README.md。
基础记忆里程碑要求 MP-0～MP-3，完整本轮要求 MP-0～MP-6 及全部 GUI 声明范围，不能互相代替。
新 GUI 记忆页直接使用新插件契约，既有记录由 Core 兼容投影和迁移承接，不先造一套旧页面再重写。
下文 Phase 与 GUI/MP 编号保留为能力和范围索引；执行排期以 Beta 2.0 附件为准。

<a id="gui-live-integration-plan"></a>

### Beta 2.0 GUI 范围：现有模块真实接入（GUI-LIVE-20260909）

记录身份：Codex。适用对象：所有 Agent。GUI-L0 已随 B2-1 交付，后续接入状态见 memory/current.md。现按 [Beta 2.0 统一批次](../design/Operant-Beta-2.0更新计划.md)
与记忆线并行，不再要求全部 GUI 完成后才开始记忆召回与治理。

让已有页面形成可日常使用的 Core → 正式协议 → GUI 闭环，同时推进完整记忆引擎与受控协作。
单个后端模块、生成 Client、候选包或启动 smoke 通过，均不能代表对应 GUI 已完成。
详细范围、缺口依据和唯一接入任务表维护在
[客户端规范第 16.1 节](../UI_UX_DESIGN_SPECIFICATION.md#gui-live-integration-plan)。

模块范围（具体先后与并行见统一计划）：

1. 修复 Live 路由与 Mock 隔离，包括旧深链接、嵌套路由及状态切换；核对真实运行版本与实施基线。
2. 优先完成聊天历史/取消、ModelProfile 与 Agent 配置、任务列表/详情和运行入口；先统一 Task、
   RolePreset、AgentInstance、Session/Run 的语义，再以单一 Schema 补齐所需 Query/Command/Event。
3. 补齐项目管理、新契约下的记忆查询/确认/修改/停用、设置和保留治理，以及 Graph/Team 的选择、
   编排、运行监控、群聊、任务板和 Agent 个人页，消除日常操作手填内部 ID 的要求。
4. 接通技能信任/安装/项目装载与插件生命周期所需的最小 Host 能力，并保留 MCP、调度、审批、
   Remote 已有链路；不能靠客户端本地状态模拟安装、授权或运行。

基础记忆界面直接对接 MP-0 冻结、MP-2 实现的新协议，保留现有数据的来源/版本/Scope 兼容与迁移。
通用插件生命周期复用 [MP-0 至 MP-2](../项目架构.md#memory-plugin-plan)，不另造 Host 或永久内置记忆引擎。
GUI 独立模块与 MP 后续阶段按依赖交错；Graph 智能创建、任意节点等仍不在本轮完成范围。

验收以真实桌面 GUI 的用户流程为单位：选择/配置 → 执行 → Core 持久化 → 刷新/重启回读 →
错误、取消、审批和断线恢复。每项分别记录后端能力、正式协议、GUI 操作与真实验收状态；
缺口未补齐的模块不得标记完成。具体门禁见客户端规范；实际实现时才更新 `PROJECT_ARCHITECTURE.md`。
