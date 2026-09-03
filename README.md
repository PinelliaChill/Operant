# Operant

Operant 是一个由角色预设驱动的多模型 Coding Agent Runtime。角色可以配置提示词、模型、
reasoning effort、工具权限、运行预算和 Memory 策略。创建会话时，系统会保存一份不可变的
`RoleSnapshot`；之后修改角色，不会改写旧会话的执行配置。

## 当前能力

Operant 已打通一条能运行、能测试、能追溯，并具备基础自我纠错与安全执行边界的主链路：

- 使用 SQLite 保存 Model Profile、Role Preset 及其版本；
- 为 Session 和 Agent 保存不可变的角色快照；
- 通过 OpenAI-compatible 协议接入流式模型；
- 运行 Tool Calling Agent Loop，并把 Tool Result 写回模型上下文；
- 通过 Tool Policy 控制 workspace 工具和审批事件；
- CLI 与 FastAPI/SSE 共用同一个 Application Service；
- 完成 Planner → 一个或多个只读 Explorer → Coder → Reviewer → Main 汇总编排；
- 提供 Graph IR、Compiler、Definition Revision、Graph Run、NodeRun/Attempt、有限 Loop、边界与持久恢复；
- 既有 Coding Workflow 已投影到 Graph Runtime，保留兼容 CLI/API 和单 Writer Coder 边界；
- 提供本地 Team、Roster、定向 Mailbox、消息投影、Task/Artifact Board 和幂等 Ack；
- 冻结 `phase1e.v1`，新增 `phase23.v1` 生成 TypeScript/Python Client，并让 React GUI live 接入 Graph、
  运行监控和本地 Team；
- 支持最多 4 个只读 Explorer 的有限并行、自定义角色替换和结构化失败汇总；
- Reviewer 明确要求时可触发有限返工，必需角色失败时会安全停止；
- 通过 SQLite 保存 Workflow 状态和事件，并从已提交的阶段检查点恢复中断任务；
- 当 Coder 写入结果未知时转为人工核对，只有显式允许后才会重放写阶段；
- 提供 Working、Episodic、Project 三类版本化 Memory、FTS5 检索、作用域判权、项目结构候选和
  保守激活；
- 汇总 Session/Workflow 的 token、耗时、工具失败与 verdict，并可导出脱敏 JSONL Trace；
- 提供 Evaluation Runner v1：用不可变 Suite/Case/Variant 对单 Session 或完整 Workflow 做顺序、
  隔离、可复现的对比运行，记录实际模型、Prompt、Role、Memory、Workflow 和环境快照；
- 聚合成功率、测试通过率、首次成功率、返工/修复、Token、已知费用、延迟、工具失败和审批指标，
  并按模型、Prompt/协议、工具/上下文、环境和编排五类生成 Trace 根因分析；
- 提供不依赖 CDN 的本地 Web 工作台，用于配置、运行、审批、观察、恢复和取消任务；
- 支持总超时、运行中取消和高风险工具审批后继续执行；
- 将失败测试压缩为结构化反馈，连续相同失败会安全停止；
- 新初始化的默认 Coder 在过滤后的 Docker workspace 快照中运行命令，限制网络、CPU、内存和进程数。

模型凭据只通过环境变量名引用。API Key 不会写入 Model Profile、Role Snapshot、Event
或 SQLite。

## 本地开发

项目兼容 Python 3.10 或更高版本，本地开发环境通过 `.python-version` 固定为 Python
3.13.3。依赖与虚拟环境由 `uv` 管理。

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy src
npm run test --prefix clients/gui
npm run typecheck --prefix clients/gui
npm run build --prefix clients/gui
```

真实模型集成测试需要一个 OpenAI-compatible API 中转服务。将 `.env.example` 复制为
`.env`，再填写本地配置。Operant 会安全解析 `.env`，不会执行其中的 shell 内容；进程中已有
环境变量优先。不要提交 `.env`。

## 常用命令

```bash
# 初始化 SQLite
uv run operant init

# 查询中转站实际提供的模型 ID；密钥从环境变量读取
uv run operant model discover

# 创建三个 Model Profile 后初始化默认角色
uv run operant role seed-defaults \
  --planner-model-profile-id <planner-profile-id> \
  --coder-model-profile-id <coder-profile-id> \
  --reviewer-model-profile-id <reviewer-profile-id>

# 运行角色驱动的多 Agent 工作流（默认包含一个只读 Explorer）
uv run operant workflow run \
  --task "修复目标项目中的缺陷并运行测试" \
  --workspace /absolute/path/to/workspace \
  --max-rework-rounds 1

# 可重复指定最多 4 个自定义只读 Explorer，并限制并行数
uv run operant workflow run \
  --task "修复目标项目中的缺陷并运行测试" \
  --workspace /absolute/path/to/workspace \
  --explorer-role-id <architecture-explorer-role-id> \
  --explorer-role-id <test-explorer-role-id> \
  --max-parallel-explorers 2 \
  --main-role-id <main-summary-role-id>

# 启动 API 和 SSE 接口
uv run uvicorn operant.api:app --reload

# 查看任务、事件与聚合 Trace
uv run operant workflow list
uv run operant workflow show <workflow-run-id>
uv run operant workflow events <workflow-run-id>
uv run operant workflow trace <workflow-run-id>

