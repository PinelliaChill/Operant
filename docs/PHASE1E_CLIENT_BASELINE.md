# Phase 1E 客户端真实接入基线

> 状态：冻结勘误（`phase1e.v1`，保持 8 个 operation）
>
> 冻结日期：2026-09-02
>
> 记录身份：Codex
>
> 适用对象：所有 Agent
>
> 代码基线：`origin/main@27fb387`（PR #9 merge commit）

本文只冻结 Phase 1E 的交付边界、公共协议语义和三条执行线的文件所有权。当前实现状态仍以
`docs/PROJECT_ARCHITECTURE.md`、源码和测试为准；目标 Kernel 与目标客户端分别以
`docs/项目架构.md`、`docs/UI_UX_DESIGN_SPECIFICATION.md` 为准。

## 1. 本阶段目标

Phase 1E 建立客户端真实接入的最小闭环：

```text
Core 连接
→ Workspace/Project 只读投影
→ Thread
→ Session Command/Run
→ 已提交 SSE Cursor 回放与重连
→ Approval / 类型化错误 / 持久状态校正
```

本阶段不建设第二套运行、审批、恢复或权限状态机。服务端 SQLite Projection 是 Thread、Run、
Approval、Receipt、Cursor 和恢复状态的权威；客户端只保存选择、布局、筛选和未提交输入等 UI 状态。

## 2. 明确排除

- Graph、Definition/Revision、实例生命周期和模板发布封锁；
- Team、Mailbox、群聊寻址和 Agent DM；
- Skill、MCP、工作区 Skill 软链接与动态工具注入；
- Scheduler、Cron、Timer 和 Trigger；
- OAuth、多 Provider 批量导入与凭据持久化；
- Remote Control、Relay、Remote Target；
- TUI、Tauri 和 PTY；
- 会话 Task 新实体、知识库映射、全局四模式审批策略；
- 任意模型流字节位置恢复、Approval Future 跨进程恢复。

上述能力可以在协议草稿或 Mock 中保留，但不得出现在 `phase1e.v1` 生成 Client 的正式可调用面，
不得由 GUI live 模式伪造成功。

## 3. 单一协议 Schema

1. `sdk/protocol/schema/operant-phase1e.openapi.json` 是 `phase1e.v1` HTTP/SSE 公共协议的唯一源。
2. TypeScript 与 Python 类型、Client 和模型从该 Schema 生成；手写代码只允许是传输适配器、
   SSE 解析器和生成器本身，不得复制维护公共请求/响应模型。
3. 生成命令必须固定、可离线复现，并有 clean-tree 校验：重新生成后 `git diff --exit-code` 为空。
4. Schema 只描述当前真实 Core 与本阶段新增投影；Graph、Remote、Team 等 Mock-only 类型不进入
   `phase1e.v1` 正式 Client。

### 3.1 版本协商

- 新增只读 `GET /v1/protocol`。
- 响应至少包含：`protocol_version="phase1e.v1"`、`schema_digest`、`min_client_version`、
  `capabilities`。
- Client 首次连接先查询该端点；未知 major/阶段版本必须明确失败，不能静默降级为 Mock。
- `schema_digest` 是规范化公共 Schema 的 SHA-256；生成 Client 内嵌相同 digest 并在连接时核对。

### 3.2 Command、Receipt 与错误

- 每个修改型 Client 方法生成并发送 `Idempotency-Key`；同一逻辑动作重试必须复用原 key。
- 首次响应读取 `Idempotency-Key`，重放读取 `Idempotency-Replayed`；SSE Command 已接受后的
  同 key 重试仍按既有 M0 语义返回 `202 application/json` typed Receipt，不重新执行或伪造 SSE。
- 公开错误保留兼容 `detail`，并统一解析 `error.code/message/retryable/recovery`；Client 不从
  HTTP 文案猜测恢复动作。
- 未知副作用、`manual_reconcile_required` 和 `outcome_unknown` 只显示并查询持久事实，不自动重放。

### 3.3 Cursor 与 SSE

- Cursor 是 `0..2^63-1` 的 SQLite 已提交整数，只能在同一资源、同一事件流 scope 内复用；gap 合法。
- SSE `id` 等于公开 JSON 中的 Cursor；`event` 是类型，`data` 是 JSON payload。
- 重连发送同 scope 的 `Last-Event-ID`，只回放 `cursor > after_cursor` 的已提交事件。
- 网络断线后先回放，再通过 Query Projection 校正；断线不等于后台一定继续，也不触发新 Command。
- Event Reducer 以 `resource_scope + stream_kind + cursor` 去重，不以客户端时间戳或随机 ID 裁决顺序。

### 3.4 冻结勘误：Session 与 Thread 的原子绑定

- `CreateSessionRequest.thread_id` 是可选的公开字段，以保留 CLI 和旧 API 创建未绑定 Session 的兼容性。
- 提供 `thread_id` 时，Core 只接受已存在且 `active` 的 Thread；Thread 已有其他 Session 的
  `session` legacy ref 时必须返回类型化冲突错误。
- Core 在一个 SQLite 事务中写入 Session 与 `thread_legacy_refs`；校验或写入失败不得留下孤儿
  Session。相同 Idempotency-Key 的 replay 返回原结果，不重复创建 Session 或 legacy ref。
- GUI live 创建必须传当前明确选中的 Thread ID；没有选中 Thread、Thread 非 active 或已绑定 Session
  时禁用并说明原因，不创建无归属 Session，也不静默回退到 Mock。
- 本勘误只扩展既有 `createSession` 请求字段，不新增 operation、不新增 Migration，也不改变本阶段
  8 个 operation 的集合。

