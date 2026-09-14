# Operant

**让多个 AI 分工写代码，过程可查看，关键操作由你把关。**

Operant 是一个在本机运行的 AI 编程工具。你可以给不同角色配置不同模型，让它们分别负责
理解项目、制定计划、修改代码和检查结果。任务记录保存在本地，也可以通过命令行、网页或桌面界面使用。

当前版本：**Beta**。欢迎从源码安装、体验和反馈问题。桌面构建目前仍是未签名的候选包。

## 可以用它做什么

- **读懂项目**：让只读助手查找文件、解释代码，帮助你定位问题。
- **分工完成修改**：让规划、探索、编码和审查角色接力完成任务；不同角色可以使用不同模型。
- **控制执行范围**：指定工作目录、工具权限、时间和用量预算，处理需要确认的操作。
- **查看过程与结果**：查看模型输出、工具调用、审批、文件产物和任务历史；中断后按已保存的状态恢复。
- **保留项目知识**：保存和检索有版本记录的记忆，供后续任务使用。
- **扩展工作方式**：通过工作流图、Agent 消息、定时任务、MCP 工具和远程连接组织更复杂的任务。

典型的编程流程是：

```text
你的任务 → 制定计划 → 只读探索 → 修改代码 → 检查结果 → 汇总
                                           ↓
                                     必要时有限返工
```

Operant 面向单个用户：本机服务负责运行与权限，本地 SQLite 数据库保存任务状态。
它需要你自己的模型 API 配置；模型调用费用由对应服务商计算。

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

默认运行数据写入当前目录的 `.operant/`。第一次体验请使用新目录，不要指向已有的重要数据库。

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
| [桌面客户端](clients/desktop/README.md) | Tauri 桌面壳；仍需本机 Core，目前没有签名、公证或自动更新 |
| 开发接口 | FastAPI 接口与事件流；生成的客户端位于 `sdk/` |

## 当前限制与数据边界

- 当前为 Beta 版。远程连接、容器与多 Agent 等复杂组合，需要按自己的环境验证。
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
