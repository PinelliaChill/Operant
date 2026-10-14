# Operant 客户端设计历史归档

> 归档日期：2026-09-22；归档身份：Agent4；适用对象：所有 Agent
>
> 本文件保存原 `docs/UI_UX_DESIGN_SPECIFICATION.md` 第 19–28 节的历史正文。原文中的记录身份、署名、状态、失败限制和阶段新增能力均保留；本文件只修正了因移动产生的相对链接。
>
> 这些内容用于追溯早期修订、GUI demo v6–v10 与旧客户端对接需求，不是当前实现、协议或排期依据。当前设计约束见[客户端设计规范](../UI_UX_DESIGN_SPECIFICATION.md)，当前实现以 [PROJECT_ARCHITECTURE.md](../PROJECT_ARCHITECTURE.md)、源码与测试为准，当前协作职责以 [AGENTS.md](../../AGENTS.md) 为准，后续未实现目标见[Harness 待实现目标](../design/Operant-Harness待实现目标.md)。
>
> 原规范中指向第 16.1 节的链接已改为回到当前正文的短路由；当前正文保留第 19–28 节的锚点入口，避免旧链接断开。

---

<a id="history-v02"></a>

## 19. v0.2 相对初版的关键修订

1. GUI/TUI 改为“物理解耦、协议统一”，删除 TUI 直读内部 Event Bus 的设计。
2. REST/SSE 成为默认协议，WebSocket 只用于真实双向场景。
3. Tauri 降为薄壳，PTY、命令、权限和安全裁决回到 Operant Core。
4. 远程状态、事件流状态和本地 UI 状态分层，不再全部交给 Zustand。
5. Graph 明确区分 Draft Definition、Published Revision 和 Workflow Run。
6. 智能创建只生成可审查的 Draft/Patch Proposal。
7. Agent 通信采用 Canonical Message + Mailbox Projection，避免其他 Agent 上下文污染。
8. 删除“展示内部思考”，改为可审计的结构化摘要和证据。
9. 审批模型改为动态 ModelProfile，不硬编码示例模型；LLM 不能覆盖 Policy DENY。
10. 缓存清理改为显式生命周期，不再解析用户自然语言自动删除。
11. 补充响应式、无障碍、对比度、真实焦点、深色与减少动画要求。
12. 调整实施顺序：协议与现有会话 GUI 优先，Graph Runtime 先于画布，Tauri 最后。

<a id="history-v03-remote"></a>

## 20. v0.3 Remote Control 修订

1. 将响应式 Web GUI 同时定义为可安装 PWA，不为 2.0 复制 iOS/Android 业务客户端。
2. 将 Remote Control 与 Remote Execution Target 分离，Remote PWA 只控制同一用户的本地 Core。
3. 增加 HostInstance、RemoteDevice、RemoteSession、RemoteCommandReceipt、Device Scope 与撤销状态。
4. 采用局域网/VPN 直连优先、用户自托管 Relay 回退；Relay 只路由端到端加密 Envelope。
5. 明确 Relay Ack 与 Host Ack 不同，Queue、Steer、Approval、Cancel 在 Core 确认前保持未确认。
6. Remote Control MVP 提前到 UI-0/UI-1，随后随 Graph、Team、TUI 和 Tauri 增量扩展。
7. 增加手机宽度、Host/Client/Relay 分别断线、重复 Command、Ack 丢失、设备撤销和传输切换验收。
8. 明确 Operant 2.0 保持单用户、单 Core、本地 SQLite 权威，不建设 SaaS、多用户或分布式控制面。

<a id="history-agent-routing"></a>

## 21. 客户端 Agent 身份路由更新

2026-09-20 按 User 要求改用编号身份；以下为默认分工，实际分工以任务包为准；记录身份：Agent1；适用对象：所有 Agent。

1. Agent2 负责界面呈现、布局/样式/动效和外观类 UI bug，发挥快速迭代优势。
2. Agent1 负责正式 Client 接入、业务交互/状态逻辑、交互与客户端测试、Tauri/TUI 工程、联调和验收；
   不把真实桌面、浏览器操作或端到端测试作为 Agent2 的交接前置条件。
