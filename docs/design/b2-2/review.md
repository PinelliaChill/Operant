# B2-2 独立审查（进行中）

记录身份：Codex Reviewer；适用对象：B2-2 负责人。复核候选为 `b34de8c`（含 `34542eb`、`988fb24` Host 增量；文档证据随后更新）。本文件是独立增量记录，不是整批通过签署。

## 安全增量切片

`src/operant/plugins/protocol.py:541-645` 的 `_managed_parent` 逐级使用 `O_NOFOLLOW|O_DIRECTORY` 并以 `dir_fd` 固定父目录；受管文件读取循环受 `max_bytes` 限制，写入先检查普通文件及 `st_nlink == 1` 再截断，删除使用父目录句柄。`src/operant/plugins/registry.py:1011-1055` 的 `create_resource` 在资源登记前复核受管资源。

独立运行六项安全回归：

```text
env UV_CACHE_DIR=/private/tmp/operant-uv-cache uv run --no-sync pytest -q \
  tests/test_plugin_host.py::test_managed_resource_rejects_parent_swap_after_path_validation \
  tests/test_plugin_host.py::test_managed_file_rejects_hardlink_before_truncation_and_bounds_reads \
  tests/test_plugin_host.py::test_managed_write_keeps_opened_directory_after_concurrent_rename
```

结果：6 个参数/用例全部通过。此切片未发现新的阻断；没有把 sandbox transport shim 当作隔离证据。

## 历史发现与增量裁决

以下 P1 均来自旧候选，已在本轮逐项核对关闭；当前仍阻断项见文末“当前阻断”。

### [已关闭 P1] 旧 RunLease 可清除新 Run 的活动标记（`8baf09f`）

`src/operant/plugins/registry.py:962-983` 的 `release_run` 按传入旧 `lease_id` 更新旧记录后，无条件按 `lease.run_id` 从 `binding.active_run_ids` 删除。序列为：释放 lease A → 同一 binding 重新创建相同 `run_id` 的 lease B → 迟到的 lease A `release_run`；此时 B 仍为 active，但 `active_run_ids` 被清掉。后续 `stop`/`uninstall` 只看该列表（`src/operant/plugins/host.py:603-631, 674-702`），可绕过活动 Run 依赖，破坏 MP-1.4/MP-1.5 的 fencing 与清理屏障。需要在释放时以当前活动 lease 身份做 CAS/fencing，旧/已终态 lease 不能移除新活动 run 的标记，并增加回归。

### [已关闭 P1] Host 回调没有执行来源 scope/epoch 的边界复核（`8baf09f`）

`src/operant/plugins/protocol.py:704-747` 的 `RestrictedHostApi.read_source/search/model` 只比较嵌套 `RpcContext`，随后把插件提供的 `SourceRef.scope`、`SourceRef.permission_epoch`、`RecallRequest.explicit_refs` 和 `ModelProxyRequest.model_profile_id/input_source_refs` 原样转给可选回调。`PluginHost` 没有包装这些回调（`src/operant/plugins/host.py:132-137,396-427`）。因此只要回调实现按请求字段取数据，合法 lease 上的插件即可伪造跨 scope/dataset/epoch 的来源或任意模型 Profile 请求；当前测试回调正是按请求 source 回显（`tests/test_plugin_host_stdio.py:86-88`）。应在 Host/API callback 入口强制校验 dataset、scope、permission epoch、availability 及允许的 model profile，或让回调接口携带不可伪造的 Host binding 并有明确的 enforced contract；补越权回归。

### [已关闭 P1] Cleanup 路径异常与父目录替换没有安全/续做闭环（`34542eb`）

`src/operant/plugins/host.py:783-800` 在 `path_under(...)`（783 行）之后才进入 `try`，父目录被替换成 symlink 时该异常直接逃出 `_apply_cleanup`，计划仍保持 `pending`，`uninstall` 没有失败 Receipt；`resume_cleanup`（828-835 行）也会再次直接抛出同一异常。即使路径校验返回后才发生替换，`_remove_owned_path`（803-817 行）仍以普通 `Path`/`shutil`/`unlink` 操作，未使用受管目录句柄；在受控竞态下可将 `data/owned` 路径切到外部目录并删除外部同名文件。该路径绕过了本轮已加固的 `_managed_parent`，违反 MP-1.5 的可续做清理和受管资源边界。需要把路径解析纳入可记录的失败分支，并让清理按 dirfd/no-follow 语义删除（含目录），对可重试 blocker 保留/恢复明确状态并增加竞态与中断回归。
另外，`registry.complete_uninstall()`（`registry.py:1290-1305`）会把含未完成项的计划持久化为 `state="blocked"`，而 `resume_cleanup()`（`host.py:838-845`）对 blocked 计划直接跳过；清理失败后即使外部条件已恢复也不会再试，必须区分活动 Run/保留锁等真实屏障与可重试 cleanup blocker，并提供可续做路径。

### [已关闭 P1] Session 无 Agent 的失败事件没有进入 Task/History 终态投影（`caa2ba8`）

