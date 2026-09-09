# B2-1 / MP-0 独立代码复核

- 记录身份：Codex；实际配置：`gpt-5.6-luna`，reasoning `max`；工具没有 Fast 开关，Fast 未启用且不可配置。
- 复核对象：`/private/tmp/operant-b2-1-a`，集成 HEAD `03cd4b040be56dc6625ceae76adf7d2c4236aab1`，基线 `ecb00437e9a44a5e79d54e8cf4944fd0d456bf02`。
- 范围：B2-1-A/MP-0、B2-1-B/GUI-L0；不把 MP-1 PluginHost、真实 Tauri 窗口或真实模型缺口当成本批代码缺陷。

## 需要修改

### P1：Live 全局路由门禁截断仍有正式重定向目标的旧入口

- 位置：`clients/gui/src/live/liveRouteSupport.ts:30-46`、`clients/gui/src/app/RailLayout.tsx:577-590`；目标入口为 `clients/gui/src/app/routes.tsx:110-121`。
- 触发：在 Live 模式打开 `/remote` 或 `/session`。解析器分别返回 `{isSupported:false, unavailableSection:'remote'}` 和 `session`，RailLayout 先渲染 `LiveUnavailableView`，因此 React Router 中 `/remote -> /settings?tab=remote -> /settings?cat=system` 与 `/session -> /chat` 的 Navigate 永远不会执行。
- 影响：`/remote` 对应的 Live Remote 页面实际存在（`clients/gui/src/features/settings/SettingsView.tsx:2640-2654`），但旧 Remote 深链被错误显示为“本阶段未接入”；旧 Session 深链也不能回到已支持的 Live Chat。GUI-L0 的旧入口/深链连续性不完整。
- 最小修复：让安全别名先通过门禁（至少将 `remote`、`session` 纳入 resolver 的支持/重定向集合，或在 RailLayout 中优先执行这些静态 Navigate）；为两条入口增加集成路由测试，验证 Live 下不会落入 Demo。

### P1：生命周期回执允许把未完成清理报告为 `uninstalled`/`completed`

- 位置：`src/operant/contracts/b2_1.py:432-449`，尤其 `LifecycleReceipt.state`、`cleanup`、`ack` 缺少跨字段约束。
- 触发：以下对象当前可被 Pydantic 接受：`state="uninstalled"`、`ack="completed"`，同时 `cleanup` 中有 `outcome="blocked"`（`active_run`）或 `external_unconfirmed`；同样可接受 `state="disabled"`、`ack="host_accepted"`。
- 影响：消费方可能把仍有活动 Run、共享/外部残留或仅 Host 接受的操作当作已卸载/已完成，破坏 keep/delete、停止屏障和清理进度语义；这不是 MP-1 执行缺失，而是 MP-0 回执契约本身允许矛盾状态。
- 最小修复：在 `LifecycleReceipt` 添加模型校验：`uninstalled`/`ack=completed` 只能在没有 `pending|blocked|external_unconfirmed` 清理项时成立；有未完成项必须返回 `blocked`/`uninstalling`/`failed`/`unknown` 对应状态；`disabled` 不得配 `host_accepted`。补负例 fixture/test，并重新生成离线产物。

## 建议在返工时补齐的契约边界

### P2：PrivateIndex 操作的 payload/CAS 形状未冻结

- 位置：`src/operant/contracts/b2_1.py:625-632`。
- 触发：`operation="replace"` 可以没有 `payload`/`content_digest`；`operation="delete"` 可以携带任意 payload；`read` 也可以携带写入字段。虽然请求有 `expected_revision`，接口没有把读/替换/删除的必需字段关系表达出来。
- 影响：不同 Host 实现可能对同一请求作不同解释，尤其在 CAS 失败重试和私有索引清理时扩大歧义。Core 运行时当然仍需授权校验，但 MP-0 应先拒绝明显不可表达的操作。
- 最小修复：增加按 operation 的模型校验（read 禁止写载荷；replace 要求 payload 与 digest；delete 禁止 payload，或明确其 tombstone 语义），补 fixture 负例。

