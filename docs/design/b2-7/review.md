# B2-7 独立 Reviewer 初审与增量复核

记录身份：Codex 独立 Reviewer；适用对象：B2-7 执行者与 root。Fast 工具开关未暴露，本次不宣称启用。

> 当前结论以文末“最终独立签审（审查源 `684bea1`）”为准；前面的初审与增量复核段保留历史证据，不代表当前收口状态。

## B25/B26 大 Projection 增量复核（代码冻结 `b932e30`，当前读点 `6661ef5`）

本次只审 B25/B26 Command Projection 修复及其回归，不重审上方已关闭的兼容读授权问题。独立执行：

```text
UV_CACHE_DIR=/private/tmp/operant-review-uv-cache uv run --offline pytest -q -o addopts='' \
  tests/test_b27_command_projection.py
2 passed in 14.93s

UV_CACHE_DIR=/private/tmp/operant-review-uv-cache uv run --offline pytest -q -o addopts='' \
  tests/test_b27_command_projection.py tests/test_b25_api.py tests/test_b26_api.py
11 passed in 34.78s
```

在本次三文件改动中未发现新的 P1/P2：

- `src/operant/api.py:1385-1388` 将 `command_outcome_unknown` 映射为 `manual_reconcile`，`1498-1505` 仅把 `/v1/b2-5/commands` 与 `/v1/b2-6/commands` 从通用 M0 截断/快照 journal 中排除；B25/B26 自己仍先经过 `phase45_action_gateway`，再访问各自持久命令表。
- `src/operant/api_b2_5.py:422-452,525-542` 与 `src/operant/api_b2_6.py:247-275,322-347` 对首次响应和幂等回放都构造完整 typed `B25Result`/`B26Result`，再统一调用既有 `_bounded_projection`；未知状态直接返回 409，不重放业务动作。回归实测两端点各 16 条记录、脱敏、完整 `affected_ids`、幂等回放标记和单事件均成立。
- 代码仍先比较同一幂等键的 `request_digest`/`project_id`，不匹配走冲突边界；定向回归确认 B25/B26 业务事件不会因回放或未知结果重复增加。本次未观察到权限绕过或结果日志旁路。

该结论只覆盖这三文件及定向测试。`42b2186` 的全门禁运行曾主动中止并保存为 interrupted，不能据此签总体门禁；最终学习真实重试和其余冻结证据仍由 root 汇总，不能因本节通过而提前签审。

## 增量复核（冻结修复 SHA `2bf45c4a2263490880bc1e351cc8c5612d158809`）

本次只复核 `002f98c` 初审中记录的 P1/P2 修复，没有改产品或测试。独立执行：

```text
UV_CACHE_DIR=/private/tmp/operant-review-uv-cache uv run --offline pytest -q -o addopts='' tests/test_b27_compat_authorization.py
5 passed in 7.52s

UV_CACHE_DIR=/private/tmp/operant-review-uv-cache uv run --offline pytest -q -o addopts='' \
  tests/test_memory.py tests/test_workflow_memory.py tests/test_b27_fault_migration.py
12 passed in 15.67s
```

原 P1/P2 在该 SHA 上均已关闭，未复现新的同等级问题：

- **P1（已关闭）**：`ApplicationService.get_memory/query_memories` 现在把 `session_id`、显式 `kinds` 传入 Manager；`_compat_authorize_version` 逐版本检查 dataset/scope、binding、permission epoch、发布头、role、可信 Agent、condition、来源治理依赖和 Core `_authorize_memory`，`_compat` 也保留 `role_scope`。五项回归覆盖角色/类型、Core live lease Agent、跨项目/撤销和当前/历史版本；独立运行 5/5 通过。无 live lease 或 Agent 非 `RUNNING` 时仍拒绝 Agent 限定版本。
- **P2（已关闭）**：Manager 存在时 `list_memory_versions` 已路由到 `compat_list_versions`，并对每个 immutable version 执行历史发布、当前 head、scope/role/Agent/condition/source/epoch 校验；无 Manager 的迁移兼容分支也逐版本调用 `_authorize_memory`。独立历史版本复现现在拒绝旧角色受限正文，当前公开版本仍可读。

这只是受影响授权路径的增量关闭判定，不是 B2-7 总体签审；真实模型、最终评测脚本/fixture、最终候选包和其他尚未回读的验收证据不因本次测试通过而改变状态。后续如有新的冻结 SHA，只需按本节证据与修复差异增量复核。

