# B2-1 契约与迁移边界 v1

记录身份：Codex；适用对象：Antigravity、Codex 执行者与 Reviewer。仅覆盖 B2-1 / MP-0。

源码事实见 [基线](source-baseline.md)，治理来源/基线 HEAD/归属见 [任务包](task-package.md)。
统一计划和记忆设计仍是范围来源；本文解释本批代码，不另建目标计划。

## 入口和版本

- 单一契约源：`src/operant/contracts/b2_1.py`。Pydantic 禁止未知字段，Scope 有明确 discriminator。
- 生成：`uv run python -m sdk.protocol.generate_b2_1`。输出 JSON Schema、digest、Python/TypeScript 离线 Client 接口。
- App 契约：`operant-b2-contract.v1`；插件 SDK：`operant-memory-sdk.v1`，分别产生文件和 digest。
- 两者均未加入 `/v1/protocol*` 能力协商，没有 HTTP 路由或可调用 Host。生成 Client 是异步接口声明，
  不包含返回假成功的实现。现有五份 OpenAPI 与 SQLite v14 保持原样；下一阶段实现须通过正式 Core 协商启用。
- JSON Schema 负责类型/必填字段；Pydantic 额外检查跨字段关系。Core 后续必须在真实事务里校验当前
  owner/grant/epoch/CAS/lease/source，不能把 Schema 校验通过当作授权成功。
- [合成实例](../../../tests/fixtures/b2_1_contract/contracts.json) 覆盖项目、Task、Agent、绑定、Memory、
  保留数据、清理阻断、Host 准入及错误；`tests/test_b2_1_contracts.py` 验证负例与生成确定性。

## 冻结的身份与行为

| 对象 | 本批冻结语义 |
| --- | --- |
| Project / Workspace | Project 是显式登记身份；worktree 映射带 revision。路径或 Git remote 相同不授予共享权。旧 `ProjectProjection.workspace_ref` 实际可含本地路径，新 `WorkspaceIdentity.workspace_ref` 为 `workspace:<sha256>`，由 Core 规范化路径后映射；迁移不直接重解释旧字段。 |
| Task | 稳定键为 `(source_type, source_id)`；来源限 Session、WorkflowRun、TeamTask，状态和动作来自各自服务端投影。`/v1/tasks` 仍返回旧 WorkflowRun；`TaskAssignment` 是消息，不能冒充持久 Task Board。 |
| Agent | RolePreset/Definition 是配置，Snapshot 是冻结事实，AgentInstance 是执行实例。旧 Snapshot 嵌入 Session/Agent，无独立 ID；未来转换须持久保存来源+快照摘要映射。ModelProfile ID 与 Provider model ID 分别记录。 |
| 设置 | 全局→项目→Agent/Run 的来源、生效时间和 revision 可表达。MemoryEnabled 必须声明 global_enabled=true；全局关闭提升 epoch，下层不能覆盖。每个有效绑定仅一个主安装/数据集。 |
| Dataset | 用户为最终主体，业务 owner 固定 plugin_dataset，namespace 必须与 dataset_id 一致；installation_id 可为空，保留数据仍可 inspect/export/delete/rebind。重新绑定必须授权，不按插件名字授予旧数据。 |
| Memory | 版本不可变，唯一 Head 与独立 Proposal；发布携带精确 Proposal revision、Head revision、request digest 和幂等键。模型 confidence 不授予发布权。持久记录的正常追加与授权清除分开。 |
| Scope / Source | workspace/session/run/personal 明确区分；personal 必须 opt-in grant。来源是带版本/hash/scope/epoch 的引用；Mailbox 可见不等于持久保存或分享许可。未知 token/owner 明确拒绝。 |
| Memory Pack | 自动与显式引用共用总预算，Manifest/包/配置/索引代际冻结。ContextMemoryUse 保存实际版本与派生压缩。当前权限/撤销高于冻结点，旧摘要不能继续污染下一请求。跨 dataset 首期需显式授权导入。 |
| Host | PluginHost 安装、Remote HostInstance、Remote Execution Target 是不同对象；HostAdmission 只引用 Core/Host 身份。Relay Ack、Host 接受与执行/清理完成不可互换。 |
| 认证 | Host 信任的 issuer 校验签名、包/依赖/权限/lifecycle 证据摘要和当前有效/撤销状态。Manifest 自报不构成认证。可信进程内也须有认证记录，不宣称沙箱。 |