3. Agent1 通过沟通区发布含字段/props、状态说明、负责路径的 UI 小项；Agent2 可反馈接口需求，
   先完成无依赖的视觉部分。呈现占位必须标为开发数据，不能作为 Live 状态或验收证据。
4. 每个文件/组件同一时段只有一个写入负责人；UI 交付后由 Agent1 接入和测试，后续视觉修改重新明确归属。
5. 其他 Agent 按本批分工参与，记录独立编号；历史作者、确认与验收记录保持不变。

<a id="history-demo-v6"></a>

## 22. v0.4 GUI demo v6 改版修订（2026-08-30）

1. 模式切换收敛为 rail 顶部唯一切换钮：恒显当前模式（对话=气泡图标、协作=双人图标，当前模式
   高亮），菜单选择「对话/协作」，模式由 URL 路径派生（`/chat`、`/collab`）；rail 移除独立
   「会话」项，移动端底部 tab 保留会话入口。
2. 可折叠布局：会话/协作情境侧栏在所有 ≥960px 桌面宽度可折叠（收起=完全隐藏+边缘细把手），
   `< 960px` 维持抽屉；协作画布节点面板与节点配置面板各自可折叠；面板状态按 localStorage
   （`operant.panel.*`）持久化，默认展开；最左 64px 图标 rail 固定不折叠。
3. 协作模块改为“模板 → 实例”模型：工作流模板可创建多个实例（一对一群聊会话，自动命名
   「{模板名} · 实例 N」，会话重命名即实例重命名）；总览重排为模板为主双栏，侧栏分草稿/实例/
   运行进度三组，实例菜单提供重命名/归档/删除/恢复，运行中实例禁止归档/删除/重命名，存在
   活跃实例的模板禁止发布新版本；「运行」概念统一改名「运行进度」（侧栏组、总览 chips、监控
   面板、状态栏、页面标题）。
4. 审批拆分：设置新增「审批与权限」tab（权限策略模式卡 + 分类规则表，roving tabindex 保持）；
   rail「审批」页改为审批收件箱（待处理审批富卡片 + 「近期决定」历史区），旧页内嵌「待处理
   审批」section 取消。
5. Agent 合并：设置「角色预设」tab 移除（旧深链 `?tab=roles` → `/agents`）；Agent 页为单一
   Agent 体系的卡片网格 + 可编辑详情抽屉 + 页头「+ 新建 Agent」弹窗表单。
6. 设置 tab 最终清单：模型配置 → 审批与权限 → 记忆治理 → 保留策略 → 远程与设备。

<a id="history-demo-v7"></a>

## 23. v0.5 GUI demo v7 改版修订（2026-08-31）

1. **模式钮交互重构与遮挡修复**：
   - 提升 rail 层叠上下文（z:30）高于侧栏（z:20），彻底解决弹出模式菜单被侧栏遮挡问题，移动端抽屉内同逻辑对齐；
   - 引入条件菜单交互：非主页点击直接回到当前模式主页（对话→`/chat`；协作→`/collab`），不切换模式；已在主页点击打开切换菜单（当前项置灰不可点，另一项点击切换跳转并归还焦点）。
2. **项目 = 工作区目录绑定模型**：
   - `DemoProject` 扩展绑定 `path` 目录路径与 `isDefault` 标识；新增默认项目「工作区」（绑定演示 workspace 路径，置顶且不可删除），保留现有演示项目；
   - 会话挂项目（`projectId`），工作流模板挂项目（目录条目新增 `projectId`，实例随模板归属），运行进度保持全局；
   - 项目页重做为「会话」+「工作流」双区块（工作流行=模板名+版本+实例数，点击进入协作总览/会话），页头提供「+ 新建项目」弹窗表单；侧栏项目组头增加「+ 新建项目」入口与默认项目置顶标识；
   - 新建会话默认归属「工作区」，支持改选项目。