## 初审结论（冻结核心 SHA `002f98cd5b5f8a5df1a9360ad40bd19d0c5b862d`）

本次针对冻结核心 SHA `002f98cd5b5f8a5df1a9360ad40bd19d0c5b862d` 的初审发现 1 项开放 P1 和 1 项开放 P2，暂不通过独立签审，也不把真实模型、完整桌面、候选重打包或完整门禁标为通过。P1 是兼容 Memory 读路径丢失 `role_ids/agent_ids` 后造成的权限绕过，必须在任何候选收口前修复并回归。

审查基线为治理根 `/Users/bigo/agentworkspace/codexworkspace/operant`、`workflow-20260915.1`、实施树 `.worktrees/b2-7`，基线 `5b7833aa0a9c3d7c1cddc44689654d2c1509297c`。冻结树当前仍有 root 正在维护的脚本/测试和 B2-7 证据文件；本报告只写本文档，没有修改产品或测试代码。

## 初审开放问题（已由上方增量复核关闭）

### P1：兼容读丢失角色/Agent 限制，返回未授权正文

位置：

- `src/operant/application/service.py:2727-2755`：Manager 存在时把 `get_memory`、`query_memories` 路由到兼容适配；
- `src/operant/memory_plugins/manager.py:1504-1569`：`compat_query` 只调用一次 `PROJECT` 类型授权，`_compat` 没有把 `MemoryVersion.role_ids` 或 `agent_ids` 映射到兼容 `Memory.role_scope`/会话约束。

复现方法是在隔离 Core 中创建 `memory-standard` 项目并发布一条版本，然后把新版本设为 `role_ids=("role-other",)`。当前角色的快照仅为 `read: [project]`。调用：

```python
service.get_memory(
    record_id,
    snapshot=session.role_snapshot,
    session_id=session.id,
    project_scope=workspace,
)
service.query_memories(
    "restricted",
    snapshot=session.role_snapshot,
    session_id=session.id,
    project_scope=workspace,
)
```

在冻结 SHA 上两者都返回了受限正文；观察到的结果分别为 `version=2, role_scope=()` 和同一条记录的 `role_scope=()`。正确结果应是拒绝或空结果，因为当前角色/Agent 不在版本允许范围内。常规 `MemoryManager.begin_memory_run` 的正式召回链已有 `allowed_role_ids/allowed_agent_ids` 过滤，但兼容查询和显式引用绕过了该过滤；旧 `/v1/memories/search` 也进入该路径，`run_session` 的显式 Memory resolver 同样可能使用它。

修复要求：兼容读必须保留并重新校验版本的角色、Agent、作用域、发布头、来源和当前权限 epoch，或统一复用正式 Manager/Recall 授权链；补充角色限制、Agent 限制、跨项目和撤销后的 `get/query` 回归，未通过前保持 P1 开放。

### P2：`list_memory_versions` 仍绕过 Manager 治理

位置：`src/operant/application/service.py:2828-2847`。该方法无条件调用 `store.list_memory_versions()`，只用最后一个版本的 kind/scope/role 做一次 `_authorize_memory`，随后把全部历史版本原样返回；Manager 已启用时也不检查插件 dataset 的发布头、来源依赖、撤销/删除状态、每个历史版本的 role/Agent 限制或权限 epoch。

隔离复现：版本 1 为其他角色和私有项目作用域，版本 2 改为当前允许作用域；调用 `list_memory_versions` 后仍得到版本 1 的私有正文。当前没有发现直接 HTTP 路由，但这是公开的 ApplicationService 兼容读入口，未来评测、上下文解释或客户端调用会重新打开旁路。应明确只读历史契约：要么把该调用接到 Manager 的受治理历史查询，要么在无 Manager 的兼容范围内逐版本授权并拒绝跨作用域/角色历史；补充回归后再关闭。

## 已核对且暂未发现新增问题

- Service 旧写入口在冻结核心上统一抛出 `schema_upgrade_required`；Workflow 的最近条目拼接、完成后候选写回和验证命令关键词晋级已移除，`persist_memory_candidates=True` 也明确拒绝。定向 `tests/test_memory.py tests/test_workflow_memory.py tests/test_b27_fault_migration.py`：`12 passed`。这只能证明确定性边界，不能替代真实模型/桌面验收。
- `src/operant/package_resources.py` 的 source checkout 回退路径和 wheel 内 `operant/protocol_schema` 定位逻辑一致；已有 `package-preflight-02.json` 记录安装 wheel、两种插件 save/search/keep 和十个协议入口通过。该证据仍需随最终候选重打包重新生成，不能沿用旧 wheel hash 作为最终包证明。
- 故障/迁移测试和 `migration-final.json` 明确区分合成替身、真实 macOS `sandbox-exec` 最小进程与生产能力；当前没有因这些测试扩大隔离、升级或回退声明。原子升级编排仍是文档列明的范围限制。

