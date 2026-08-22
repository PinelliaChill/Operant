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
- 完成 Planner → Coder → Reviewer 编排，并支持 Reviewer 明确要求时的有限返工；
- 支持总超时、运行中取消和高风险工具审批后继续执行。
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

# 运行真实三角色工作流
uv run operant workflow run \
  --task "修复目标项目中的缺陷并运行测试" \
  --workspace /absolute/path/to/workspace \
  --max-rework-rounds 1

# 启动 API 和 SSE 接口
uv run uvicorn operant.api:app --reload
```

第一周真实验收已使用 Kimi K2.6、GLM 5.2 和 Gemini 3.1 Pro Preview 完成
calculator fixture 的规划、文件修改、测试和只读审查。

Reviewer 最终必须给出 `VERDICT: APPROVED` 或 `VERDICT: REWORK`。默认最多返工一次，
可设为 `0` 关闭，最多为 `3`；缺少明确结论时始终产生审计事件，且不会自动修改 workspace。

新初始化的默认 Coder 需要可用的 Docker CLI 和预先准备好的项目镜像；Docker 不可用时，命令会明确失败，
不会静默改在宿主机执行。自定义角色的 Host Runner 只适用于可信 workspace。完整的安全前提、
威胁模型与 Docker 集成测试条件见 [`SECURITY.md`](SECURITY.md)。

通过 `operant role add --writable` 或即时角色创建可配置 `--command-runner`、
`--docker-image`、`--cpu-limit`、`--memory-limit-mb` 和 `--pids-limit`；可写角色默认选择
Docker，只有明确填写 `--command-runner host` 才会直接运行宿主机命令。

完整的模块关系、调用链、Snapshot 规则、数据表和安全边界见
[`docs/PROJECT_ARCHITECTURE.md`](docs/PROJECT_ARCHITECTURE.md)。