## 4. 最小 Workspace/Project 投影

COM-20260901-001 的“Project=Workspace”方向本阶段调整为既有 Workspace registration 的只读投影，
不新增可变 Project CRUD、颜色、默认项目或删除语义，也不猜测旧数据归属。

- `GET /v1/projects` 返回已注册 Workspace 的最小 Project Projection。
- `project_id` 复用服务端已有 Workspace initialization 的稳定 ID；`workspace_ref`、readable/writable、
  创建时间来自持久事实。
- 每项聚合精确 `workspace_ref` 下的 Thread 与现有 Workflow Run 摘要/ID；无绑定事实时返回空集合，
  不自动映射 legacy Session。
- Projection 可提供安全显示名和计数，但不创建新的业务实体或恢复权威。

### 4.1 安全只读文件浏览

- `GET /v1/workspaces/{workspace_id}/files` 只列出已注册且 readable 的 Workspace 内目录项。
- 输入是相对路径；拒绝绝对路径、`..`、空字节、目录逃逸、大小写别名和任意软链接组件。
- 复用 workspace 文件工具的敏感路径规则，至少拒绝 `.git`、`.operant`、`.env*`、凭据文件、
  私钥/证书和常见 Secret 文件名。
- 返回相对路径、名称、类型和有界 metadata；不返回正文、绝对宿主路径、inode、storage key 或 Secret。
- 排序和分页必须确定且有上限；目录变化导致分页前提失效时明确要求刷新，不伪造 Event Cursor。
- 本阶段不提供客户端任意文件内容下载。`@文件` 只选择引用，正文仍由 Core 在 Context/权限边界内解析。

## 5. GUI live 模式

- Mock 演示模式继续保留，并在全局状态、设置和关键空态中明确标为“演示数据”。
- live 模式必须由用户显式选择；Core 健康检查、协议协商、请求或 SSE 失败时显示真实错误，禁止静默
  切回 Mock、混合 Mock 列表或生成伪造 Receipt/Cursor。
- live 路径只消费生成的 TypeScript Client；GUI 不手写重复 HTTP 响应类型。
- 最小路径覆盖：连接 → Project/Workspace → Thread → 选择/创建 Session → 绑定 Thread 运行 →
  SSE 回放/重连 → Approval 决定或错误/人工核对状态。
- Thread、Run、Approval 和恢复终态始终由 Query/SSE Projection 校正；客户端 Store 不得把本地按钮
  点击、Relay/网络送达或 SSE 结束当成服务端已完成。

## 6. COM-20260901-001/002 裁决

| 需求 | Phase 1E 结论 |
|---|---|
| Project/Workspace 与树形聚合 | 调整后接受：只读 Workspace Project Projection，不做 CRUD/默认项目 |
| 客户端工作区文件浏览 | 接受：仅安全目录 metadata，不提供任意正文下载 |
| 模板→实例、发布封锁 | 延期到 Graph Runtime/实例契约阶段 |
| 群聊寻址、DM | 延期到 Team/Mailbox 阶段 |
| 多 Provider 批量导入 | 延期，当前继续使用单端点 discover |
| OAuth 2.0 PKCE | 拒绝纳入本阶段，需独立安全设计 |
| 会话 Task/知识库映射 | 延期，不能把 Workflow/Evaluation/Memory 强行等同 |
| 全局四模式审批策略 | 延期；Phase 1E 只接现有 Tool Policy 与 Approval 事实 |
| Workspace Skill 软链接 | 延期到 Skill Discovery 之后；本阶段不创建或扫描软链接 |

## 7. 三条执行线与唯一所有者

| 执行线 | 可修改范围 | 唯一所有权/禁止事项 |
|---|---|---|
| 协议与 SDK | `sdk/**`、生成器、协议契约测试 | 公共 Schema 唯一负责人；不得修改 Migration 或 GUI |
| 后端客户端投影 | `src/operant/**`、后端/契约测试 | Migration 唯一负责人；不得修改公共 Schema 或 GUI；本阶段优先零 Migration |
| GUI 真实接入 | `clients/gui/**`、GUI 测试 | 只消费生成 SDK；不得修改公共 Schema、Migration 或复制 Core 状态机 |

所有执行线从本冻结提交创建独立 worktree。公共文件冲突由 Codex 在集成分支处理；任何执行线不得
为了让本线先通过而改写另一条线的所有权文件。

## 8. 草稿导入清单

- 来源：共享目录中的未跟踪 `clients/`、`sdk/`，未修改来源目录。
- 纳入：77 个 `.ts/.tsx/.css/.json/.html` 源码与配置文件，985,375 bytes。
- 排除：`clients/gui/node_modules/`、`clients/gui/dist/`、`.vite/`、coverage/build、`.DS_Store`。
- 来源清单聚合 SHA-256：`3d5e7f087e34b963db87429f055a4675f6019d1f95e3754cc14f8dcf415e944e`。
- `package-lock.json` SHA-256：`36664f8e86426a81eb5d4455bf39f1a2a1c21783eaf881d81262d65021982d0a`。
- 文件名与内容形态扫描未发现 `.env`、私钥/证书、凭据文件或可疑 Secret 值；真实凭据仍不得提交。

## 9. 统一验收

后端与协议：

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv lock --check
git diff --check
```

客户端与生成物：

```bash
npm ci --prefix clients/gui
npm run typecheck --prefix clients/gui
npm run build --prefix clients/gui
# 生成命令由协议线固化；重新生成后必须是 clean diff
```

还必须运行协议契约测试和真实本地 Core 闭环测试。Mock、fixture、静态构建或被跳过的 Docker 测试
不能单独称为真实本地 Core 闭环。