## 尚未完成的收口门

- `docs/design/b2-7/gates.json` 的 `pytest` 步骤为 exit code 1；root 当前报告为 1056 通过、5 个旧 benchmark fixture 失败及 1 个 Docker 条件 skip。失败/skip 未被本审查改成通过，且当前工作区仍有脚本基线适配改动。
- `docs/design/b2-7/native-fresh.json` 标记 `completed: false`，仍待重装后查询、真实模型、升级环境和断线只读场景。
- B2-7.2 的完整真实 Provider 对照、无记忆/旧策略/新 FTS 三路真实质量结果，以及生成 Client/TUI 的当前冻结版本复核尚未完成；确定性评测或一次 read-only preflight 不能替代它们。
- 候选包需要在最终 SHA 后重建并重新核对 wheel/sdist、插件资源、协议 digest 和哈希；未签名、Core 依赖、Remote/Docker/Host 性能限制仍须在最终交付页保留。

## 增量复核规则

本报告包含 `002f98c` 初审和 `2bf45c4` 增量复核，不是最终签审。若评测脚本、桌面证据或候选包重新生成，须核对其 `evidence_head` 与源码/依赖输入一致后再更新结论。任何未完成真实门禁仍保持未验收。

## 最终独立签审（审查源 `684bea1ca994d59ea63df7c34ba2f75cf076c7fc`）

记录身份：Codex 独立 Reviewer；适用对象：B2-7 执行者与 root。审查治理根为
`/Users/bigo/agentworkspace/codexworkspace/operant`，治理版本为
`workflow-20260915.1`，实施树为 `.worktrees/b2-7`，基线为
`5b7833aa0a9c3d7c1cddc44689654d2c1509297c`。本次指定使用 `gpt-5.6-luna/max`；Fast
开关未暴露，因此不宣称启用 Fast。本节只写本报告，不修改产品、测试、评测脚本或候选包。

产品冻结为 `b932e30b31e707191417aeb8ab00b1265906c9ae`，评测/候选审查读点为
`684bea1ca994d59ea63df7c34ba2f75cf076c7fc`。本轮文档回读确认任务包状态为
`accepted_pending_push`，AC-07 已记录最终 Reviewer 签审；评测汇总状态为
`completed_with_recorded_negative_results`，同时保留 `original_fixed_query_learning_complete=false`
和门禁 `raw passed=false`。`b932e30..684bea1` 在
`src/`、`tests/`、`sdk/`、`plugins/`、`clients/`、`pyproject.toml` 和 `uv.lock` 下无差异；
增量仅涉及评测脚本。早先 `002f98c` 的 P1/P2 兼容授权修复和 `b932e30` 的 B25/B26
Projection 回执修复已由上文记录的独立定向回归覆盖，本次没有重新把它们当作新发现。

### AC-01～AC-07 判定