3. **输入框高级控制（/ 命令面板 + @ 统一上下文选择器）**：
   - **斜杠命令面板**：输入 `/` 唤起并实时过滤，支持三组命令：
     - 导航：`/帮助`、`/模型`、`/角色`、`/审批`、`/新建会话`、`/导出追踪`；
     - 会话动作：`/重命名`、`/清空上下文`、`/压缩上下文`、`/切换演示模式`；
     - CLI 风格占位（后端实现要求）：`/init`、`/review`——执行后明确提示“需后端实现”并立项跨端契约提议；
     - `/帮助` 打开命令速查 Modal（包含命令说明与后端要求标注）。
   - **@ 统一上下文选择器**：输入 `@` 唤起，支持 Agent / 文件 / 会话 / 工作流（模板+实例）四组对象多选与全键盘导航，选中项以 Context Chips 浮显于输入框上方，支持单个移除并在发送消息时携带；保留原「添加上下文」按钮。
4. **设置中心「通用」Tab**：
   - 新增「通用」tab 插在「模型配置」之后，包含通用外观（浅色/深色/跟随系统，与 rail 主题钮双向联动；舒适/紧凑字号密度）、快捷键速查（全局/输入框/画布只读表）、关于与诊断（版本、连接状态、演示模式说明、一键重置演示数据）；
   - 设置最终 6 tab：模型配置 → 通用 → 审批与权限 → 记忆治理 → 保留策略 → 远程与设备。

<a id="history-demo-v8"></a>

## 24. v0.6 GUI demo v8 改版修订（2026-08-31）

1. **设置中心主次层级导航重构**：
   - 彻底解构原单一聚合「通用」页，重构为 **4 大主类导航**（通用偏好、模型与环境、安全与治理、系统与设备）+ **11 个分类 Sub-Tabs**；
   - 涵盖外观模式/强调色/信息密度/代码行号、快捷键分类速查与重置、流式输出与推理等级偏好、自动 Compaction 阈值、终端 Shell 与沙箱模式、HTTP/HTTPS Proxy、审批策略模式卡与规则表、FTS5 记忆治理、数据保留与审计、远程设备配对及系统诊断日志导出。
2. **多入口联动调度中心升级**：
   - 调度任务支持 **Cron 周期表达式** 与 **一次性倒计时 (Timer)** 双模式，调度目标支持绑定工作流模板、指定 Prompt 指令任务或系统健康巡检；
   - 支持列表「立即运行 (Run Now)」、启停开关与删除；
   - 在协作工作流总览与会话内部工具栏提供「添加到调度 / 设为调度」联动入口，点击携带参数自动预填弹窗创建。
3. **技能与扩展专区深度升级**：
   - **技能专区 (`/skills`)**：遵循 standard Agent Skill 规范（包含 `SKILL.md` frontmatter 元数据、`scripts/` 与 `references/`）；支持顶部「扫描本地技能」（模拟扫描本地 `~/.gemini/antigravity/builtin/skills` 与 `.operant/skills/` 目录）、「+ 新建技能」弹窗表单与「技能详情抽屉」（查看 SKILL.md 文档、工具依赖与脚本清单）；
   - **扩展专区 (`/extensions`)**：支持管理 Model Context Protocol (MCP) Server (stdio 子进程 / sse 远程端点) 与本地系统插件，提供「测试连接 (Ping)」、可用工具清单展开与启停开关；
   - 涉及的本地 Skill 扫描与 MCP stdio/sse 协议在跨端契约提议 COM-20260831-004 中立项向 Codex 提出。

<a id="history-demo-v9"></a>

## 25. v0.7 GUI demo v9 改版修订（2026-08-31）

1. **设置中心 Codex 式单流纵向滚动重构**：
   - 移除右侧顶部横向 Sub-Tabs 切换栏，重构为左侧 4 大主类导航（通用偏好、模型与提供商、安全与治理、系统与设备）+ 右侧全量单页平滑纵向滚动流；
   - 每个大分类包含清晰的三级标题模块区块（Section）：
     - 【通用偏好】：`### 外观与界面`、`### 快捷键速查与自定义`、`### 对话与交互偏好`；
     - 【模型与提供商】：`### 已配置模型概览`、`### 主流厂商 OAuth 快速连接`、`### 模型提供商 (Hermes 模式)`、`### 终端与执行环境`、`### 网络与代理`；
     - 【安全与治理】：`### 审批与权限策略 (PolicySettings)`、`### 记忆治理 (SQLite FTS5)`、`### 保留与审计策略`；
     - 【系统与设备】：`### 远程与设备管理 (RemoteView)`、`### 关于与系统诊断`。
