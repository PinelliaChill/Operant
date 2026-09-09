# B2-1-A / MP-0 最终交接

记录身份：Codex（本批协调方与公共契约负责人）；适用对象：Antigravity、集成 Codex、Reviewer。

**A 线已交付，B GUI 代码已集成；独立代码/证据审查通过。B2-1 整批仍待真实 Tauri 补证，不进入 MP-1。**
代码验收 HEAD：`1e0cfc749125de98c85fe15001f2bf72811361f8`，worktree `/private/tmp/operant-b2-1-a`；
分支 `codex/b2-1-contract-baseline`，基线 `ecb0043`。本文件及最终报告的后续提交仅同步证据。
治理根 `/Users/bigo/agentworkspace/codexworkspace/operant`，治理版本与归属见 [任务包](task-package.md)。

## 改动与入口

| 内容 | 可读证据 |
| --- | --- |
| 真实 SQLite v14、五个 App Protocol、Memory/Context、Plugin/MCP/Skill/Scheduler、Project/Task/Agent/Host 基线 | [源码核对](source-baseline.md) |
| 项目/Workspace、Task 来源、Agent 配置与实例、数据集/Host/Memory/生命周期契约 | [契约边界](contract-boundaries.md)，单一源 `src/operant/contracts/b2_1.py` |
| 分别版本化的 App/Memory SDK Schema、digest、离线 Python/TypeScript Client 接口 | `sdk/protocol/generate_b2_1.py` 与 `sdk/protocol/schema/operant-b2-contract.json` / `operant-memory-sdk.json` |
| 旧表映射、legacy_unverified、明确所有权、keep/delete、清理/回退 | `src/operant/contracts/migrations/b2_1_mapping.json`；只有映射，无 SQL 升级执行 |
| 42 案例、独立项目保留集、无记忆/旧直接/旧最近条目三对照、冻结性能门 | [评测报告](evaluation-baseline.md) / [原始结果](evaluation-results.json) |
| GUI-L0 路由/Demo Hook/模式隔离、旧 HttpClient 假投影与假 SSE 阻断 | B 来源 `ce2ab66`；[实际浏览器组合验证](browser-route-evidence.md) |

`/v1/tasks` 仍是 WorkflowRun，TaskAssignment 是消息，TeamTask/Task Board 是独立持久对象。
旧协议保持不变；新契约未注册运行 API，不是 PluginHost 或新召回实现。
B GUI 增量来自 Antigravity `ce2ab66`，Codex 在集成阶段修复 `/remote`、`/session` 的别名门禁；
SDK 由 Codex 单一持有，未纳入 B 的重复 SDK 测试及仅为 strip-only 加载所做的公共协议重写。

## 验证与独立审查

- 后端已补齐单次完整门禁：`uv run --offline pytest -ra` **734 passed / 1 skipped**，exit 0，382.62 秒。
  跳过项明确要求 Docker 与 OPERANT_DOCKER_TEST_IMAGE，不算容器验收；此前分段证据作为历史保留。
- Reviewer 返工新增 5 项契约用例，受影响契约/评测合计 **25 passed**；Ruff format/check、mypy、offline lock、diff 检查通过。
- 集成 GUI：**82 passed**，类型检查与生产构建通过；SDK：**2 组通过**。
- 真实浏览器验证 HashRouter 两条正式别名及画布阻断；临时浏览器/Vite 已关闭。该证据不替代 Tauri。
- 同一个独立 `gpt-5.6-luna / max` Reviewer 已确认原 P1/P2 和 raw provenance 全部关闭，
  **代码与证据审查通过**。工具没有 Fast 开关，未启用/未宣称 Fast。
- [审查完整记录](review.md)、[验证汇总](verification.json)。新 raw 在已提交 `1e0cfc7` 上重跑，HEAD/hash/表格一致。

## 缺口与下一步

Antigravity 已补构建和进程记录；Codex 经 User 当次授权后，已在真实原生 WebView 验证 Mock→Live、任务页阻断、任务页刷新与连接失败行为，见 [原生窗口证据](tauri-native-evidence.md)。
原生旧别名及其他深链仍未覆盖。续接工具集缺少 CUA，当前无法继续窗口操作；恢复工具后直接复用已有产物和证据，完成剩余项再关闭本批门禁，无需重新授权。

没有迁移用户库、复制凭据、启用 MP-1、推送、合并或部署。模型调用为 0，Provider usage/cost unknown；
Host 两模式、真实生命周期执行、新 FTS 和真实模型链路按后续对应批次验收。
