# B2-1-A 源码基线（MP-0）

> 记录身份：Codex；核对日期：2026-09-09；范围：只读源码/迁移/生成协议基线。

## 1. 基线与证据

实施 worktree 为 `/private/tmp/operant-b2-1-a`，分支
`codex/b2-1-contract-baseline`，HEAD
`ecb00437e9a44a5e79d54e8cf4944fd0d456bf02`。工作树不是 clean：任务包、主 Agent 的
`src/operant/contracts/` 以及本批其他未跟踪契约/fixture 文件存在；本子任务只写本文件。
当前 HEAD 的 `docs/PROJECT_ARCHITECTURE.md`、源码、迁移 DDL、测试和生成 Schema 是实现证据，
目标设计文档只作为计划来源。

用现有环境在临时目录运行 `SQLiteStore(...).migrate()`（未接触用户库）得到：

```text
sqlite_version 14
protocol_versions phase1e.v1, phase23.v1, phase45.v1, phase56.v1, beta.v1
```

只读运行态核对（2026-09-09）发现 `127.0.0.1:8000` 有 Python listener，但其 cwd 是旧的
`/private/tmp/operant-beta-rc-productization`，不是本 B2 worktree；3000 无 listener。系统拒绝本次
`ps` 读取，未启动服务。`clients/desktop/src-tauri/tauri.conf.json` 只证明 Tauri 配置的
`beforeBuildCommand=../gui build`、`frontendDist=../../gui/dist`、`devUrl=127.0.0.1:3000` 和
Core connect-src `127.0.0.1:8000`；未构建、未做 Tauri/Core live 验收，桌面状态为 unknown。

## 2. 版本基线

`src/operant/persistence/sqlite.py:484-576` 的 `_migrations()` 注册 v1–v14；
`migrate()` (`sqlite.py:409-458`) 只接受 1–14，并校验 manifest/checksum、DDL、外键、索引、trigger、FTS5。
临时库实测 v1–v14 连续，v14 名为 `beta_remote_gateway_container_lifecycle`。当前没有 B2-1 新 migration。

| route | 实际 protocol | 实测 Schema digest |
| --- | --- | --- |
| `/v1/protocol` | `phase1e.v1` | `10bb7cf130c0dc9b183da7915faf3900dc133ed9983e1ab022460150cfabc5f3` |
| `/v1/protocol/phase23` | `phase23.v1` | `98d58402a526c95663c0c151ab69fcc5a7d0c4bbe68b99dab283b648a8be3874` |
| `/v1/protocol/phase45` | `phase45.v1` | `94a3b48ba9482184712c587937a9606373637663dd650d92fd252766e6e25af3` |
| `/v1/protocol/phase56` | `phase56.v1` | `538eb163b88e0bfbb42f99c84d31314b01965b23e95b662604b97963c7de6513` |
| `/v1/protocol/beta` | `beta.v1` | `1565354a3ce029073212292fd38dcfcc6beced58ff87360d967ff50087c79b91` |

来源是 `src/operant/application/protocol_metadata.py:10-172` 和
`sdk/protocol/schema/*.openapi.json[.sha256]`；`api.py:1788-1851` 在 digest 不可用时返回 503，
不编造协议。计划/架构文档称 additive `operant-beta.v1`，实现字符串实际为 `beta.v1`，契约应以生成物和协商结果为准。

## 3. 当前实现（源码与真实表）

### 3.1 Memory/Context

- `Memory`/scope/status/保守激活在 `src/operant/domain/memory.py:21-253`；
  `ApplicationService._authorize_memory()` 在 `service.py:3027-3064` 校验 session、project、role scope。
- v2/v6 实际表是 `memories(id,current_version,created_at)`、
  `memory_versions(memory_id,version,body,kind,content,project_scope,role_scope,source_session_id,source_task,confidence,status,created_at,body_hash)`、
  `memory_fts(memory_id,version,content,source_task,project_scope)`；DDL 在
  `sqlite.py:4232-4266`，版本追加/FTS 在 `sqlite.py:14416-14702`。
- `search_memories()` (`sqlite.py:14535-14636`) 只返回 current head，按 status/kind/project/session
  SQL 过滤后做 role filter；API CRUD 在 `api.py:3449-3531`。DELETE 是 deactivate，不是物理删除。
- 当前 Workflow 自动读取仍是 `SequentialCodingWorkflow._memory_context()`
  (`application/workflow.py:1384-1404`) 的空 query、最多 5 条 project Memory，未做任务语义 FTS，未取 episodic。