2. **Hermes 风格模型提供商与上游模型自动扫描导入**：
   - 在「模型与提供商」页面中打造以 Provider 为中心的模型生态管理体系；
   - **置顶卡片**：`本地 / 自定义端点`（提供 Ollama [11434]、vLLM [8000]、LM Studio [1234]、Llama.cpp [8080] 与自定义 Base URL 预设切换与一键扫描）；
   - **搜索框**：快速过滤 11 个主流云端提供商（"搜索提供方…"）；
   - **预置主流云端 Provider**：DeepSeek、Anthropic、OpenAI、Google Gemini、GitHub Models、OpenRouter、SiliconFlow 硅基流动、Fireworks AI、xAI、MiniMax、智谱 GLM、Moonshot / Kimi、OpenCode Zen / Go；
   - **一键扫描与批量导入**：点击「🚀 扫描上游模型 (Discover Models)」自动拉取上游可用模型目录，支持全选与单选，提供「⚡ 一键全量导入」与「📥 导入选中项」，自动推断上下文窗口、Token 预算与推理努力等级，保存为系统 ModelProfile 并联动已配置模型概览。
3. **主流厂商 OAuth 账号授权登录（Hermes / Claude Code 模式）**：
   - **OAuth 快速连接专区**：在模型设置页顶部提供 GitHub Models、Anthropic、OpenAI、Google Gemini、OpenRouter 的一键快速连接卡片；
   - **双认证模式切换**：支持 OAuth 账号登录与 API Key 凭据双模式切换；
   - **OAuth 2.0 PKCE 授权弹窗体验**：模拟浏览器跳转握手、权限 Scope 确认、Access Token 本地持久化与退出登录，授权成功后自动拉取专属模型。

<a id="history-demo-v10"></a>

## 26. v0.8 GUI demo v10 改版修订（2026-08-31）

1. **模型提供商 5 层瀑布流架构重构**：
   - 彻底重构模型与提供商页面，严格按照 5 个递进层级瀑布流排布：
     - ① **已连接的账号 (Connected Accounts)**：展示已登录的 OAuth 账号卡片，展开展示该账号已授权/激活的模型列表（带测速延迟、启停开关与删除），支持「重新授权」与「断开连接」；
     - ② **已接入的 API (Connected APIs)**：展示已配置 API Key 的提供商卡片（DeepSeek, SiliconFlow, Ollama 等），展开展示旗下模型列表，支持「重新扫描」与「移除 API」；
     - ③ **连接账号 (Connect Account - OAuth)**：主流 OAuth 厂商卡片列表，一键发起 OAuth 2.0 PKCE 授权；
     - ④ **以 API 接入 (Connect with API)**：热门云端提供商卡片列表，输入 API Key 与 Base URL 后扫描拉取并入层级 ②；
     - ⑤ **自定义提供商 / 本地端点 (Custom API Provider & Local)**：置顶/快捷连接 Ollama、vLLM、LM Studio、Llama.cpp 与自定义端点。
2. **新建会话与协作实例项目工作区绑定**：
   - 新建会话弹窗 (`ChatSidebar.tsx`) 增加「归属项目工作区」下拉选择器（默认选中「工作区」）；
   - 新建协作实例弹窗 (`WorkflowOverview.tsx`) 增加「所属项目工作区」下拉选择器，创建后与指定项目绑定挂载。
3. **协作侧栏「模板」标准分组**：
   - 协作侧栏 (`CollabSidebar.tsx`) 划分为 4 大分组：`草稿 (Drafts)`、`模板 (Templates)`、`实例 (Instances)`、`运行进度 (Runs)`；
   - 点击「模板」项直达 `?view=home&tpl={id}` 深链定位。
4. **会话与协作侧栏拖拽调宽与自动收起**：
   - 在侧栏右边缘植入拖拽分割条 (`RailLayout.tsx`)，支持鼠标拖拽在 `180px~480px` 之间自由缩放；
   - 向左拖动宽度小于 `160px` 时自动触发平滑收起折叠；
   - 用户自定义侧栏宽度持久化至 `localStorage`。