## 评测证据限制

- `scripts/benchmark_memory_baseline.py:430-548` 在已创建 Service/临时 SQLite 后才进入 `_measure`；`timing.cold_ms` 只是第一个查询样本，不是进程/Service/SQLite 冷启动。计划要求的“冷启动”门没有被实际测量，冻结门应改名为 first-sample，或另测初始化/进程启动并把两者分开。
- `resource.getrusage(...).ru_maxrss` 是进程单调 high-water（`benchmark_memory_baseline.py:377-383,545`），三个策略在同一进程顺序运行，后一个策略的 RSS 包含前面策略的峰值，不能作策略间内存比较。当前报告未将其标为不可比；如保留该字段，应改为每策略独立进程或明确仅作整次运行观测。
- `cost.sqlite_query_calls`（`benchmark_memory_baseline.py:549-554`）实际写入 `len(samples)`，即逻辑 `ApplicationService.query_memories` 调用数；每次 `SQLiteStore._connect` 还会执行两个 PRAGMA，再执行搜索 SQL。因此该字段不是实际 SQLite SQL 次数，应改名为 `retrieval_calls`/`service_query_calls`，或用 SQLite trace callback 真实计数。
- 这两项不改变当前“合成旧策略基线、无模型调用”的结论，但会限制后续 Host 性能门使用该结果作为冷启动/RSS 对照。

## 已核对通过

- 单一 Pydantic 源、App/Memory SDK 两份 JSON Schema/digest 与 Python/TypeScript 离线接口重生成后确定性一致；现有 `*.openapi.*` 未改。
- `tests/test_b2_1_contracts.py tests/test_b2_1_benchmark.py`：20 passed。
- `node --test sdk/typescript-client/http-client.test.mjs`：2 passed；旧 HttpClient 的 Thread/Context/审批/Graph/Workflow/Remote/SSE 假投影路径均显式 `SCHEMA_INCOMPATIBLE`，未观察到剩余假数据。
- `npm --prefix clients/gui test -- --test-name-pattern='resolveLiveRouteSupport'`：现有套件 82 passed；纯 resolver 测试未覆盖 `/remote`、`/session` 的真实 RailLayout/Router 组合，故上述 P1 未被现有测试捕获。
- `git diff --check` 通过；工作树 clean。

## 未满足/未验证验收门

- `verification.json` 已诚实标记完整 pytest 未由单次命令完成：聚合覆盖 729 passed/1 skipped，`single_full_command_completed=false`；不能称单次完整 pytest PASS。
- `tauri_runtime` 为 `not_verified`：当前只有 npm/cargo 检查，无真实 Tauri 窗口、旧深链、刷新和模式切换证据。真实桌面联合验收与代码复核分开报告。
- 本批没有 PluginHost 两种运行方式、CAS 事务执行、真实 keep/delete、数据库迁移、真实模型或用户库迁移验收。

## 结论

代码审查结论：**需修改后再通过**。至少修复 P1 生命周期回执矛盾，并修复/明确 `/remote` 与 `/session` 的 Live 旧入口路由；修复后复用本 Reviewer 做增量复核。其余 MP-1/真实 Tauri/真实模型属于明确的后续或未验证门，不作为本批代码缺陷替代。

## 同一 Reviewer 增量复核（03cd4b0..1e0cfc7）

- 实际配置仍为 `gpt-5.6-luna` / reasoning `max`；工具没有 Fast 开关，Fast 未启用且不可配置。
- 最新 worktree HEAD `1e0cfc749125de98c85fe15001f2bf72811361f8`，工作树 clean。本次只复核返工差异与原报告发现，没有重跑全套门禁或扩大范围。

### 原发现关闭

