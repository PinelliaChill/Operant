# B2-6 / MP-5 独立审查（最终）

记录身份：独立 Reviewer Codex（gpt-5.6-luna/max）。审查日期：2026-09-17（Asia/Shanghai）。
本轮只读增量检查实现树 `/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-6` 的 root 修复、Remote、GUI/TUI 和 Sharing Writer 验证边界；没有修改实现。

审查快照：`codex/b2-6-experience-sharing`，产品源码提交为
`e41cf5699445c4785b0f164d20766b74eb2eba54`（相对基线
`b871b3310c501ee0c08e761520bf824df2427de4`）；随后仅有测试期望修正提交
`dce035dfc5c59d82b9de704c48899319b5dc4b85`。本轮复核了产品提交及该测试提交，未修改实现；
最终 `delivery_head`尚未由本 Reviewer 确认，下面是独立代码审查与证据复核，不是合并/部署授权。

## 本轮 P1/P2 结论

root 随后补齐了本轮发现的三处 Writer 边界；下列 C-11～C-13 已在产品提交
`e41cf56`及其当前测试提交中复核关闭。因此本轮没有保留的 Writer P1/P2；完整门禁的
有界复用和最终证据状态见 C-14。

## 已关闭或撤销的审查项

### C-01（原 P1-01）：同 Session 无 Agent 限制的历史可以按策略复用

root 已将 `experience_runtime.py:31-54` 的快照校验改为使用 `b26_run_context` 行的 `originating_agent_id` 校验历史快照原始身份，并继续用当前 Agent 检查 `version.agent_ids`；当前 Session/role/model 仍由 `assert_snapshot_usable()` 核对。该策略明确允许没有 Agent 白名单的同 Session follow-up 复用历史，同时阻止仅授权原 Agent 的内容流向新 Agent，不应把无白名单知识误报为 Agent 私有。新增的同 Session 第二次未撤销调用通过、撤销后第三次调用阻断；定向 `tests/test_b26_runtime.py` 通过。因此原 P1-01 关闭。

### C-02（原 P1-03）：API 数据集投影已限于当前项目绑定

`src/operant/api_b2_6.py:79-114` 现在先解析当前项目的 `installation_id`/`dataset_id`，再只投影匹配的数据集。静态复核未再看到把整个 Registry 数据集列表返回给请求项目的路径，原 P1-03 关闭。

### C-03（原 P2-02）：GUI 两个入口互斥挂载

`clients/gui/src/features/management/LiveManagementView.tsx:953-959` 使用 `tab` 条件只挂载当前选中的 `KnowledgePanel` 或 `SkillsPanel`；两处 `B26ExperiencePanel` 声明不会同时存在于 DOM。两者提交仍经过同一服务端命令/CAS 状态校验。原“并存重复面板”问题撤销；若最终路由改为并行挂载需重新复核。

### C-04（原 Writer source P1）：非 memory_version 来源现已进入 Core canonical authorization

`src/operant/memory_plugins/sharing.py:519-611` 的 `_core_authorize_writer_source()` 仅接受 Core 当前具备 canonical authority 的 Item，调用 Manager authorizer 并再次核对 Item 内容 digest；候选创建、绑定/复核和晋级约束路径均调用它（`:1920-1927`、`:2168-2175`、`:2280-2287`）。未知 source type 或无法解析的 Item 现在 fail closed。原 Writer source 身份缺口关闭。

### C-05（原 Remote Session P1）：无绑定 Session 现已拒绝

`src/operant/memory_plugins/remote_memory.py:360-403` 现在要求 Session 存在可核对的 canonical Thread，并比较 Thread workspace；无绑定或查找失败返回 `session_binding_unavailable`。新增 Remote 定向测试覆盖无绑定 Session，原跨项目身份缺口关闭。

### C-06（原 Sharing epoch P2）：同作用域授权现已核对 epoch 和来源链

`src/operant/memory_plugins/sharing.py:1141-1206` 对带/不带 `grant_id` 的同作用域路径都读取当前 epoch，并调用 `_version_sources_current()`；过期目标 epoch 或来源链 epoch/digest 不匹配会拒绝。原 P2-01 关闭。

### C-07（原 Remote batch P2）：上传候选现已在单一 Ledger 事务中提交

`src/operant/memory_plugins/remote_memory.py:989-1170` 使用 Ledger write transaction，把候选版本、proposal、幂等记录和 upload receipt 一起提交；异常回滚整批。`tests/test_b26_remote_memory.py:301-401` 注入中途失败后检查没有残留行，并验证同一 upload 可安全重试。原 P2-04 关闭。