| 验收项 | 最终判定 | 依据与边界 |
| --- | --- | --- |
| AC-01 联合故障 | 通过（范围内） | `fault-migration.md` 记录冷启动、并发、崩溃、撤销/停用、keep/delete、缺包恢复、v18 升级/回退和安全重启边界；最终相关测试 137 项通过，`migration-final.json` 的 v14→v18 行计数/hash 保持，真实 macOS `sandbox-exec` 只按一次本机隔离证据计入。没有扩大到生产库、Docker、Remote 或磁盘安全擦除。 |
| AC-02 真实对照与形成后复用 | 通过（负结果按事实保留） | `evaluation-real-final.json`（evidence `5d41b975...`）以发现的 `gpt-5.6-luna` 完成固定三策略各 4 例、共 12 次调用，答案成功为无记忆 `0/4`、旧策略 `2/4`、新 FTS `3/4`；不外推收益。确定性 `evaluation-deterministic-final.json`（`42b2186f...`）为 `43×3`、无模型调用。形成链通过正式 `ApplicationService.run_session -> B25 propose/review` 生成冻结记录，`evaluation-learning-final-04.json`（`8c4e6c4...`）记录 2 次模型完成和 1 次实际 `read_file`。固定原问题重试（`evaluation-learning-retry.json`，`6661ef5...`）确实选中同一记录、digest/cutoff 一致并完成 1 次模型调用，但拒绝转述推断证据，`answer_success=false`；这是有效的负观察，不是缺少复用步骤。补充问题显式要求说明推断证据与适用条件后成功，但问题已改变、未提升信任，且该报告采集于 `6661ef5` 加未提交脚本改动、随后才提交为 `684bea1`，不能作为 clean `6661ef5` 或原固定问题成功。 |
| AC-03 隔离升级回退 | 通过（受限语义） | `migration-final.json`、`native-upgrade.json`（`2bf45c4...`）和原生 Tauri/Core 证据覆盖合成 v14→v18、旧行保留、空新增表回退及非空新增表拒绝；不宣称跨包原子升级或通用降级。 |
| AC-04 旧旁路收敛 | 通过 | `legacy-cleanup.md` 与既有 5 项兼容授权回归、12 项内存/Workflow/故障回归保持旧写升级拒绝、旧 Workflow 自动读写/关键词晋升移除，历史兼容读逐版本受治理；无新 P1/P2。 |
| AC-05 真实桌面、GUI/TUI、单/多 Agent | 通过（声明范围内） | `native-fresh.json`（`002f98c...`）、`native-upgrade.json`（`2bf45c4...`）、`graph-live.json` 的实际 Core/Provider/Graph 链路，以及产品冻结 `b932e30` 的 `native-projection-final.json` 16 条 typed receipt 与 `tui-projection-final.json` 四步真实 HTTP/断线证据相互分开记录。TUI 证据明确是 headless Textual，不冒称原生桌面；Graph 只声明两 Agent 同链与私有隔离。 |
| AC-06 候选包与说明 | 通过（候选，非正式发布） | `candidate-verification.json` 的产品路径在候选构建后未变，独立 wheel 两插件/10 协议检查及 TUI 12 项测试通过；文档补充了可直接安装 Core 与 TUI wheel 的命令。候选包实际 SHA-256 为 `2a92aa27d2a8f978ddefe975eaae7baf396dd6dc34a0126dbe46130c0b27b0ca`、大小 3,785,659 bytes。包为 macOS arm64、ad-hoc 签名，无 Developer ID/公证/DMG/自动更新，`formal_release=false`。 |
| AC-07 门禁与独立审查 | 通过（签审完成；条件化复用） | `gates.json` 原始读点 `b932e30` 的六个步骤均 exit 0，pytest 为 1070 passed、1 个 Docker conditional skip；`gates-closure.json`（读点 `684bea1`）明确保留 `passed=false`，原因是门禁期间 `scripts/b27_real_evaluation.py` 发生变化，且 `source_unchanged=false`。当前两个评测脚本的 Ruff 检查和 5 项 evaluator tests 另行通过；`src/tests/sdk/plugins/clients` 未在门禁期间变化。因此 1070 项和未受影响产品证据可复用，但不能把这份记录写成当前源的 full gate passed，也不能把 Docker skip 写成容器验收。 |

### 最终结论

本次最终审查未发现新的 P1/P2，也未复现上文已关闭的兼容授权和大 Projection 回执问题。按 MP-6.2 原始要求，形成冻结记忆、在后续正式入口选中并复用它已经有真实证据；固定问题的拒答/失败是模型观察结果，不能因为补充问题成功而改写为成功。因而没有缺失的“必须把固定问题答对”核心门；`evaluation-acceptance.json` 中 `original_fixed_query_learning_complete=false` 应继续作为答案质量结果保留，而不应被解释成复用步骤未执行。

本签审结论为：B2-7/MP-6 在已授权范围内可进入普通工作分支推送和可审阅 PR，属于带限制的候选交付；不授权 merge、deploy、正式发布或真实用户库迁移。本轮候选 ZIP 已按使用说明变更重新核对为
`2a92aa27d2a8f978ddefe975eaae7baf396dd6dc34a0126dbe46130c0b27b0ca`（3,785,659 bytes）。交付摘要必须继续标明：原始门禁是 conditional、Docker 未验、候选未签名/未公证、Remote/生产 Target/Host 性能和外部擦除未验、模型 cost 与 first-token 未测，以及补充复用问题采集时的 dirty-ref 事实。
