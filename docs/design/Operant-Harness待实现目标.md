# Operant Harness 待实现目标

> 清单编号：HARNESS-GOALS-20260921；记录身份：Agent1；适用对象：所有 Agent
> 日期：2026-09-21；2026-09-27 更新（Codex）：前三部分已合并；第四部分实施中，状态以源码、验收及 Git/PR 为准。
> 核对基线：`74a0251f0e17a46a1d66c4a5d293b4ee0b11e453`；本机对应工作树 `.worktrees/ui-install-main`。

## 1. 定位与维护

本文件把 User 的原始多 Agent Harness 设想、后续目标修订与源码差距收敛为后续目标清单。
Beta 2.0 的 B2-1～B2-7 候选交付完成，不等于完整长期愿景完成；本清单不重开历史批次、不改判旧验收。
当前已具备 Python Core、会话与状态持久化、权限/审批、Graph/Team 基础、记忆主链、Skill/MCP 管理及 GUI/TUI 基础。
主要剩余工作是补充执行能力，以及把已有后端能力接成用户能够直接使用的流程。

- Kernel 目标以[项目架构](../项目架构.md)为准，客户端目标以[客户端规范](../UI_UX_DESIGN_SPECIFICATION.md)为准。
- 本文件是两份目标文档的交付附件，只维护差距、后续目标与建议顺序，不成为第四份架构权威。
- 下表“核对基线”是指定版本的只读源码核对，不代表本轮重跑了 GUI、模型或端到端验收。
  当前根目录仍是旧代码分支，不能用它的同名源码替代上述版本。
- 下表保留 `74a0251` 差距基线；已实施部分见[第一部分验收](session-workbench/acceptance.md)、[第二部分验收](orchestration-system/acceptance.md)和[第三部分验收](config-approval-task-control/acceptance.md)。后续启动时按最新代码复核差距；
  不强制每个 ID 独立开批次，也不逐项复制已有治理规则。
- 本地协作总进度写未纳入 Git 的 `memory/current.md`。完成目标后在本表补交付证据入口，
  并同步实施版本的 `docs/PROJECT_ARCHITECTURE.md`；不靠改清单状态替代真实交付。

## 2. 功能差距与待实现目标

“部分”表示已有底座或部分入口，不能据此宣称整项原始体验已实现。

第一部分完成 H-01/H-02、H-10/H-11/H-13 常用入口和 H-18 会话基础；第二部分完成 H-03～H-06；
第三部分实现 H-08/H-09/H-12。各部分实际证据与限制见上方验收记录。下面仍是 `74a0251` 的历史差距基线，
不以修改清单代替当前源码、运行和验收。