### C-08（原 GUI/TUI draft P2）：草稿动作现已前置拒绝

GUI `clients/gui/src/features/management/b26-view.ts:76-91` 对无 published head 禁用停用/回退并在 command builder 再次拒绝；TUI `clients/tui/operant_tui/experience.py:73-76` 同样前置拒绝。`clients/gui/test/b26-experience.test.ts:54-63` 覆盖两类草稿动作，原 P2-02 关闭。

### C-09（Remote 修复证据）：v18 frozen checksum 下 13 项 Remote 定向测试通过

root 已报告使用真实 v18 frozen migration checksum、全新数据库且不注入 checksum hook 重跑 Remote 13 项定向测试全部通过；其中无绑定 Session 和多候选失败回滚也有源码/测试覆盖。该结果属于 root 的验证回报，Reviewer 没有把早先带临时 hook 的结果复用为最终门禁。

### C-10（原 Writer verification refs P1）：Core 生成工件并在晋级前重验

本轮对冻结提交的 `sharing.py:2432-2770` 与 `ledger.py:1469-1679` 复核确认：

- `verify_writer_memory()` 对非空调用方 refs 直接写入 blocked/failed，不把调用方字符串纳入通过证据；空 refs 才会先读取真实 clean Git target，再由 Core `create_artifact()` 生成
  `application/vnd.operant.writer-target-verification+json` 工件。
- `_validate_verification_artifacts()` 通过 Core ArtifactStore 读取工件，核对字节 SHA-256、媒体类型及完整 merge/target 报告；`evaluate_writer_promotion()` 再次读取当前 Git identity、工件内容和 `_verification_digest()`。
- `ledger.promote_verified()` 的唯一当前调用点是 `sharing.py:2870`；它保留
  `candidate.evidence`，冻结的正式集成断言 `tests/test_b26_writer_integration.py:389-451` 明确验证
  伪 ref 先 blocked、随后由 Core 生成工件并发布，且发布版本 `evidence == "inferred"`。

因此原“任意 verification ref 可直接 passed/tested”的 P1 在正常 SharingService 路径已关闭，也不把
`business_tests_attested: false`误写成业务测试证据。root 提供的 `writer-live-final-03.json` 证明默认 TrustedGit 路径在真实
`gpt-5.6-luna` 链路完成，但不能替代上述 adversarial API 边界复核。

### C-11（本轮 P1）：Merge target 已绑定 Project registration

root 的定向修复使 `_merge_for_evidence()`（当前 `sharing.py:2451-2501`）重新读取 active
registration，核对 `evidence.workspace_id`、Core workspace initialization hash、Project 归属，
并要求 adapter 解析出的 target 路径与该 registration 的真实 workspace 完全相同。因而此前
“Project A candidate 使用另一个 mapped clean target”路径在生成验证报告前即拒绝；当前没有保留该
P1。新增定向回归 `tests/test_b26_writer_integration.py:607-649` 将 target adapter 切到另一
clean worktree，确认 promote 返回 blocked 且 evidence 仍保持 eligible。

### C-12（本轮 P1）：Merge result ref 已强制 Git commit identity

当前 `_target_git_identity()`（`sharing.py:2432-2450`）在执行任何 Git 读取前要求非空 `git:`
result ref，并将其规范化后与目标 worktree 的实际 HEAD 精确比较。successful reconcile 仍可记录
通用字段，但非 Git ref 无法进入 Writer 验证/晋级；正常 `TrustedGitMultiWriterAdapter` 的
`git:<commit>` 结果与 `writer-live-final-03.json` 路径一致。新增定向回归
`tests/test_b26_writer_integration.py:653-687` 注入 opaque reconcile result，确认 promote
blocked 且 evidence 不被改写；此前 opaque result 旁路关闭。

### C-13（本轮 P2）：已发布 evidence 的旧 refs 请求保持幂等只读

当前 `verify_writer_memory()`（`sharing.py:2576-2595`）先判断 revoked/published，再处理调用方
refs；已发布 evidence 直接返回，不会被旧客户端的非空 refs 清空或改成 blocked。候选阶段的非空
refs 仍会 fail closed，且 `tests/test_b26_writer_integration.py:389-451` 覆盖伪 ref blocked 后
重新以空 refs 走 Core 生成工件的流程；`tests/test_b26_writer_integration.py:691-723` 再确认
已发布版本收到伪 ref 后保持 revision、refs 和后续召回不变。此前 Ledger 已发布而 evidence
projection 被错误阻断的 P2 关闭。