- `ContextRevision`/`PromptBlock`/`ReferenceBinding`/`Compaction` 的不可变输入、source hash/cursor、
  memory refs、watermark 和 append-only compaction 在 `domain/context.py:235-500`、
  `application/context.py:81-319`；v6 物理表字段在 `sqlite.py:1161-1279`。这证明已发送输入证据链，
  不能证明已有插件 Memory Pack/Manifest、自动召回冻结点或撤销 epoch。

### 3.2 Plugin、Skill、MCP

- 当前源码没有运行时 `PluginHost`、installation/dataset 记录、认证/卸载或 Memory Engine；
  `WorkflowDefinition.required_plugins` 反而在 `application/graph.py:296-300` 被拒绝为 out of scope。
- Skill 只有 bounded `SkillDiscovery` (`skills/discovery.py:22-215`) 和 v11
  `skill_candidates(candidate_id,root_ref,relative_directory,name,description,manifest_sha256,snapshot_json,trust_status,discovered_at)`
  (`sqlite.py:7789-7816`)；`api_phase45.py:625-657` 只有 discover/list，持久投影总是
  `untrusted_candidate` (`persistence/phase45.py:34-66`)。没有安装、注册或执行授权。
- MCP 由 `mcp/adapter.py:46-204`、`persistence/phase45.py`、`api_phase45.py:683-1026` 实现 stdio
  digest-pinned Docker snapshot 与 legacy SSE；v11/v12 有 `mcp_servers`、tool snapshots、lifecycle、
  `mcp_action_receipts`、start leases、stdio sandboxes。它们是 Server/Tool 事实，不是 Plugin installation，
  没有 owner namespace、dataset、consumer、keep/delete。

### 3.3 Scheduler、Project、Workspace/worktree

- `ScheduleDefinition`/`RunRequest`/leases/attempt 在 `domain/scheduler.py:26-199`；v11 表有
  `schedule_definitions`、`schedule_heads`、`run_requests`、authority/job leases、attempts、
  `scheduler_graph_dispatches` (`sqlite.py:7914-8001`)。`TriggerService` 只接受已发布 Graph Definition
  (`application/scheduler.py:224-243`)，`SchedulerWorker` 经 gateway 绑定 Graph Run
  (`runtime/scheduler.py:46-124`)；它不是插件作业执行器。
- `workspace_initializations(sequence,id,workspace_ref,workspace_hash,readable,writable,created_at)` 在
  v8 (`sqlite.py:6909-6920`)；`ProjectProjection` 明确是 registered Workspace 投影
  (`domain/projections.py:41-53`)，`client_projection.py:146-224` 只按 exact workspace_ref 聚合 Thread/Workflow。
  `/v1/projects`、目录 metadata 路由在 `api.py:1853-1932`。
- 当前公共 Project Projection 的 `workspace_ref` 是绝对路径并会返回给客户端（`client_projection.py:177-192`；
  `tests/test_phase1e_backend_projection.py:259` 有绝对路径断言），不能写成“当前已隐藏路径”。后续新契约的
  `workspace:<hash>` 是新增的脱敏语义；旧 `workspace_ref` 需要保留兼容并在迁移映射中明确区分。
- 当前没有独立 Project/worktree/Git remote/branch/commit/共享授权表。相同路径不能产生共享权；
  `WorkspaceTools.root` (`tools/workspace.py:35-213`) 只是一次工具实例的 resolved path。

### 3.4 Task、Agent、TaskAssignment 与 Task Board

- `ModelProfile` 的本地 `id` 与 Provider `model_id`、`RolePreset → RoleSnapshot → Session/AgentInstance`
  在 `domain/models.py:110-259`、`sqlite.py:9652-9771`；Graph `NodeAttempt.agent_instance_id` 由
  legacy bridge 绑定真实 Agent (`application/workflow.py:296-314`)。
- `POST /v1/tasks` 与 coding workflow 共用 `SequentialCodingWorkflow.run()` (`api.py:3078-3137`)；
  `GET /v1/tasks` 返回 `service.list_workflow_runs()` (`api.py:3139-3149`)。v2 表只有
  `workflow_runs(id,body,status,current_stage,created_at,updated_at)`，task/workspace/role 主要在 body。
  所以 **`/v1/tasks` 当前是 WorkflowRun 投影，不是通用 Task Board**。
- `MessageKind.TASK_ASSIGNMENT = "TaskAssignment"` (`domain/team.py:147-162`) 是带 recipients/payload/
  delivery/context trust 的消息类型；持久化转为 `team_messages.message_kind='task_assignment'`
  (`persistence/graph_team.py:756-787,1298-1304`)。消息是上下文输入，不能改 Graph/Approval。