| ID | 用户目标 | 核对基线已有能力与缺口 | 待交付结果 |
| --- | --- | --- | --- |
| H-01 | 会话内创建子 Agent，父子会话折叠管理 | 部分：Thread 有 `parent_thread_id`；GUI 映射未保留父级，侧栏平铺；没有完整子 Agent 创建/绑定流程 | 正式创建、取消和状态/结果回读；父子历史树；模型、权限、工作空间和上下文继承明确；GUI/TUI 可进入子会话 |
| H-02 | Agent 自主分工、定向通信与等待 | 部分：Team、Mailbox、Ack 与下一模型请求的消息投影存在；正式 Agent 工具缺少委派/发送/等待，读取 Mailbox 不等于唤醒空闲 Agent | Agent 可委派、定向发消息、等待和收回结果；事件唤醒、去重、消费位置、回复关联、取消传播及重启边界；用户群聊展示收件人 |
| H-03 | 可视化建立和调整 Workflow | 部分：Live 有创建/编译/发布/运行及 Team/任务板入口，主要是表单与节点列表；旧 Demo 画布不能算 Live | 真实节点编辑、拖拽连线、参数绑定、图校验、版本差异及运行状态展示；客户端继续消费 Core 权威投影 |
| H-04 | Agent、脚本、操作和 Loop 混合编排 | 部分：IR 定义多种节点和有界 Loop 状态约束；当前正式 `BoundedGraphExecutor` 只接受 Agent 节点，Timer 编译拒绝 | 补 Tool/Script、条件/分支、并行汇合、Loop/Timer 等必要节点的实际执行；循环预算、退出条件、取消和恢复可验证；不把类型定义当执行能力 |
| H-05 | 与 Agent 对话智能创建或修改 Workflow/Loop | 有完整定义提交和按角色创建 Graph 的基础接口；无自然语言草稿生成与确认发布流程 | 对话生成/修改草稿，显示差异、权限及预算，经同一编译器校验后确认发布；生成结果不能直接绕过审批执行 |
| H-06 | 定时与 Hook 驱动任务/Loop | 部分：Cron/Timer、持久队列和租约存在；通用定时派发到正式 Agent executor 的衔接尚需补齐/验证，缺 Hook/文件/Git/应用事件触发类型 | 从触发到真实模型执行及终态的闭环；事件触发按实际需求选择；重复事件去重、停用、取消、重启与失败处理明确；不重做已有记忆后台调度 |
| H-07 | 侧栏预览文件、代码与交互终端 | 部分：Live 主要提供只读目录 metadata；Agent 的命令工具不是用户交互终端，Demo TerminalViewer 不算交付 | 文件正文与代码预览、必要 Diff、带会话生命周期的交互终端；路径/权限和取消边界保留；完整 IDE 编辑器不列为本项前置 |
| H-08 | 全局→项目/工作区→Agent/Run 的配置继承 | 部分：模型连接、Role 提示词、Effort 和部分预算可编辑；完整提示词继承、权限/最大输出/温度/Skill/MCP 绑定缺统一入口 | 配置继承、覆盖、恢复默认及有效来源可见；参数按 Provider 能力显示；修改对新运行/下一轮的生效边界明确，不暗改活动 Run 的冻结快照 |
| H-09 | 审批模型自动处理灰区申请 | 部分：Policy 与 Reviewer Adapter 存在，正式入口默认未注入实际模型；严格度、模型选择及自动触发未形成完整体验 | 通过 Discovery 选择独立审批 Profile；严格度和自定义规则、最小脱敏输入、自动触发、人工回退/关闭及审计；Reviewer 不能覆盖硬 DENY |
| H-10 | `@` 文件/会话，先简介后按需读取 | 部分：Core 接受 references、行范围及 Token 限制，Live 发送未接引用选择器 | 选择文件/会话/允许的对象，先加载摘要与来源，Agent 按需请求正文；引用版本、Token 和权限复核可见，不自动全量注入其他会话 |
| H-11 | 会话内显式调用命令、插件和 Skill | 部分：后端固定 Slash Registry 有 `/init`、`/review`、`/clear-context`、`/compact-context`；Live 未形成统一调用入口 | GUI/TUI 命令发现、补全、参数校验与执行反馈；Skill/插件注册命令并按权限调用；管理页安装/启停不能替代显式调用验收 |
| H-12 | Goal、Plan、BTW 和默认能力集 | Goal 只有压缩摘要字段；固定 Planner 不等于正式 Plan 产品；BTW Sidecar 后端已有但会话命令入口不足 | 持久 Goal 的预算/状态/完成条件；可修改和追踪的 Plan；接入只读 BTW 与显式提升结果；grill-me、docx/pptx/pdf、create/find skills 作为可安装的默认能力包逐项验证，不硬塞进 Core |
| H-13 | 上下文占用详情与手动/自动压缩 | 部分：Core 自动压缩和手动 API 存在，Live 记忆检查器已有；主会话缺统一占用与压缩操作 | GUI 占用指标与详情、手动压缩/清理入口、自动压缩前后解释；TUI 提供等效信息；保留目标/约束/决定/未完成项/来源，按真实长任务评测质量 |
| H-14 | 会话/群组临时资源与缓存治理 | 部分：Provider CacheObservation、Artifact 保留/回收和插件卸载清理存在；无统一临时资源自动清理流程 | 分类展示资源大小/归属/保留原因，确认完成后的 TTL、未回复 TTL、Pin/保留锁、预览与手动清理；自动清理只作用于可安全清理的临时资源 |
| H-15 | 通用模块化与可插拔扩展 | 部分：现有正式 PluginManifest 使用 memory SDK，主要覆盖提取/召回/维护/索引事件；Skill/MCP 不能代表任意模块均可替换 | 按需开放工具、命令、事件、模型/运行时适配和能力驱动接口，具备生命周期、版本兼容、权限与可核验安装；保留统一状态/安全/恢复内核 |
| H-16 | 插件化浏览器与电脑操控 | 部分：已有 capability、observe-before-act 和远程动作协议；不等于完整内置驱动 | 浏览器/桌面真实驱动与插件接入，目标绑定、操作前后证据、用户接管、凭据隔离及未知结果处理；以真实任务验证，不以接口或模拟 Target 代替 |
| H-17 | 易用的跨设备控制与受控远程执行 | 部分：Remote Control、Target 与传输/权限基础存在；生产远程和设备体验仍有限制 | 配对、配置、状态诊断、撤销和断线恢复形成用户流程，完成声明范围内真实远程验收；不由此扩大到公网、多租户或分布式 Core |
| H-18 | TUI 作为日常会话入口 | 部分：目前偏 Graph 运行监控、审批与部分经验/管理操作，普通会话交互不足 | 会话创建/历史/子会话、输入与引用、命令和上下文操作共用 Core 协议；复用 H-01/H-10～H-13，图形画布不要求终端对等 |