5. **协作总览双栏折叠与全宽自适应**：
   - 协作总览界面 (`WorkflowOverview.tsx`) 默认全宽展示当前选中模板详情，彻底消除与侧栏的视觉重复；
   - 顶部提供「展开/收起模板列表」切换按钮，展开时呈现左模板列表+右详情，收起时自适应全宽。

<a id="history-v09-client-backend"></a>

## 27. v0.9 客户端-后端对接需求清单（2026-09-01）

本章 A/B/C 为当日历史对接快照，不能替代 Beta 2.0 实施基线。Memory、知识库与插件生命周期按
新 MP 契约统一；其余历史需求未全部纳入本轮，具体交付/后续范围以统一计划第 2 节为准。

> 定位：本章由 ZCode 依据 GUI demo v10 现状、`docs/PROJECT_ARCHITECTURE.md` 实现权威和
> `memory/communication/` 契约台账盘点，汇总前端对接真实 Core 所需的后端能力与缺口，供
> Codex 评估契约与排期。本章是需求索引，不是契约权威：逐项正式语义以对应 COM 条目为准；
> 标注「缺口」且尚无 COM 条目的项，由 COM-20260901-001 统一立项，经 Codex 确认后方可实施。
>
> 状态图例：**A** = 后端已实现且契约已确认，客户端可直接对接；**B** = 契约已确认或已立项
> （后端实现中或待排期），客户端按契约准备对接；**C** = 尚无契约的后端缺口，需新建能力或语义。

<a id="history-v09-session"></a>

### 27.1 会话、消息与上下文

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 会话列表、消息流、跨会话历史 | Thread/Turn/八类 Item 只读接口 + 已提交 Cursor 的 SSE 回放（demo 现用 fixtures） | B（COM-20260828-001 已确认） |
| 群聊 @Agent 寻址、默认接收者、Agent 个人 DM 回显 | Thread/Item 的受众/寻址语义，后端暂无 | C（缺口，COM-20260901-001） |
| 状态栏上下文窗口用量（tokens/%） | ContextRevision metadata-only Query（hash/source/Watermark/Stub） | B（COM-20260829-001 已确认） |
| `/清空上下文`、`/压缩上下文` | Context Baseline 提升与追加式 THREAD_ITEMS Compaction 的类型化 Command | A（Phase 1D PR #9 已实现，待合并验收） |
| `/init`、`/review` 等 CLI 风格命令 | Workspace 注册校验、只读 Reviewer 审查的类型化 Command | A（Phase 1D PR #9 已实现，待合并验收） |
| 消息内 Artifact 附件展示、下载、导出 | capability 票据保护的 `/content`、`/download`、`/export`，path-free 边界 | B（COM-20260831-001，后端 Phase 1C 已实现，待客户端确认对接） |
| `@文件` 上下文选择（v7 起） | 面向客户端的工作区文件列表/浏览 API（现有 workspace 文件工具仅为 Agent 内部工具） | C（缺口，COM-20260901-001） |
| 会话内「知识库」Tab | 与三类 Memory（Working/Episodic/Project）的映射语义确认，或新建知识库实体 | C（映射待确认） |
| 会话内「任务」Tab（任务卡片/清单） | 面向会话的 Task 实体与查询 API，后端暂无（Evaluation/Workflow 实体不等价） | C（缺口，COM-20260901-001） |
| 通知与未读计数 | 基于已确认的 SSE 事件流客户端聚合，无需新增后端 | B（事件源已确认） |

<a id="history-v09-workflow"></a>

### 27.2 工作流（模板 → 实例）

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 编排画布全链路（草稿保存/校验/发布/列表） | 通用 Graph Runtime 与 Definition Compiler（`PROJECT_ARCHITECTURE.md` 尚未完成清单在列）；MockClient 已占位全套接口 | C（大项缺口） |
| live 模式零草稿时画布新建入口 | 依赖草稿保存 API（同上） | C（随上） |
| 实例=会话绑定发布版本；实例生命周期（归档/恢复/删除）；运行中守卫 | 实例实体与生命周期 Command、运行状态关联（demo 数据层已实现，D-027） | C（缺口，COM-20260901-001） |
| 模板发布封锁（活跃实例跨版本聚合校验） | 发布前置校验 API（随实例实体） | C（随上） |
| 运行进度列表/详情/取消/恢复/Trace 导出 | WorkflowRun/Event/Trace 后端已实现；M0/Phase 0 契约已确认 | A（客户端对接即可） |