- `TeamTask` 有 task_id/team_run_id/title/assignees/status/artifact_refs/source_message_id/revision
  (`domain/team.py:339-367`)，v9 `team_tasks` 有独立 version/body/body_hash；Board API 为
  `api_phase23.py:617-664`，必须 expected_revision/idempotency。生成 Client 也分别定义 MessageKind 与
  `TeamTask/TaskBoardProjection` (`sdk/python_client/phase23_generated.py:318-398`)。三者不能互相升级，
  Task Board 也不是 Working Memory。

## 4. 迁移映射与来源不足（供主 Agent 冻结契约）

| 计划语义 | 当前可复用来源 | 不足/安全处理 |
| --- | --- | --- |
| `installation_id/dataset_id` | 无统一表；MCP `server_id`、Skill `candidate_id`、Memory `project_scope` | candidate/server/scope 都不是安装或 dataset；未知 owner/consumer 必须失败，旧记录不能默认归属 |
| owner namespace/consumer | Memory role/session/project scope、Team recipient、MCP root ref | 没有统一 owner、共享授权、认证或 epoch；不能从路径、标题、消息推断共享权 |
| `source_ref/revision/published head` | Memory `current_version`/`memory_versions.version/body_hash`；Context source snapshots；Graph/Team definition version | Memory 没有原始证据、发布者、Proposal、CAS head、revoke epoch；缺证据的 active 迁移为 `legacy_unverified` |
| `keep/delete/retention lock` | Artifact retention tables 可作参考；Memory 只有 deactivate | Memory/Skill/MCP 没有专属目录、活动 Run 依赖、保留锁或孤儿管理；deactivate 不等于卸载 delete |
| Project/worktree | `workspace_initializations.id/workspace_ref/hash`、Thread/Workflow workspace refs | 旧绝对路径保留兼容；新契约可用 `workspace:<hash>` 脱敏引用；仍必须新增 Project/worktree/Git identity，相同规范化路径不共享 |
| Task source | `WorkflowRun.id`、Graph legacy link、`team_run_id/task_id/message_id` | 缺 `TaskSourceType+source_id` 统一投影；保留 WorkflowRun、TaskAssignment、TeamTask 各自 source type/id |
| Role/Model binding | role_versions、RoleSnapshot、AgentInstance、`NodeAttempt.agent_instance_id`；profile id 与 provider model id 并存 | 没有 effective source/time、binding epoch、主 Memory Engine；不能用 RolePreset ID 冒充 AgentInstance |
| Plugin SDK/App Protocol | 五组生成 App Schema/digest；Skill/MCP 是内部 Python/API | 没有可协商 Plugin SDK、Host API、deadline/cancel/idempotency/type error；Phase 45 不能冒充 Plugin SDK |

迁移只能在 v14 隔离副本演练，不原地改历史。无法表达的 scope/owner/清理范围应明确失败或
`legacy_unverified`，并报告影响数量；不要新增旁路 schema 或从旧 JSON/路径猜授权。

## 5. MP-0 计划（不是当前实现）

依据 `docs/design/Operant-Beta-2.0更新计划.md:57-82,186-220` 和
`docs/design/记忆系统设计草案.md:561-578`，主 Agent 后续需冻结：

- installation 与 dataset 分离；owner namespace、consumer、认证/epoch、资源清单、keep/delete、墓碑和清理进度；
- Core 保留身份/授权/Scope/immutable provenance/发布撤销/Context Composer/Action Gateway/audit/recovery，
  插件才负责 schema、提取、召回排序、维护、冲突建议、procedure/Skill draft；
- Project/Workspace/worktree/Run/Writer 显式身份，ModelProfile/Capability 的 effective source/time；
- Task projection 保留 source type/id，Session、WorkflowRun、TeamTask 保持独立；消息可见性不等于 Memory 保存；
- Plugin SDK 与 App Protocol 分开版本化，unknown scope/owner/cleanup 明确失败；新召回先 shadow，再在受控项目启用，
  活动 Run 固定 package/config/Manifest/index generation。

## 6. 本子任务检查与交接

已完成临时 v14 migration、表字段回读、五组协议 digest/route 常量核对、Phase 23 Message/Board 类型核对、
路径检查、敏感值形状扫描和 `git diff --check`（含未跟踪文件的 `diff --no-index --check`）。纯文档增量，
未运行无关测试、真实模型、服务、用户库迁移或插件执行。

交接给主 Agent：本文件只提供基线和迁移风险；公共 Schema、migration、生成 Client、契约测试和集成仍由主 Agent
单一负责。当前 HEAD 与工作树新增契约必须分开报告，未跟踪设计/生成文件不证明运行时能力。