上述源码核对的高价值入口（路径均相对基线仓库，不是旧治理根代码）：

- H-01/H-02：`src/operant/domain/threads.py`、`src/operant/tools/workspace.py`、`src/operant/application/team.py`，
  `clients/gui/src/live/liveAdapter.ts` 与 `clients/gui/src/features/chat/LiveSidebar.tsx`。
- H-03～H-06：`src/operant/application/graph_execution.py`、`src/operant/application/graph.py`、
  `src/operant/runtime/scheduler_integration.py`、`src/operant/api_b2_4.py`，`clients/gui/src/features/collab/LiveGraphTeamView.tsx`。
- H-07～H-13：`clients/gui/src/features/chat/LiveChatView.tsx`、`clients/gui/src/features/agents/LiveAgentsView.tsx`、
  `clients/gui/src/live/LiveContext.tsx`，`src/operant/api_phase45.py`、`src/operant/application/slash_commands.py`、`src/operant/application/context.py`。
- H-14～H-18：`src/operant/domain/threads.py`、`src/operant/contracts/b2_1.py`、`src/operant/plugins/protocol.py`、
  `src/operant/application/remote_execution.py`，`clients/tui/operant_tui/app.py`。

## 3. 建议优先级与首个可验证场景

以下为建议顺序，不是新授权或固定批次数。负责人启动时可依据依赖、收益和实际缺口调整，并说明取舍。

| 顺序 | 工作主题 | 主要目标与依赖 |
| --- | --- | --- |
| 1 | 会话式多 Agent 工作台 | H-01/H-02；并行补 H-10/H-11/H-13 的常用入口与 H-18 等效操作，按需要补最小配置和文件预览 |
| 2 | 可操作的编排系统 | H-03/H-04/H-06；智能创建 H-05 复用已明确的定义契约、编译器和执行能力，不先生成无法运行的节点 |
| 3 | 配置、审批与任务控制完善 | H-08/H-09/H-12；其中前两组所需的权限/预算配置应前置，不能为遵守排期而阻塞基本使用 |
| 4 | 扩展能力与运行治理 | H-15 支撑 H-16 及默认能力包；H-17/H-14 按真实远程与资源压力推进；H-07 交互终端按日常使用需要插入 |

首个建议验收场景：User 在普通会话中提出任务，主 Agent 创建两个不同职责的子 Agent，
按明确模型、权限和工作区执行；User 可查看父子会话、看到定向消息、取消其中一个；
主 Agent 收回其余结果并继续任务。其他 Agent 的私有上下文不被广播，取消/重复消息和重启结果有明确状态。
该场景覆盖底层执行与真实 GUI/TUI 操作，不以静态数据、手工拼 API 或 Demo 作为完整交付。

