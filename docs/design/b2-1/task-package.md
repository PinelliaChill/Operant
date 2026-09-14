# B2-1 任务包 v1

记录身份：Codex；适用对象：Codex 执行子 Agent、Antigravity、后置 Reviewer。2026-09-09。
协调方与公共契约唯一写入者：Codex 主 Agent。范围：B2-1-A / MP-0；B2-1-B / GUI-L0。

治理根：`/Users/bigo/agentworkspace/codexworkspace/operant`，治理文件不随 Git worktree 复制。
按上述根目录读取执行提示词第 1 节、沟通规则；只读统一计划第 3、4 的 B2-1 和第 8 节、记忆草案 §13.3；必要时按符号追查。

## 治理版本（SHA-256）

- `AGENTS.md`: `678ecd078ef9ba7afb17139b9a65590eb5fe9de12a0b352348622900c9d10eb9`
- `MEMORY.md`: `7b217d78279a7947c3895b5f041458f7c3f67a92c312ec01ced542c51cddffe7`
- `memory/communication/README.md`: `16c0f45025001f271e889720a16b1d36b8f30d6e127306c3a09229476da4c393`
- `docs/design/Operant-Beta-2.0执行提示词.md`: `f5a90c4fe58ca348a636c43d46a23395969ca1bf0206bf0237f8cb1ada7a3ba6`
- `docs/design/Operant-Beta-2.0更新计划.md`: `2bcc96b4168e48d8a4464ae847a76d84b0d7a31166803e210e9c63f74ad8d516`
- `docs/design/记忆系统设计草案.md`: `85fc4d7e9a7a88116d47e96eb27dbe61b266a9a4df7b5f5f84020dbd42bbdef6`

## 实施与归属

- 主线经 GitHub API 核对并 fetch：`ecb00437e9a44a5e79d54e8cf4944fd0d456bf02`。
- A / 集成 worktree：`/private/tmp/operant-b2-1-a`，分支 `codex/b2-1-contract-baseline`，初始 HEAD 同上、clean。
- 治理根仍为 `codex/phase1b-context-composer@2b54b073a0d6b8a1677253502db5cff33a601996`；既有 dirty：两个目标设计文档；untracked：clients/、sdk/、docs/design/、四个截图。全部保留。
- Codex 主 Agent 独占 `src/operant/contracts/`、`sdk/protocol/` 新 B2-1 契约与生成器、生成 Client、迁移映射、契约测试、当前架构文档和集成。保持当前运行协议和 SQLite v14，不运行用户库迁移。
- Codex 基线子 Agent：只读源码核对；仅写 `docs/design/b2-1/source-baseline.md`，反馈迁移映射证据，不编辑共享契约。
- Codex 评测子 Agent：只写 `tests/fixtures/b2_1/`、`scripts/benchmark_memory_baseline.py`、`tests/test_b2_1_benchmark.py`、`docs/design/b2-1/evaluation-baseline.md`。合成数据、临时 SQLite、直接旧策略基线，不实现 Host/FTS，不读取 .env 或用户库。
- Antigravity：独立 B 线，负责 `clients/gui/`、`clients/desktop/`（实际路径确认后细化）。不得写 sdk/；SDK 合成返回缺口交 Codex。B 实施 worktree/HEAD 待实名确认，不假称已启动。
- 交接：`COM-20260909-001`，治理根 `memory/communication/items/COM-20260909-001.md`。B 完成必须给可读 commit/patch、实际 HEAD、真实 Tauri 入口与 Live 隔离证据。

## 验收及排除

A：可检查版本化契约/fixture、所有权与清理可表达、未知 scope/owner 失败、迁移映射、合成开发/保留集、原直接路径结果/成本和冻结性能门。协议生成确定性；代码交付按 AGENTS 完整基础门禁。
只提供契约和基线，不启用新运行 API，不进入 MP-1，不迁移用户库，不读凭据、不改服务、不推送/合并/部署。
B：统一计划 GUI-L0；嵌套路由、旧深链、刷新、模式切换、Adapter/SDK 合成返回与显式 unsupported，真实 Tauri 验证。
两线实名交接后 Codex 集成，再新建独立 Reviewer，实际 gpt-5.6-luna / max。当前子 Agent 工具没有 Fast 开关，不宣称已启用 Fast。返工复用同一 Reviewer。
所有内部子 Agent 继承上述渐进读取与简短交接约束，不另行委派，不复制 B 线工作。

## v1 接单补充（2026-09-09，Codex）

User 已转交 Antigravity 实名确认：B worktree `/private/tmp/operant-b2-1-b`，分支
`antigravity/b2-1-gui-live-isolation`，初始 HEAD 同 A；Codex 已只读核对。
B 负责 `clients/gui/src/` 路由、模式/上下文、Tasks/Agents/Run/Collab/workflowdir 隔离和对应测试，
以及 `clients/desktop/` 真实 Tauri 验证。`sdk/typescript-client/http-client.ts` 保持 Codex 单一写入，
A 已修复旧 Adapter 的合成对象并添加 `sdk/typescript-client/http-client.test.mjs`。
接单确认不等于代码完成或桌面验收，最终 B commit/patch 与证据待交接。
