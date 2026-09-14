## B2-3/MP-2 初审结论

审查范围限定在 `dcf73ae + 未提交集成`，基线 `d257abe`。确认 2 项 P1、3 项 P2；未宣称最终验收。已知的 Skill 生命周期、Artifact UI、`project_detach`、package/GUI 收尾项未重复计入。

### P1-01：全局关闭可能报告完成，但实际未关闭插件

位置：

- `src/operant/memory_plugins/manager.py:404-420`
- `src/operant/plugins/host.py:803-846`

全局 `memory_switch(enabled=false)` 先持久化全局关闭，再逐个调用 `host.stop()`，但忽略返回的 `LifecycleReceipt`。存在 active Run 时，Host 会返回 `blocked/ack=failed`，不会真正关闭引擎；Manager 仍可继续返回 `completed`。

复现条件：

1. 安装并启用插件；
2. 创建一个 active Run；
3. 执行全局 memory switch 关闭；
4. 结果显示完成，但 installation 仍为 enabled，active Run 和引擎仍存在。

影响：关闭屏障失真，完成状态与实际运行状态不一致，可能留下未停止的 memory-side runtime。

建议：收集并校验所有 stop receipt；任一 `blocked/restart_required/failed` 时返回持久化的 blocked/manual-reconcile 状态，不得返回 completed。全局状态最好在屏障成功后提交，或使用可恢复的关闭事务状态。

### P1-02：Ledger 幂等摘要遗漏 CAS 与 Proposal 参数

位置：

- `src/operant/memory_plugins/ledger.py:697-706`
- `src/operant/memory_plugins/ledger.py:1007-1016`

`save_version()` 在幂等缓存命中前，摘要主要基于 version 内容；`propose()` 传入 version 时也未把以下参数纳入请求指纹：

- `expected_head_revision`
- `permission_epoch`
- `operation`
- `source_refs`
- `reason`
- `extractor_version`

已用 `MemoryLedger(":memory:")` 做只读内存复现：

- 相同 version、相同 idempotency key，第一次 `expected_head_revision=0`，第二次改为 `99`，第二次仍返回第一次的成功结果；
- 相同 version、相同 idempotency key，第一次 reason=`first`，第二次 reason=`second`，第二次仍返回第一条 Proposal，未产生 idempotency conflict。

影响：调用方可能误以为新 CAS 已成功，审计字段或来源引用也可能被旧 Proposal 静默复用，直接削弱 Proposal/CAS 约束。

建议：由 Ledger 对完整规范化请求自行生成摘要；摘要至少覆盖 dataset、version/proposal、CAS revision、permission epoch、operation、source refs 和 reason。相同 key 但摘要不同必须抛出 `IdempotencyConflictError`。

### P2-01：管理命令重放丢失完整结果

位置：

- `src/operant/memory_plugins/manager.py:299-306`
- `src/operant/memory_plugins/manager.py:311-323`

命令首次成功时，`b23_commands.result` 只保存 `status` 和 `message`。重放时再构造 `ManagementResult`，因此 `export_data`、`records` 等字段会丢失。

复现：同一 `Idempotency-Key` 执行 `dataset_export`，首次结果包含导出内容；再次重放得到成功状态，但 `export_data` 为空。

建议：保存完整的规范化 `ManagementResult`，或持久化一个不可变的结果/导出引用；重放必须返回与首次一致的业务结果。

### P2-02：旧 API 查询在 Manager 尚未初始化时静默返回空

位置：

- `src/operant/application/service.py:2797-2801`
- `src/operant/api_b2_3.py:21-37`

开启 `memory_plugin_mode` 后，`query_memories()` 在 `memory_manager is None` 时直接返回 `[]`。但 Manager 是按需通过 factory 初始化的。

因此，进程重启后直接调用旧的 `/v1/memories/search` 可能返回空结果；先访问一次 `/v1/b2-3/management` 初始化 Manager 后，同样查询又可能返回真实数据。

建议：旧读路径应惰性初始化 Manager；若无法初始化，应返回明确的 503/typed error，不能把“尚未初始化”伪装成“没有记忆”。

### P2-03：Legacy migration 可被旧 payload 的 scope 覆盖

位置：

- `src/operant/memory_plugins/manager.py:429-447`
- `src/operant/memory_plugins/ledger.py:2057-2078`

Manager 传入了当前项目的显式 scope，但 `_legacy_scope()` 优先读取 legacy payload 内的 `scope`/`scope_json`。因此旧数据可以被导入成另一个 workspace/run/personal scope。

目前确认的是 scope 写入完整性问题，尚未复现跨项目直接读取；实际表现更可能是幽灵数据、当前项目不可见，或未来匹配其他上下文后被错误发现。

建议：项目迁移强制使用 Manager 传入的注册 WorkspaceScope；payload scope 不一致时拒绝或进入明确的人工映射流程，不应直接信任旧库字段。

## 其他核查结果

- v15 migration manifest/checksum 与冻结值一致。
- B2-3 schema canonical digest 与记录值一致。
- 未将普通 `ledger.delete` 当作物理清理完成；物理删除仍按显式计划和 Host 屏障理解。
- `tests/test_api.py` 中旧 memory 写入仍期待成功，而当前 plugin mode 会拒绝旧写入。根据当前计划，这更像测试/契约同步项，不单独升级为代码阻断。
- 目标 pytest 未能收集：受只读沙箱限制，没有可用临时目录，报 `FileNotFoundError: No usable temporary directory found`。没有把该环境错误算作测试失败。
- 未读取 `.env`、用户库或凭据；未执行真实 J1、完整门禁或桌面证据，因此本报告不是最终验收。

