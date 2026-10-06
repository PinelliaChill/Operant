# Operant

**让合适的模型分工协作，让验证过的经验留给下一次任务。**

当前处于 **Beta 阶段**，采用**非商业使用许可，商用须另行协商授权**。
| 版本入口 | 包含什么 |
| --- | --- |
| [`v0.1.0-beta.2`](docs/releases/v0.1.0-beta.2.md) | 本次 Beta：源码、Core/TUI 安装包与未公证 macOS arm64 App；会话协作、可视化编排、可调自动审批、经验复用、本机扩展与私有跨设备能力 |
| 当前 `main` | 持续开发分支；日常使用建议选择固定标签 |
| [`v0.1.0-beta.1`](docs/releases/v0.1.0-beta.1.md) | 2026-09-14 的早期源码快照，范围截至 B2-3 |
| [旧桌面候选](https://github.com/PinelliaChill/Operant/releases/tag/desktop-candidate-2026-09-28-08e65ea) | 9 月 28 日的 macOS arm64 包，不包含本次全部更新；桌面仍无 Developer ID 签名、公证或自动更新 |

[本次发行说明](docs/releases/v0.1.0-beta.2.md) · [更新记录](CHANGELOG.md) · [安装、升级与回退](docs/guide/candidate-installation.md)



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

GUI 提供可视化画布，可编辑节点、连线、配置角色与模型，并查看发布前后的差异。
14 类节点覆盖 Agent、工具、脚本、条件、循环、人工输入、审批、等待、子工作流、工件与合并等场景。
你也可以先用对话生成或修改工作流草稿，确认后再发布运行。运行状态、实际上下文和记忆来源都能查看。
本地文件或 Git 变化、定时器可以触发已经发布的工作流。

### 分层安全与模型辅助审批：把注意力留给需要判断的操作

**让规则明确的操作自动执行，让需要判断的操作进入审批，让禁止的操作保持禁止。**
Operant 将工具权限、风险策略、审批与执行隔离分开处理，避免把安全完全寄托在模型是否遵守提示词上。

策略层区分允许、待审批和拒绝。对于符合条件的待审批操作，可以指定独立的裁判模型，
选择谨慎、平衡或宽松三档严格度，并添加限制规则。超出自动审批范围的操作交回人工，
裁判模型不能覆盖硬性拒绝；模型异常或超时不会自动放行，审批结果保留记录。

默认使用人工审批，自动模式需要你显式配置。工具权限、审批与执行环境共同决定自动化范围；
Host 使用本机用户权限，Docker 用于受控的容器执行，不能把所有操作都视为已经处在沙箱内。

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

[本地扩展包](docs/design/extension-runtime-governance/external-tool-package.md)提供工具、命令、事件、Provider、Runtime 和能力驱动六类接口，按类别分别授权。GUI 可检查、安装、启停及卸载；GUI/TUI 会话可发现并显式调用扩展命令及已授权 Skill，参数、审批和结果使用正式 Core 协议。

本机电脑操控只面向用户明确选中的 App 与窗口。普通文本框和文本区域（如 TextEdit 正文）
支持观察后输入；安全输入框、密码框和无法确认焦点的控件禁止输入或按键。
窗口截图只截取匹配的窗口；剪贴板读写需单独授权，原文和截图不写入运行数据库。

### 当前主线与设计目标

本次 Beta 已完成约定的六轮完善和对应验收。以下列出常用能力与适用边界：

| 方向 | 当前主线 | 继续完善的部分 |
| --- | --- | --- |
| 多 Agent 协作 | 会话内子 Agent、定向消息与历史树；14 类可视化编排节点、审批等待、固定子图、隔离 Git 写入合并；持久建议对话与本地文件/Git 触发 | 任意第三方节点、容器 Writer 与跨设备调度 |
| 模型辅助审批 | 独立裁判模型、三档严格度、人工回退和审计；硬性拒绝始终有效 | 默认人工模式；需自行配置模型，不保证固定审批速度 |
| 经验复用 | 检索召回、治理审阅、后台候选整理、经验 Skill、显式共享和撤销 | 更大样本效果评测；不承诺统一的 Token 节省或成功率提升 |
| 插件扩展 | 记忆插件、MCP、六类本地扩展、显式 Skill 命令；GUI 本机浏览器/App 控制、截图与剪贴板 | 本机操控已验收 macOS 指定窗口；Linux 第三方扩展隔离、任意网站/App 不在验收范围 |
| 日常工作台 | 文件与 Diff、终端、Goal/Plan/清单、旁路提问、上下文压缩和临时资源管理 | 写入结果未知时需人工核对；清理遵守归属、引用和保留规则 |
| 私有跨设备 | 设备配对、权限范围、加密任务与结果、断线追平、远端受控执行 | 已验收 macOS 与 Ubuntu 私有链路；不代表公网服务或远程桌面 |

更详细的实现与安全边界见 [当前实现说明](docs/PROJECT_ARCHITECTURE.md) 和 [安全说明](SECURITY.md)。

## 快速开始

下面的命令适用于 macOS / Linux 终端，请在仓库根目录执行。

### 1. 安装与初始化

准备 Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。项目要求 Python 3.10+，
仓库用 `.python-version` 固定开发版本，uv 可以按需安装对应 Python。
首次安装需要网络。

```bash
git clone --branch v0.1.0-beta.2 --depth 1 https://github.com/PinelliaChill/Operant.git
cd Operant
uv sync --frozen --extra dev
cp .env.example .env
uv run operant init
```

这套命令安装固定的 `v0.1.0-beta.2`。参与开发时可克隆 `main`；旧版本请按对应标签的说明安装。

默认运行数据写入当前目录的 `.operant/`；本轮源码的数据库为 SQLite Schema v23；旧发布版按各自源码核对版本。
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

私有双设备入口：本机 GUI「设置→远程」或 TUI `Alt+6` 管理配对、Scope、Session、Host Ack、Target Lease 与结果；另一台设备使用 `operant remote-device`，远端受控任务使用 `operant remote-target`。部署和操作步骤见[跨设备使用说明](docs/design/beta-remote/usage.md)。正式双设备验收状态以[任务 5 验收记录](docs/design/beta-remote/acceptance.md)为准；旧标签和已安装 App 按各自版本核对能力。

六项默认能力包随 Core 分发，需在项目中显式安装与启用。DOCX/PPTX/PDF 所需依赖用 `uv sync --extra artifacts` 安装。干净安装、升级、备份回退和跨机器安装见[候选安装说明](docs/guide/candidate-installation.md)。

## 当前限制与数据边界

- 当前为 Beta 版。本轮已验收 macOS arm64 的 Core/TUI/原生桌面与 Ubuntu 24.04 的 Core/TUI、私有跨设备链路；未验收 Docker 场景和 Linux 第三方扩展隔离器不计为通过。历史性能限制、失败样本和跳过项保留，见[验收记录](docs/design/beta-daily/acceptance.md)。
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

项目采用 [PolyForm Noncommercial 1.0.0](LICENSE)。许可证允许的非商业用途可直接使用；
**超出允许用途的商业使用，须事先协商并取得书面授权**，联系方法见 [商业授权说明](COMMERCIAL.md)。

这是源码可见许可，不属于 OSI 定义的开源许可证。已经按 Apache-2.0 公开的历史代码保留原授权，
本次变更不追溯限制这些副本的商业使用；第三方依赖保留各自许可证。