<a id="history-v09-project"></a>

### 27.3 项目与工作区

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 项目=工作区目录绑定：默认「工作区」项目、新建项目（名称/颜色/目录路径） | Project 实体与项目 CRUD API；Session/Thread 已有 workspace 绑定字段，但无项目概念与列表接口 | C（缺口，COM-20260901-001） |
| 会话与工作流模板挂项目、新建会话/实例选择归属项目 | 项目归属字段与过滤查询（随 Project 实体） | C（随上） |
| 会话侧栏与协作侧栏按项目工作区树形可折叠分组 | `GET /v1/projects` 聚合返回各项目绑定的会话与实例 ID 列表及摘要统计 | C（缺口，COM-20260901-001 增量） |

<a id="history-v09-approval"></a>

### 27.4 审批

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 审批收件箱（待处理列表、批准/拒绝、审计、来源会话深链） | M0 Approval Request/Decision/Audit 已实现，跨进程可查 | A |
| 全局策略模式（询问/工作区写入/完全开放/自动审批）+ 三类动作分类规则 | Role Tool Policy 双层校验已实现；「全局模式切换 + 分类规则」的服务端持久化与裁决语义需对齐或新建 | C（部分缺口，可论证映射 Role policy） |

<a id="history-v09-provider"></a>

### 27.5 模型与提供商

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 模型配置卡 CRUD、发现模型、健康检查 | Model Profile 全套已实现 | A |
| 本地端点预设扫描（Ollama/vLLM/LM Studio/llama.cpp/自定义 Base URL） | 现有 discoverModels（OpenAI-compatible `/v1/models`）可覆盖 | A |
| Hermes 5 层瀑布流架构（已连接账号、已接入 API、连接账号 OAuth、热门 API 接入、自定义端点） | 多 Provider 注册表与批量模型发现、推断与导入 API（现 discoverModels 为单端点） | C（缺口，COM-20260901-001） |
| 主流厂商 OAuth 2.0 PKCE 授权登录（GitHub/Anthropic/OpenAI/Gemini/OpenRouter，Token 本地持久化） | OAuth 流程、Token 存储与安全边界全部未实现（现凭据模型为 secret_ref）；需 Codex 主导安全设计 | C（缺口，安全敏感） |

<a id="history-v09-skills"></a>

### 27.6 技能与插件 (MCP)

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 标准 Agent Skill 扫描/解析/新建（`SKILL.md` frontmatter + `scripts/`） | Skill Discovery Runtime | B（COM-20260831-004 阶段一，已立项未实现） |
| 项目工作区技能软链接隔离（在工作区 `.operant/skills/` 维护 symlink 并由 Agent Loop 按工作区加载） | 工作区 Skill 软链接管理与状态查询 API（`POST/DELETE /v1/workspaces/{id}/skills/links` 或工作区 Skill Manifest 契约） | C（缺口，v11 新增） |
| MCP Server（stdio/sse）管理、连接测试、工具接入 Action Gateway | MCP Adapter | B（COM-20260831-004 阶段二） |

<a id="history-v09-scheduling"></a>

### 27.7 调度

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| Cron/Timer 调度任务、目标绑定（工作流/Prompt/巡检）、Run Now、启停/删除 | Scheduler Runtime（持久租约、fencing、幂等 Command） | B（COM-20260831-004 阶段三） |

<a id="history-v09-governance"></a>

### 27.8 安全与治理

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 记忆治理（FTS5 搜索、候选确认/激活/停用） | 三类 Memory + FTS5 已实现 | A |
| 保留与审计（Retention/Pin/宽限期/Trash/恢复、显式物理删除、path-free 审计、孤儿修复） | Phase 1C 已实现 | B（COM-20260831-001，待客户端确认后对接） |
| 审批策略模式与规则 | 见 27.4 | C（部分） |

<a id="history-v09-remote"></a>

### 27.9 远程与设备