Task action 的 availability/reason/revision 是服务端裁决的消费面，客户端不凭本地计数决定允许取消、归档或发布。
未来 Graph 发布/归档继续复用现有 Compiler/Revision/Run 守卫；本批不引入第二状态机。

## Memory Engine 与 Host API

- Engine：`extract(SourceBatch)`、`recall(RecallRequest)`、`maintain(MaintenanceInput)`、
  `on_index_event(IndexEvent)`；只返回 Proposal、候选版本引用或 IndexReceipt。
- 通用生命周期：negotiate/health/cancel/checkpoint/restore；RPC context 固定 SDK 版本、安装/数据集、
  scope、deadline、cancel token、request digest、幂等键、binding/permission epoch 与 lease fencing。
- Host API：有权来源读取、有限搜索、指定 ModelProfile 代理、登记资源的私有索引访问。
  不传 SQLite Connection、ApplicationService、任意文件/SQL 或整套环境变量。配置仅引用 secret_ref。
- PrivateIndex 只接受当前安装/数据集的 index 资源；read/delete 禁止 payload/digest，replace 必须携带二者且 SHA-256 与 UTF-8 payload 一致。所有操作显式带 expected_revision，真实 CAS 留给 Host 事务执行。
- deadline/cancel 后不接收迟到提交；幂等键与请求摘要绑定，重复同请求回读原结果，异请求明确冲突。
  checkpoint 不能裁决 Workflow 恢复或未知副作用；未知结果先 Query/人工核对，不自动重放。
- 未认证插件必须有实际文件/网络/资源隔离证据。隔离不可用返回 isolation_unavailable，不能降级进程内。
  已加载进程内插件停止失败进入 restart_required，停止准入并处理现有 Run 后安全重启；不得假称已卸载。

## 关闭与清理

关闭顺序：原子拒绝准入并提升 epoch → 取消在途 RPC、队列、Scheduler 来源/订阅 → 等待停止证据。
失败显示 disabling/failed/restart_required；只有真正停止才返回 disabled。普通聊天/工具/历史/恢复不依赖插件。

UninstallRequest 必须一次携带 keep/delete、inventory revision、binding epoch、幂等键及明确 stop_run_ids。
Resource 同时表示 Core 托管行、专属目录、共享依赖、外部资源，以及消费者/锁/可重建性。
清理对每项返回 pending/deleted/retained/blocked/external_unconfirmed，保留 cursor 和 inventory revision，
重启幂等续做。enabled/disabled/uninstalled 必须配 completed，且 completed 不允许 pending/blocked/external_unconfirmed 清理项；这些关系同时写入 JSON Schema 与 Pydantic 校验。目录 identity 改变必须阻断，不能按名字扫描 Home。无法表达的资源不允许进入清理计划。

- keep：保留数据集及不可重建状态，解除安装绑定，数据管理仍有入口；非数据安装资源按盘点清理。
- delete：删除专属数据库行和目录；共享消费者、保留锁、活动 Run 明确阻断，不称完整删除。
- Core 只保留无正文清理/安装审计与墓碑。原聊天/恢复证据不是任意复制普通插件正文的借口。
- 外部导出、已提交模型请求不能收回；WAL/空闲页/备份不属于普通卸载的安全擦除保证。

## 迁移与后续验收

机器可读映射：`src/operant/contracts/migrations/b2_1_mapping.json`，source SQLite=14，target=null。
本批没有 SQL 升级脚本，不预占迁移号。映射核对真实表字段；B2-3 才在隔离旧库副本演练。
旧 active 证据不足保留 legacy_unverified，不自动判定合格；旧 candidate 不能覆盖正式发布头。
旧 API 后续必须走同一治理 Repository，表达不了的请求 upgrade_required/conflict，不留旧写旁路。

新库必须沿用已有迁移 checksum/版本守卫拒绝旧二进制写入；回退不能恢复旧关键词自动激活或失效授权。
用户库迁移前必须有具体影响数量/所有权/阻断项及恢复方案。本批没有读取用户库。

本批确定性检查只证明契约和旧策略基线。PluginHost 两模式真实隔离/停止、CAS 事务、生命周期执行、
新 FTS、真实模型单/多 Agent、Tauri 完整产品联合验收分别留在对应批次，不能用这些 fixture 宣告通过。