- 路由 P1 已关闭：`resolveLiveRouteSupport` 只放行精确的单段 `/remote`、`/session`（含尾斜杠归一化），`/remote/unknown`、`/session/unknown` 仍拒绝；新增 resolver 用例覆盖别名和嵌套拒绝。`browser-route-evidence.md` 记录真实 Vite + React HashRouter：`#/session -> #/chat`、`#/remote -> #/settings?cat=system`（重新打开仍成立），`.live-unavailable-state=false`；画布深链仍为 true。这是浏览器组合证据，不能替代真实 Tauri。
- 生命周期 P1 已关闭：`LifecycleReceipt` 的 Pydantic 校验和生成 JSON Schema `allOf` 同时要求 terminal success 使用 `completed`，且 completed 的 cleanup 只能为 `deleted|retained`；新增负例覆盖 pending/blocked/external_unconfirmed 及 host_accepted。当前契约不再接受原报告中的矛盾回执。
- PrivateIndex P2 已关闭：新增 `PrivateIndexResource` 限定 index 与 managed_directory/core_rows，校验 resource 所属 dataset/installation、read/delete 的空 payload、replace 的 UTF-8 SHA-256、expected_revision 和生成 Schema；新增负例覆盖错误 owner/storage/载荷/digest。
- 评测指标问题已关闭：`cold_ms` 改为 `first_query_ms`，新增 fixture/bootstrap 计时，`service_query_calls` 与 `sqlite_sql_calls=unknown` 分开，raw/docs 明确 RSS 为顺序整进程 high-water、策略间不可比较；阈值同步改为 first-query/bootstrap 语义，未将 Host 门写成通过。

### 仍需修正的证据追踪项（P2，非运行代码缺陷）

- `docs/design/b2-1/evaluation-results.json` 的 `implementation.head` 仍为 `03cd4b040be56dc6625ceae76adf7d2c4236aab1`，但 raw 中已包含本次 `1e0cfc7` 才提交的 `first_query_ms`、`service_query_calls` 等脚本输出；`evaluation-baseline.md:5,43` 也将运行 HEAD 写为 03cd。也就是说，按记录的 clean HEAD 03cd 重跑会得到旧字段，冻结 raw report 没有精确指向其生成脚本版本。
- 最小修复：在 clean `1e0cfc7` 上重新生成一次 raw/report 并更新其文件 hash、文档运行 HEAD；若保留现有 timing 结果，则至少记录生成时 worktree dirty/source script digest，并明确 raw 是 03cd 上的未提交返工脚本产物。该项不改变指标口径或安全结论，但在修复前不应称 raw report 可由记录 HEAD 完全复现。

### 增量结论

代码审查：**通过（原 P1/P2 已关闭）**；保留一项评测 raw provenance 文档修正。真实 Tauri 窗口/深链/刷新/模式切换仍单列为未验证，MP-1、真实 keep/delete、真实模型和用户库迁移仍不属于本批代码验收。

## raw provenance 增量关闭（1e0cfc7）

- 已核对当前 HEAD 为 `1e0cfc749125de98c85fe15001f2bf72811361f8`；当前脚本相对该 HEAD 无差异，dirty 仅限评测/交接证据文档和 raw JSON。
- `evaluation-results.json` 的 `implementation.head` 已更新为 `1e0cfc749125de98c85fe15001f2bf72811361f8`，文件 SHA-256 为 `85e895e5b83be582da7cbb788aef2ca16b4bb1cb66fbdbad0fb9e8f459504f25`。
- `evaluation-baseline.md` 已同步该 HEAD、raw hash、bootstrap/first-query/service-query 字段与表格数值；raw 中的 timing 字段与当前脚本一致，且报告仍明确 RSS 仅为整次进程 high-water、SQL 次数为 unknown、Host 门 pending。
- 因此最后一项 raw provenance P2 **已关闭**。最终代码/证据审查结论保持：**通过**；真实 Tauri 仍未验证，单独保留为验收限制。