| 前端能力（demo 现状） | 需要的后端 | 状态 |
| --- | --- | --- |
| 远程与设备管理（设备配对、远程控制） | Host Connector、自托管 Relay、Remote Gateway、RemoteDevice/RemoteSession 均为目标态，后端未实现 | C（远期） |
| live 模式直连（`clientMode=live` 访问 `/web`、`/v1/*`） | Web 身份认证、设备配对与远程访问控制（`PROJECT_ARCHITECTURE.md` 尚未完成清单在列）；在此之前 `/web`、`/v1/*` 不得暴露公网 | C（安全前提） |

<a id="history-v09-client-only"></a>

### 27.10 纯客户端能力（无需后端对接）

主题与外观、字号密度、快捷键速查与自定义 UI、斜杠命令面板与 `@` 选择器交互本身、布局折叠与
侧栏拖宽、表单校验、演示模式开关（mock 数据层）——这些不产生后端依赖。

<a id="history-v09-order"></a>

### 27.11 对接顺序建议（供排期参考，非决策）

以下为历史建议，当前优先顺序由[第 16.1 节](../UI_UX_DESIGN_SPECIFICATION.md#gui-live-integration-plan)取代；A/B/C 也不代表现版已验收。

1. 不改后端即可启动：A 项对接（运行进度、审批收件箱、模型、记忆）与 B 项中契约已确认部分
   （Thread 回放、ContextRevision 用量、Artifact capability 读、Retention 状态机）；
2. Phase 1D 合入后：`/` 命令与上下文操作（COM-20260831-002）已具备后端支持；
3. C 类缺口按 COM-20260901-001 逐项立项确认后实现，优先级建议：项目=工作区实体（含树形聚合与软链接技能隔离） → 实例
   生命周期与发布封锁 → 客户端文件浏览 → 群聊寻址 → 多 Provider 批量导入 → Task/知识库映射 →
   OAuth 授权（安全设计单独评估）→ Remote（远期）。

<a id="history-v10-client-backend"></a>

## 28. v1.0 客户端-后端对接历史修订（2026-09-01）

本节保留历史需求与示例；下列路径、软链接和 API 方案不是 Beta 2.0 的执行指令，须由 MP-0/
B2-1 按 Host、所有权与安全边界核定。历史合并状态不表示当前状态，实施前核对实际提交。

> 记录身份：Antigravity
>
> 适用对象：所有 Agent

1. **项目工作区技能隔离与软链接规范**：
   - 技能中心（`SkillsView.tsx`）按项目工作区隔离启停技能；
   - 底层实现机制：全局技能集中安装于 `~/.gemini/antigravity/builtin/skills/` 与 `~/.gemini/config/skills/`，当在特定项目工作区启用技能时，在工作区根目录下创建软链接 `📁 {project.path}/.operant/skills/{skillName} ➔ {skill.path}`，关闭时断开软链接；
   - 后端 Agent Loop 在该工作区执行时，动态扫描 `.operant/skills/` 下的有效软链接注入 Tool 组；
   - 契约需求：需要后端提供 `POST /v1/workspaces/{id}/skills/link` 与 `DELETE /v1/workspaces/{id}/skills/link`，或通过 Workspace Skill Manifest 进行原子生命周期管理。
2. **会话与协作双模式项目树形组织**：
   - 会话模式（`ChatSidebar.tsx`）与协作模式（`CollabSidebar.tsx`）全面落地项目工作区树形可折叠分组；
   - 侧栏顶部保留「📌 置顶会话」与「草稿/模板」，其余会话与实例严格按 Project 归类并支持项目级折叠/展开与快速新建；
   - 契约需求：`GET /v1/projects` 需要支持聚合返回各项目绑定的会话与实例 ID 列表及摘要统计。
3. **侧栏平滑缩放与全站命名统一**：
   - 侧栏支持 `180px~480px` 鼠标拖拽调宽，向左拖动 `<160px` 自动折叠，并在折叠状态下支持右边缘向右拖拽直接拉出展开；
   - 全站统一重命名为「插件与MCP」。
4. **Phase 1D 后端支持就绪**：
   - `/清空上下文`、`/压缩上下文`、`/init`、`/review` 命令已在 Codex 的 Phase 1D（PR #9）中完整实现，待 User 合并后即可全面开启正式 Client 对接。