`src/operant/application/service.py:3353-3365` 在 Agent Factory 失败时只提交 `session.run_failed`（`agent_id=null`），这是架构规定的合法终态事实；但 `src/operant/api_b2.py:169-194` 只从 `agents` 行推导 Session Task 状态，没有 Agent 时固定返回 `created`，`src/operant/api_b2.py:325-340` 的 history 也只返回空 Agent/Item。实际注入 Factory 异常后，SSE 已包含 `session.run_failed`，而 `/v1/b2/tasks` 仍显示 `created` 且 cancel 为 `no_active_run`。这使 GUI/Task Projection 丢失失败终态，不能满足终态优先和可核对的任务闭环；应按同 Session 的最新持久终态事件投影无 Agent 失败，并加 API 回归。

### [已关闭 P1] Stop/Uninstall 可在受信进程内调用仍在执行时宣称完成（`34542eb`）

`src/operant/plugins/host.py:624-646` 先释放 Run、移除 engine，再调用 `InProcessPluginEngine.close()`；`src/operant/plugins/protocol.py:912-942` 的 `close()` 只调用插件自身 `close`，不持有/取消 `PluginHost.invoke` 已创建的 asyncio task。实测一个正在等待的 trusted `handle()` 调用中执行 `disable(..., stop_run_ids=(...))` 立即返回 `state=disabled`，`host._calls` 仍保留请求且任务未结束；释放等待后任务仍继续。Uninstall 具有同一时序（`host.py:696-727`）。因此安全停止/卸载完成 Receipt 不能证明在途可信代码已收束，旧代码可继续执行自身副作用，违反 MP-1.2/MP-1.4 的取消、Run 依赖与安全重启边界。应追踪并等待/取消所有关联调用，无法强制终止时返回 `restart_required`/失败并保留明确状态，增加 in-process 在途 stop/uninstall 回归。

当前待集成的 quiesce 修复仍有一个 Run 竞态：`host.py:670-671,759-760` 在等待在途调用期间设置了 `_lifecycle_fences`，但 `host.py:375-387` 的 `start_run()`/`registry.py:919-955` 没有检查该 fence 或卸载状态，新的 lease 可以在窗口内创建。随后 `stop` 的 `begin_disable()` 可能抛未包装的 `cleanup_blocked`，`uninstall` 可能关闭引擎后留下活动 lease。新 lease 必须在生命周期转换开始后被拒绝，并补并发回归。

当前 cleanup 增量在 `uninstall` blocked 返回路径（`host.py:817-828`）解除 `_lifecycle_fences`，但 `registry.begin_uninstall()`（`registry.py:1223-1228`）尚未关闭 binding，`registry.create_run()`（`registry.py:919-955`）也只检查 `binding.enabled`。因此 blocked uninstall 后仍能创建新 lease，而引擎已关闭，反过来永久阻塞清理；该 fence 需保持到明确恢复/完成，或由 Registry 原子禁用 binding 并拒绝新 Run。

`host.py:786-794` 的 trusted `cleanup()` Hook 仍直接 await，没有生命周期 deadline 或超时后的 `restart_required` 收口；一个不返回的 Hook 会让卸载永远没有 Receipt，也阻塞后续 Host inventory 清理。MP-1.5 只允许 Hook 缺失/崩溃成为可清理场景，无法安全释放的进程内代码应按安全重启失败处理，并补受控超时回归。

### [已关闭 P1] Manifest capability 没有约束嵌套 Host API（`34542eb`）

`src/operant/plugins/host.py:472-478` 只在 `PluginHost.invoke` 的顶层 engine operation（`extract/recall/maintain/on_index_event`）检查 `manifest.capabilities`。同一调用创建的 `RestrictedHostApi`（`host.py:396-433`）始终暴露 `read_source/search/model`，而 `src/operant/plugins/protocol.py:772-841,923-940` 没有按 manifest capability 或 binding policy 再做操作授权。因而只声明 `extract` 的插件，只要 Host 配置了回调，就能从 `handle` 内调用 `host.search`、`host.model` 或 `host.read_source`；现有授权回归的 package 统一声明全部四项，未覆盖这个最小权限边界。MP-1.1 要求能力与配置绑定，需把允许的 Host API 操作映射并传入 Host API，或在调用入口拒绝未声明操作并补单能力回归。

### [已关闭 P1] 直接 engine payload 尚未通过 Host 授权边界（`34542eb`）

`src/operant/plugins/host.py:454-516` 只校验请求类型、lease/context、manifest 顶层 capability 与字节预算，然后把 `SourceBatch`、`RecallRequest`、`MaintenanceInput`、`IndexEvent` 原样交给 engine；`_validate_result`（`host.py:435-452`）只检查结果类型和 request_id。`SourceBatch.sources`/`IndexEvent.head` 可携带插件自填的 scope、dataset、permission epoch 或 memory head，`ProposalBatch` 的 `owner/base_head/proposed_version/source_refs` 及 `CandidateBatch.candidates[].ref` 也可能跨 dataset/scope。新增 callback authorizer 只覆盖 Host API（`protocol.py:772-841`），没有覆盖 direct engine 入参/出参。需要在 Core/Host 边界校验来源、head、proposal owner/dataset/scope/epoch 与引用，拒绝未授权 payload，并增加 extract/recall/maintain/on_index_event 的越权回归。

