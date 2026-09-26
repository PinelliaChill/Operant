# Operant

**让合适的模型分工协作，让验证过的经验留给下一次任务。**

当前处于 **Beta 阶段**。下面介绍当前 `main` 的源码能力；下载固定版本前，请先看版本区别。

| 版本入口 | 包含什么 |
| --- | --- |
| 当前 `main` | 已合并 B2-1～B2-7、UI 更新和会话式多 Agent 工作台 |
| [`v0.1.0-beta.1`](docs/releases/v0.1.0-beta.1.md) | 2026-09-14 发布的源码快照，范围截至 B2-3，不包含后来合入的功能 |
| 桌面候选 | 已有本机构建与安装验证；尚未提供正式签名、公证和自动更新的桌面发行版 |

更新概览见 [CHANGELOG](CHANGELOG.md)。本文按 2026-09-26 核实的主线编写；
[PR #28](https://github.com/PinelliaChill/Operant/pull/28) 已合并，但这些新增源码能力不会自动出现在旧标签或已安装 App 中。

## 项目定位

Operant 是一个自主设计并实现的、面向多模型协作与能力扩展的 AI Agent 工具。
它关注复杂编程任务中的四个问题：**多个 Agent 如何有效协作，安全审批如何减少打断，
历史经验如何继承，以及新工具和新模型如何接入。**

项目的核心思路，是把模型选择、角色分工、通信范围、执行权限和记忆复用放进同一套工作方式中。
你可以让不同模型分别负责规划、探索、编码和审查，也可以从一个简单的只读会话开始，
按任务复杂度逐步增加协作与自动化。

Operant 在本机运行，面向单个用户。任务状态与记录保存在本地，模型调用使用你自己的 API 配置。

## 核心设计哲学与优势

### 多 Agent 协同：分工明确，信息按需流动

**简单任务直接对话，复杂任务组织协作。** 会话模式提供直接的任务入口；协作模式围绕角色、
工作流和消息组织多个 Agent，让不同模型承担适合自己的工作，而不是每个角色都重复理解整个任务。

当前已支持会话内委派子 Agent、父子历史与定向后续消息，也保留角色工作流与团队协作。
子 Agent 默认继承模型、权限、预算和工作区，仅接收任务包；使用前需在角色工具权限中显式开启对应能力。消息按接收人和作用域传递，
配合上下文引用与压缩，控制无关信息进入模型的范围。这样可以减少重复传递，避免把全部群组消息
塞进每个 Agent 的上下文，同时保留可追溯的任务记录。

当前 GUI 已接入模板、成员、角色配置、编排发布与运行，以及群聊、定向消息、任务和工件板。
运行中的节点状态、实际上下文和记忆来源可以查看。自由拖拽画布和任意类型节点的完整执行仍是后续方向，
不能把演示画布当作正式编排能力。

### 分层安全与模型辅助审批：把注意力留给需要判断的操作

**让规则明确的操作自动执行，让需要判断的操作进入审批，让禁止的操作保持禁止。**
Operant 将工具权限、风险策略、审批与执行隔离分开处理，避免把安全完全寄托在模型是否遵守提示词上。

已实现的策略层区分允许、待审批和拒绝，并为模型辅助审批提供受控的 Reviewer 接口。
接入评审器后，它只能处理策略允许评审的请求，不能覆盖硬性拒绝；评审异常或超时也不会自动放行。
Docker 执行器进一步限制命令运行的环境与资源。

设计目标是让用户按任务选择审批严格程度，在减少无谓打断的同时保留人工把关与追踪能力。
自动化的范围由明确权限决定，不以模型的一次判断代替全部安全边界。

### 记忆系统与经验复用：让有效经验成为可积累的资产

**一次任务的成果，除了代码，还应包括下次能用上的经验。** Operant 将当前会话的信息、
历史任务经历和项目知识分开管理，让后续任务能够检索并复用适合自己的内容。

当前记忆通过插件和数据集管理，带有来源、版本、作用域与状态。候选知识支持确认与停用，
更新不会覆盖历史版本。这样既能积累知识，也能追查一条经验从哪里来、
适用于哪个项目，减少未经验证的模型结论进入长期记忆。

当前已经支持按任务检索与召回记忆、检查实际引用、批量审阅候选、处理冲突与来源撤销。
可选的后台整理会生成待确认候选；有来源并经审阅的过程经验可发布为 Skill，再显式共享到其他项目，
也可以撤销授权。后台整理默认关闭，经验正文不会自动授予工具权限。

这些机制用于减少重复摸索。已有小样本真实对照，结果及失败样本见 [B2-7 评测说明](docs/design/b2-7/evaluation.md)；
它们不足以证明所有任务都能节省 Token 或提高成功率。

### 模块化与插件扩展：能力可以增加，核心边界保持清楚

**模型、工具和界面可以变化，任务状态与权限应由同一套核心管理。** Operant 将模型接入、
Agent 执行、工具调用、记忆和客户端拆分为职责清晰的模块，为不同开发场景保留扩展空间。

当前通过 OpenAI-compatible 接口接入模型，通过 MCP 扩展工具，并提供受信目录下的 Skill 发现。
命令行、网页和桌面壳共用本机服务，扩展工具的操作仍需经过统一权限检查。

当前已提供两个记忆插件与独立进程宿主，支持安装、启停、卸载，以及保留或删除专属数据。
插件需要显式安装和启用，并配置对应 Host。当前还支持协作任务中的记忆使用、治理与经验共享。
未经认证插件的隔离实现依赖 macOS `sandbox-exec`；认证的进程内模式是信任边界，不能当作沙箱。

### 当前主线与设计目标

上述设计正在 Beta 阶段逐步落地。以下边界对应当前 `main` 分支：

| 方向 | 当前主线 | 继续完善的部分 |
| --- | --- | --- |
| 多 Agent 协作 | 会话内子 Agent、定向消息与历史树；角色编排、受限 Agent 节点执行和任务/工件板 | 自由拖拽完整执行、任意第三方节点和更完整的调度 |
| 模型辅助审批 | 分层策略、人工审批、受控 Reviewer 接口和 Docker 执行边界 | 默认启动未配置裁判模型；可调严格度的一体化自动审批体验仍需完善 |
| 经验复用 | 检索召回、治理审阅、后台候选整理、经验 Skill、显式共享和撤销 | 更大样本效果评测；不承诺统一的 Token 节省或成功率提升 |
| 插件扩展 | 两个记忆插件及生命周期、隔离宿主、MCP 工具和受控 Skill 发现 | 跨平台沙箱、更多插件类型和生产环境验证 |

更详细的实现与安全边界见 [当前实现说明](docs/PROJECT_ARCHITECTURE.md) 和 [安全说明](SECURITY.md)。

## 快速开始

下面的命令适用于 macOS / Linux 终端，请在仓库根目录执行。

### 1. 安装与初始化

准备 Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。项目要求 Python 3.10+，
仓库用 `.python-version` 固定开发版本，uv 可以按需安装对应 Python。
首次安装需要网络。

```bash
git clone https://github.com/PinelliaChill/Operant.git
cd Operant
uv sync --frozen --extra dev
cp .env.example .env
uv run operant init
```

这套命令安装的是当前 `main`。如果需要复现首个源码 Release，在 `cd Operant` 后先执行
`git checkout v0.1.0-beta.1`，再按该版本的发布说明安装。

默认运行数据写入当前目录的 `.operant/`；当前主线数据库为 SQLite Schema v19。
第一次体验请使用新目录。已有用户升级前应停止旧 Core 并备份数据库及运行目录，
不要让旧版 Core 接管升级后的库；安装新源码不代表已替你迁移旧数据。

### 2. 连接模型

用文本编辑器打开 `.env`，填写以下两项：

| 配置项 | 填什么 |
| --- | --- |
| `OPERANT_BASE_URL` | 模型服务的 OpenAI-compatible API 地址 |
| `OPERANT_API_KEY` | 你自己的 API Key |

不要把密钥提交到 Git。仓库已忽略 `.env`；模型配置只保存密钥所在的环境变量名。
模型服务需要支持模型列表查询、Chat Completions 流式输出和工具调用。

先查询服务实际提供的模型：

```bash
uv run operant model discover
```

将下面的 `MODEL_ID` 替换为查询结果中的精确名称，再创建一份模型配置：

```bash
uv run operant model add --name "我的模型" --model-id "MODEL_ID"
```

命令会打印一个配置 ID，下面称为 `PROFILE_ID`。它与服务商的 `MODEL_ID` 是两个不同的值。
这组示例使用默认的 `reasoning_effort` 参数，需要模型服务支持它。服务商若使用其他参数名，
可通过 `--effort-parameter` 指定；创建配置前请核对服务商说明，更多选项见 `uv run operant model add --help`。

### 3. 运行第一个只读任务

将 `PROFILE_ID` 替换为上一步打印的值：

```bash
uv run operant role add \
  --name "代码导读" \
  --model-profile-id "PROFILE_ID" \
  --system-prompt "阅读项目代码，用中文解释结构和关键逻辑。不要修改文件。"
```

它会打印角色 ID。替换下面的 `ROLE_ID`，创建一次会话：

```bash
uv run operant session create --role-id "ROLE_ID"
```

再将返回值填入 `SESSION_ID`，开始阅读仓库自带的小示例：

```bash
uv run operant session run \
  --session-id "SESSION_ID" \
  --workspace "$PWD/examples/buggy_calculator" \
  --message "请阅读这个小项目，解释它的用途，并指出可能的计算错误。"
```

这个角色没有写文件或执行命令的权限，适合检查模型是否连通。终端会持续显示运行事件和模型输出。

### 4. 打开界面

```bash
uv run operant serve --host 127.0.0.1 --port 8000
```

保持该终端运行，访问 [本地工作台](http://127.0.0.1:8000/web)。它随 Python 服务提供，
可以配置模型与角色、运行任务、处理审批和查看记录，不需要单独安装前端依赖。

如果想使用 React 界面，另外准备 Node.js 22.6+ 和 npm，在第二个终端的仓库根目录执行：

```bash
npm ci --prefix clients/gui
npm run dev --prefix clients/gui -- --host 127.0.0.1
```

打开 [React 界面](http://127.0.0.1:3000)，将界面的连接模式切换为 **实时连接 / Live**。
首次打开默认是演示模式，里面的示例数据不代表真实任务。实时模式通过前端开发服务连接本机 8000 端口；
暂未接通的页面会提示不可用。

### 5. 使用会话工作台

在实时 GUI 或 TUI 中选择已注册工作区和角色，创建会话后可查看父子历史、子任务结果和定向消息。
角色需要显式开启协作工具；已有会话保留创建时的权限快照，修改角色后应新建会话。

文件和同工作区会话可以作为引用附加，先传递来源和有界快照，由模型按需读取。
输入 `/` 可查看正式命令，包括 `/init`、`/review`、`/compact-context`、`/clear-context`。
上下文压缩或清理从下一轮生效并保留历史；活动运行或未核对的中断任务不能改基线。
工作台取消会传递到后代任务，未知写入结果仍需人工核对。

## 让多个角色一起改代码

先为规划、编码、审查角色分别创建模型配置，也可以先让三个角色共用一份配置。
把下面的三个占位 ID 换成真实配置 ID：

```bash
uv run operant role seed-defaults \
  --planner-model-profile-id "PLANNER_PROFILE_ID" \
  --coder-model-profile-id "CODER_PROFILE_ID" \
  --reviewer-model-profile-id "REVIEWER_PROFILE_ID"
```

默认编码角色执行命令时使用 Docker。请先启动 Docker，并准备适合目标项目的镜像；
默认镜像为 `python:3.13-slim`，需要时先运行 `docker pull python:3.13-slim`。
Docker 不可用时任务会明确失败。首次修改建议使用专门的测试项目，并先提交或备份原文件。

```bash
uv run operant workflow run \
  --task "修复计算错误，运行测试，并说明改了哪里" \
  --workspace "/absolute/path/to/your/test-project" \
  --max-rework-rounds 1
```

这里的工作目录必须换成你自己的绝对路径。审查角色可以要求有限次数的返工；
遇到审批时，由你决定是否允许操作。如果中断前的写入结果不确定，需要先人工核对。

```bash
uv run operant workflow list
uv run operant workflow show WORKFLOW_ID
uv run operant workflow trace WORKFLOW_ID
uv run operant workflow resume WORKFLOW_ID
uv run operant workflow cancel WORKFLOW_ID
```

## 其他入口

| 入口 | 说明 |
| --- | --- |
| 命令行 | `uv run operant --help` 查看所有命令 |
| 基础网页工作台 | 跟随本机服务启动，地址为 `/web` |
| React GUI / PWA | 位于 `clients/gui`，区分演示模式和实时连接 |
| [终端界面](clients/tui/README.md) | 可选的 Textual 客户端 |
| [桌面客户端](clients/desktop/README.md) | Tauri 桌面壳；需匹配版本的 Core，并用 `--desktop` 启动；无正式签名、公证或自动更新 |
| 开发接口 | FastAPI 接口与事件流；生成的客户端位于 `sdk/` |

## 当前限制与数据边界

- 当前为 Beta 版。B2-7 完成的是约定范围内的候选验收；Host 性能限制、真实 Docker/生产远程连接器及跨环境验证仍需单独处理。
- `memory add/confirm/deactivate` 等旧写入口已下线，使用当前 GUI 管理页或 `operant memory manage` 的正式命令；未经审阅的候选不会直接成为已验证经验。
- 默认只监听本机。私网监听需要 TLS 和 OAuth 配置；目前没有完成公网部署验收，不要直接把服务开放到公网。
- 任务记录保存在本地，但你发送给在线模型的提示词、代码和工具结果会传给对应模型服务。
- `.operant/` 内可能有对话、代码片段和工具输出。分享问题时请先脱敏，不要上传整个运行目录。
- Host 执行模式使用当前系统用户的权限，只适合可信项目。Docker 的范围与限制见 [安全说明](SECURITY.md)。
- 恢复依赖已经保存的任务状态，不保证从任意一句模型输出继续；写入结果未知时不会自动重试。
- 自动测试、演示数据和历史测试记录不等于所有环境都已验证，也不保证模型每次都能正确完成任务。

## 参与开发

欢迎提交问题、文档改进和代码修复。请先看 [贡献说明](CONTRIBUTING.md)，
提交问题时附上复现步骤、版本和已脱敏的错误信息。

```text
src/operant/       Python 服务、Agent 执行、工具与数据存储
clients/gui/      React 网页界面
clients/desktop/  Tauri 桌面壳
clients/tui/      Textual 终端界面
sdk/              生成的 TypeScript / Python 客户端
tests/            自动测试
examples/         小型示例项目
docs/             架构与设计说明
```

进一步阅读：[当前实现](docs/PROJECT_ARCHITECTURE.md) · [目标架构](docs/项目架构.md) ·
[客户端设计](docs/UI_UX_DESIGN_SPECIFICATION.md) · [安全说明](SECURITY.md)。
目标设计包含尚未完成的内容；具体能力以当前分支的源码和实现说明为准。

## 许可证

项目采用 [Apache License 2.0](LICENSE)。第三方依赖保留各自的许可证。
