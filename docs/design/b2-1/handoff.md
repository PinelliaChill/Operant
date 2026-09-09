# B2-1-A / MP-0 交接

记录身份：Codex（本批协调方兼公共契约负责人）；适用对象：Antigravity、集成 Codex、指定 Reviewer。
治理、文件归属和初始 HEAD 见 [任务包](task-package.md)。仅本批工作；未进入 MP-1。

## 交付入口

- [真实源码基线](source-baseline.md)：SQLite v14、实际五个 App Protocol、Memory/Context、Plugin/MCP/Skill/Scheduler、Project/Task/Agent/Host。
- [契约边界](contract-boundaries.md)：单一 Pydantic 源、分别版本化的 App/Memory SDK JSON Schema 与离线 Client 接口；所有权/关闭/keep-delete/CAS/来源与恢复语义。
- 迁移映射 `src/operant/contracts/migrations/b2_1_mapping.json`：旧表→新契约、未知归属/证据不足、清理/回退；不执行用户库迁移。
- [评测基线](evaluation-baseline.md)：合成开发/保留集、旧直接策略与成本、冻结性能门；真实模型和未来 Host 实测另列。
- 旧 `HttpClient` 的 Thread/消息/审批/Context/Graph/Workflow 图/Remote 合成投影和空事件订阅改为显式 SCHEMA_INCOMPATIBLE；真实生成 Live Client 保留。

## 当前验收

- A 契约/评测定向：20 passed；Ruff format/check、mypy（92 source files）、offline lock check、diff check 通过。
- 生成确定性、旧 OpenAPI 不变：契约测试通过；当前 Schema/digest 由生成器产生。
- SDK 合成隔离：`node --test sdk/typescript-client/http-client.test.mjs` 2 passed，覆盖异步和同步拒绝及网络零调用。
- 集成 GUI：`npm run typecheck` 通过，`npm test` 82 passed，`npm run build` 通过。
- 完整 `uv run pytest` 两次被会话中断；最近一次已覆盖前约 598 项，唯一失败为沙箱禁止 bind loopback。
  该用例在获准 loopback 环境补跑 1 passed；从当前收集列表第 576 项起补跑 155 passed（66.27s），
  新增契约/评测最终定向 20 passed。当前共收集 730 项；中断前记录、重叠补跑和定向结果覆盖全部用例，
  Docker integration 的 1 项环境 skip 保留，不将这些结果描述为单次完整 pytest PASS。
  [汇总证据](verification.json)：聚合 729 passed / 1 skipped / 0 未覆盖；记录原始日志 hash。
- 原始评测报告、分组和冻结阈值已保存；模型调用 0，usage/cost unknown，不算真实模型验收。

## 缺口与下一步

- B 已实名交付 `ce2ab66c3d42a8aca8a344626fd6afcfaa667ff0`，GUI 路由/组件/模式增量已集成。
  未纳入 B 的 SDK 候选及重复 SDK 测试；采用 Codex 统一修复和独立 SDK 测试，避免为 strip-only 测试改变公共 ErrorCode 形式。
  除原交接方法外，旧 HttpClient 的假审批/Thread/Workflow/SSE cursor 与无事件回读的运行入口也显式失败；正式生成 Client 未改。
- B 原交接只有 npm/cargo check，没有真实 Tauri 窗口走查。已在 COM-20260909-001 要求 B 补齐；真实桌面仍未验收。
- 两线代码已完成并集成，将新建独立 gpt-5.6-luna/max Reviewer；工具没有 Fast 开关，须如实说明。
- 本批只有契约与基线，不提供 PluginHost 执行、数据库升级或新 FTS；运行 Core 与桌面产物不因新接口文件而更新。
- 不推送、合并或部署。后续进入 MP-1 必须由本批门禁和用户下一步安排决定。

## Reviewer 返工（待同一 Reviewer 增量复核）

首轮报告：`/private/tmp/operant-b2-1-review.md`。已修复两个 P1：正式旧别名通过门禁；
生命周期 completed 与未完成清理互斥。PrivateIndex 的操作/载荷/hash/所属安装边界也已校验。
评测改为 fixture_load、bootstrap、first_query 分开测量；service_query_calls 不再冒充 SQL 次数，RSS 不可策略间比较。

新增五项契约用例，受影响契约/评测合计 25 passed，GUI 82 passed/build，Ruff/mypy 通过。
[实际浏览器组合验证](browser-route-evidence.md) 覆盖两条别名与仍应阻断的画布；不代替 Tauri。
本次不重复未改变的 Core 全套检查，原分段记录保持原义。