## 首轮范围状态（2026-09-11）

以下范围已完成首轮核对，未发现新的阻断：

- `src/operant/plugins/registry.py` 的安装/认证撤销/epoch/Run lease、`src/operant/plugins/protocol.py` 的 stdio 双向 RPC、预算/取消/资源监测，以及 Host 资源 keep/delete/重启续做；`34542eb` 的目录句柄/在途收束和 `b34de8c` 的登记预算已完成定向复核。
- `src/operant/api_b2.py`、正式 SDK surface、`src/operant/application/service.py` 与 `src/operant/persistence/sqlite.py` 的 B2 Task/Agent/API 和 canonical Event+Item 原子提交；普通 Thread 作为上下文引用时保持只读。
- 终态优先投影与 cleanup、`clients/gui/` 的 GUI-L1 任务/审批/取消/断线/历史回读，真实 Tauri 的 Session 新轮和既有 Workflow Graph 安全恢复入口；这些范围沿用 `caa2ba8` 切片通过结论。

以下范围仍待负责人交还整合版后做增量复核，不能据此宣称整批通过：

- 同一 installation 的并发 `stop()`/`uninstall()`/`Host.close()` 生命周期互斥仍未实现；本轮实际复现见“当前阻断”。
- `PluginConfig.maintenance_enabled`/`recall_token_budget` 是否属于本批 Host 强制绑定仍需负责人在交付范围中明确；当前代码未使用这两个字段，若它们属于 MP-2/MP-3，应在限制中明确。
- 整合候选的最终 `docs/design/b2-2/verification.json`、gate 日志和与新代码匹配的 Tauri/stdio 证据引用仍需最终门禁确认。

## caa2ba8 Session/GUI 增量结论（2026-09-11）

此切片已完成，结论为通过（仅针对已提交的 Session/B2/GUI 范围，不能外推到未交还的 Host worker）：

- `src/operant/api_b2.py:88-106,157-257,318-355` 从持久化 Agent/Session/WorkflowRun 和终态事件生成 B2 projection；无 Agent 的 `session.run_failed` 会进入失败 Task，后续新 Agent 以新轮时间覆盖旧失败；历史保留 typed Item 结构并逐字段脱敏，分页使用 canonical cursor。
- `src/operant/application/service.py:3270-3375` 对显式绑定当前 Session 的 Thread 写入 User Item，并在 setup/Factory 失败时以 Event+SystemItem 的同一持久化路径记录失败；普通 Thread 上下文引用不写 canonical history。`src/operant/persistence/sqlite.py:9790-9835` 的 `BEGIN IMMEDIATE` 与 Event+Item 同事务满足原子提交。
- `clients/gui/src/live/LiveContext.tsx:311-351,773-833,1184-1271,1351-1361` 通过正式 B2 Task.actions 决定取消可用性，Agent started/审批/终态触发权威 projection/history 刷新；错误、断线和 manual reconcile 不静默回退或猜测终态。
- `sdk/typescript-client/b2.generated.ts`、`sdk/python_client/b2_generated.py` 与 B2 Schema 的生成/确定性回归已核对；`client-review-increment.json` 的源文件 SHA-256 与 `caa2ba8` 对应，并记录了原生 Tauri Factory 失败→Task failed、同 Session 正式 `gpt-5.6-luna` 新轮结果 42→completed、审批等待取消→`agent.cancelled` 的闭环。上述真实证据不替代 Host worker 的最终集成门禁。

本切片没有新增阻断。B2 Task 页面仍按首屏 API 页读取，完整任务列表的更多页不是本次客户端实测范围；Host cleanup/inflight/fence、direct engine payload 接入和最终 verification/gate 仍待负责人交还后复核。

## 当前阻断

### [P1] 同一 installation 的生命周期操作没有互斥

`host.py:781-1014` 的 `stop()` 与 `uninstall()` 都直接设置 `_lifecycle_fences` 并在多个 `await` 后继续推进，没有 per-installation transition lock，也没有在已存在 fence 时返回稳定的幂等/冲突 Receipt。临时内存复现（`b34de8c`）：在一个 cooperative in-process 调用期间并发调度 `uninstall()` 与 `stop()`，`uninstall` 可直接抛 `PluginError(stale_epoch)` 而没有 Receipt，`stop` 返回 `restart_required`；最终 engine 已移除、binding 仍存在、安装状态为 `restart_required`。反向调度也得到 `stop=disabled` 与 `uninstall=restart_required` 的不一致结果。`Host.close()` 通过 `host.py:1136-1153` 调用 `stop()`，因此同类竞态仍存在。需要串行化同一 installation 的 lifecycle transition，或明确将并发请求收敛为可核对的冲突/既有 operation Receipt，并补回归。