# 从安全阶段边界恢复，或取消任务
uv run operant workflow resume <workflow-run-id>
uv run operant workflow cancel <workflow-run-id>

# 通过指定 Session 的不可变 Role Snapshot 管理可读写 Memory
uv run operant memory add \
  --session-id <session-id> \
  --kind project \
  --content "已验证的项目知识" \
  --project-scope /absolute/path/to/workspace
uv run operant memory search "项目知识" \
  --session-id <session-id> \
  --project-scope /absolute/path/to/workspace

# 从受信任的本地 JSON 创建并顺序运行 Evaluation Suite
uv run operant evaluation suite add --file /absolute/path/to/suite.json
uv run operant evaluation run <suite-id> \
  --artifact-root /absolute/path/to/evaluation-artifacts
uv run operant evaluation run show <evaluation-run-id>
uv run operant evaluation result list <evaluation-run-id>
```

Evaluation Suite 可表达 Exp 19—24 所需的单模型/多模型、模型、Role Prompt、effort 和 Memory
开关对照。Runner 会为每个 Case × Variant × repetition 创建独立 artifact workspace，并在模型运行后
执行 Suite 预先声明的安全验证命令。当前尚未用真实模型跑完 Exp 19—24；价格也不会联网猜测，只有
Suite 固定了精确模型价格且 Provider 返回对应 usage 时才计算费用，否则相关指标保持 `null`。
每个已调度组合会先持久化为唯一 Pending Result，再以同一 ID 写入终态；取消、进程中断或重启恢复
会标记为 Interrupted，保留 artifact 引用但不伪造实际快照、成功指标或验证结果。

第一周真实验收已使用 Kimi K2.6、GLM 5.2 和 Gemini 3.1 Pro Preview 完成 calculator fixture
的规划、文件修改、测试和只读审查。2026-08-22，第三周流程又在隔离的临时 calculator workspace
中使用 Kimi K2.6 与 Gemini 3.7 Flash 完成六角色真实验收：Planner、两个并行只读 Explorer、
独占写入的 Coder、只读 Reviewer 和 Main 均使用独立 Session；Reviewer 给出 `APPROVED`，模型外
再次运行 3 项 unittest 和 `git diff --check` 均通过。此前两次尝试遇到上游 Provider 瞬时错误，
不计为成功验收。

Reviewer 最终必须给出 `VERDICT: APPROVED` 或 `VERDICT: REWORK`。默认最多返工一次，
可设为 `0` 关闭，最多为 `3`；缺少明确结论时始终产生审计事件，且不会自动修改 workspace。
Planner、Explorer 和 Reviewer 槽位必须使用只读角色；并行只用于 Explorer，Coder 不会并行写入。
默认 `role_main` 会在 Reviewer 批准后读取全部结构化子任务结果，生成最终用户汇总；Main 同样只读。

新初始化的默认 Coder 需要可用的 Docker CLI 和预先准备好的项目镜像；Docker 不可用时，命令会明确失败，
不会静默改在宿主机执行。自定义角色的 Host Runner 只适用于可信 workspace。完整的安全前提、
威胁模型与 Docker 集成测试条件见 [`SECURITY.md`](SECURITY.md)。

通过 `operant role add --writable` 或即时角色创建可配置 `--command-runner`、
`--docker-image`、`--cpu-limit`、`--memory-limit-mb` 和 `--pids-limit`；可写角色默认选择
Docker，只有明确填写 `--command-runner host` 才会直接运行宿主机命令。

启动 Uvicorn 后访问 `/web` 可打开本地工作台。工作台和 API 当前没有身份认证，只应绑定受信任的
本机地址；不要直接暴露到公网。SQLite 任务事件可能包含模型输出、工具结果和本地任务内容，也应按
敏感运行数据保护。详细边界见 [`SECURITY.md`](SECURITY.md)。

## 文档入口

- [当前项目架构与实现说明](docs/PROJECT_ARCHITECTURE.md)：当前模块、调用链、数据模型、接口、
  安全边界和完成状态的唯一权威说明；
- [多 Agent Harness 目标架构](docs/项目架构.md)：中长期 Kernel、Graph、Team、Context、Action 和
  Plugin、Remote Control、自托管 Relay 与 Remote Target 架构，不代表当前已经实现；
- [目标客户端设计与实现规范](docs/UI_UX_DESIGN_SPECIFICATION.md)：GUI、TUI、桌面壳、状态管理、
  响应式 PWA、远程操控、无障碍和实施顺序，不代表当前已经实现；
- [安全边界](SECURITY.md)：当前安全前提、执行隔离、恢复、Memory 和本地部署限制。

当前 `/web` 仍是无前端框架、无 CDN 的基础本地工作台。React GUI 已在 live 模式接入 Graph 定义与
启动、运行监控和本地 Team；通用后台 Scheduler、Skill/MCP、Textual TUI、Tauri、Host Connector、自托管
Relay 和 Remote PWA 仍只属于目标设计或 Mock，不能描述为已完成。当前 `/web` 和 `/v1/*` 没有设备
配对或 Remote Gateway，不得直接暴露到公网。

Operant 2.0 的目标是单用户、单个本地 Core 和本地 SQLite 权威。用户可以通过直连或自托管 Relay 从
手机/浏览器远程启动、引导、审批和审查本地任务，也可以连接受控远程执行 Target；这不等于建设 SaaS、
多个人类用户协作或分布式 Core。
