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
  该用例在获准 loopback 环境补跑 1 passed；其余未完成区段（从当前收集列表第 576 项起）正补跑。
  不将中断命令描述为单次完整 PASS；最终补跑结果随后追加。
- 原始评测报告、分组和冻结阈值已保存；模型调用 0，usage/cost unknown，不算真实模型验收。

## 缺口与下一步

- B 已实名交付 `ce2ab66c3d42a8aca8a344626fd6afcfaa667ff0`，GUI 路由/组件/模式增量已集成。
  未纳入 B 的 SDK 候选及重复 SDK 测试；采用 Codex 统一修复和独立 SDK 测试，避免为 strip-only 测试改变公共 ErrorCode 形式。
  除原交接方法外，旧 HttpClient 的假审批/Thread/Workflow/SSE cursor 与无事件回读的运行入口也显式失败；正式生成 Client 未改。
- B 原交接只有 npm/cargo check，没有真实 Tauri 窗口走查。已在 COM-20260909-001 要求 B 补齐；真实桌面仍未验收。
- 两线代码已完成并集成，将新建独立 gpt-5.6-luna/max Reviewer；工具没有 Fast 开关，须如实说明。
- 本批只有契约与基线，不提供 PluginHost 执行、数据库升级或新 FTS；运行 Core 与桌面产物不因新接口文件而更新。
- 不推送、合并或部署。后续进入 MP-1 必须由本批门禁和用户下一步安排决定。