## 4. 沿用的设计边界与待细化方向

- **消息投递与群聊分开**：持久消息按收件人进入 Mailbox，用户界面按权限聚合展示；Agent 只消费发给自己的必要内容。
  摘要与引用默认先行，大文件按需读。唤醒、消费去重和取消是 Runtime 行为，不依赖提示词假定对方已看到。
- **缓存不混同权威数据**：按目标架构第 12 节，确认后 1 小时/未回复 3 天仅是临时资源的可配置默认策略，
  不是聊天历史、长期记忆、审批审计、未知副作用证据或仍可恢复 Run 的自动删除规则。
  Provider 内部缓存只观测/按其能力使用，不假定可以复制成所有 Agent 共享的本地缓存。
- **审批模型不是安全根**：只评估 ASK 灰区，不覆盖硬 DENY。模型与 reasoning effort 分开配置并从 Discovery 选择，
  不硬编码原始设想中的模型名称；拒绝后寻找其他方案不能变成绕过拒绝。
- **智能创建仍走正式契约**：草稿、差异、校验、确认发布共享一个 Definition/Compiler；生成脚本仍受正式工具权限约束。
- **压缩质量可评测**：已有自动压缩继续复用，后续比较策略时验证关键条件、引用、待办和工具调用配对是否保留，
  不预设某家方案天然最好，也不把 Token 减少直接等同于质量提升。
- **模块化边界明确**：Python Core、Rust 桌面壳的现有分工继续；只在实际瓶颈或平台能力需要时引入其他实现。
  共用协议与状态权威保留，新增扩展不引入第二套权限、数据库发布头或恢复裁决。

可借鉴的公开机制仅作为设计参考，不代表已移植：
[Claude Code 独立子 Agent 上下文](https://code.claude.com/docs/en/sub-agents)、
[LangGraph 执行快照与跨线程数据分离](https://docs.langchain.com/oss/python/langgraph/persistence)、
[Pi 工具/命令/事件扩展](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)。
不用为引用这些机制更换现有框架或语言。

## 5. 保留的工程限制

现有记忆链不重做，但真实任务中的形成、召回、纠正效果仍需持续验证，已有小样本和失败结果不外推为普遍收益。
B2-4 Host 原性能限制保留；本清单不自动授权重启专项优化。Docker、生产 Remote、历史安全扫描事项及正式签名/公证/分发
按各自未覆盖范围处理，不被新功能清单掩盖。安装配置、数据备份/恢复与升级体验可按试用反馈安排。
这些后续目标不会改变 Beta 2.0 的历史候选验收；前三部分的实现与 PR 不代表已更新本机安装、发布正式版本或迁移真实用户库。

## 6. 第四部分实施中记录（2026-09-27）

`codex/extension-runtime-governance` 从已合并第三部分的主线继续。当前已新增随 Core 发布的 Browser/Computer 能力插件注册表、源码摘要和版本绑定、启停/卸载、精确目标来源白名单、租约 Worker，以及 Chrome 专用临时 Profile 的状态预览和回收。真实 Chrome 本地任务通过正式 Target、Action Gateway、SQLite 回执和回环 HTTP 服务，验证观察、导航、非密码文本输入、点击、旧观察拒绝与禁用停止。受信 Tool 扩展已接 Role 白名单和 Agent/Graph 的正式入口，真实模型完成一次只读浏览器观察。详细证据和限制见[本部分验收记录](extension-runtime-governance/acceptance.md)。

此增量覆盖 H-15 的能力驱动接入和 H-16 的有限本机动作；通用第三方插件 Host 及浏览器/电脑完整操控仍未完成。macOS Computer 实际输入尚受辅助功能权限限制；H-17 真实跨设备、H-14 全局资源治理和 H-07 交互终端仍按第四部分条件与真实使用需要评估，不能由本机 Chrome 仿真推定已完成。