### C-14（最终门禁与证据复核）：无保留 P1/P2，门禁采用有界复用

产品提交 `e41cf56` 的 Writer 修复及三项回归已纳入当前 `code_head` 的测试提交
`dce035d`。我在全新 v18 临时库中定向运行了 `tests/test_b26_writer_integration.py` 的
target registration、opaque result 和 published refs 三项用例，结果为 `3 passed`；root 另报告
同文件四项 Writer 回归与 Sharing 定向六项通过。三段最终真实模型证据
`skill-live-final-03.json`、`sharing-live-final-03.json`、`writer-live-final-03.json` 均为
`completed: true`，我逐项重算了其 134 个 `src` 文件 hash，三份均无 mismatch。

`gates.json` 记录的完整套件 `pytest-final-03.xml` 为 1050 passed、1 个旧版本断言失败、1 个
Docker 条件 skip；失败是 `tests/test_phase45_migration.py` 仍断言 `[17] * 8`，随后仅测试提交
`dce035d` 改为当前 v18 的 `[18] * 8`，`pytest-phase45-final.xml` 对该文件 16 项复跑全通过。
按测试身份合并为 1051 passed、1 个 Docker 条件 skip；这是规则允许的测试文件定向复用，不能
表述为一次完整套件全绿。`gates.json` 同时记录产品源码未变、ruff format/check、mypy 134、
offline lock、diff、GUI121/typecheck/build、TUI12/真实 HTTP、Schema 两次确定生成及 native
证据；Docker skip 与未验生产 HTTPS connector 仍保留边界。

截至产品提交 `e41cf56` 与测试提交 `dce035d`，本 Reviewer 未发现新的 P1/P2，C-10～C-13
均已完成复核。该结论限于 B2-6/MP-5 当前代码与列明证据，不代替 User 的 merge/deploy 决定，
也不把确定性测试、headless TUI、InMemory/适配器 Remote 或 `business_tests_attested: false`
升级为超出证据范围的生产/业务测试通过。

## 已核对的运行时结论和边界

`ExperienceRun.guard()` 已被接到 `PersistentContextComposer.compose()`（`src/operant/application/context.py:139-149`），而 `AgentLoop` 在返回 ComposedContext 后才调用 Provider（`src/operant/runtime/loop.py:201-217`）。因此在当前非并发路径中，Skill/共享历史会在每次 Provider 请求前再次检查；`ApplicationService.run_session()` 也把 `experience_run.content()` 放入同一 Composer 输入。来源撤销后的历史 Skill 下一次运行会被当前实现阻断；同 Session 无 Agent 限制历史按已确认策略复用，受 Agent 白名单限制的历史仍会阻断。Writer verification refs 及本轮三个相邻边界按 C-10～C-13 已关闭；这只是源码定向复核结论，不等于全量门禁或 J3 通过。

最小验证（使用全新临时库和可写临时 UV cache）通过：

```text
UV_CACHE_DIR=/private/tmp/operant-b26-review-uv-cache \
OPERANT_DB_PATH=/private/tmp/operant-b26-review-db/runtime-root-fix.sqlite3 \
uv run pytest -q tests/test_b26_runtime.py
.. [100%]
```

该测试使用确定性 Provider，源码标题也明确标注为 “not real-model acceptance”，不能替代真实模型/正式 Target/真实桌面 J3。Sharing grant 的 tuple JSON 序列化已在当前快照修复，并以单测 `tests/test_b26_sharing.py::test_cross_project_grant_is_explicit_and_revocable` 复核通过；不再把此前的临时序列化失败列为当前问题。

root 已报告 GUI121/typecheck/build、TUI12+真实 HTTP、Tauri 原生真实操作、Schema 两次确定生成，以及 v18 frozen checksum Remote 定向测试；完整门禁的固定证据与复用边界见 `gates.json`，不能把一次失败后重跑的结果写成单次全绿。`writer-live-final-03.json` 的真实模型链路覆盖默认 TrustedGit target；本轮 P1/P2 定向修复已静态复核，生产 HTTPS connector 未验的边界仍保留。

测试提交 `e707922` 已将 future 迁移注入改为版本 19、两个并发迁移断言改为
`[18,18]`/`[18,18,18]`；后续测试提交 `dce035d` 将 `test_phase45_migration.py` 的并发断言改为
`[18] * 8`。这些均与当前 v18 迁移语义一致，不改变产品源码；`gates.json` 的 1051 passed/1
Docker 条件 skip 由完整套件结果和该文件 16 项定向复跑合并而来。
