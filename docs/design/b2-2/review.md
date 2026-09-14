# B2-2 独立审查（增量完成）

记录身份：Codex Reviewer；适用对象：B2-2 负责人。最终复核候选为 `518bb3f40a70728d79b743212aee6fec098a941f`（文档归档为 `b05f756`）。本文件在既有切片结论上完成最后 Host 增量复核；不替代正式门禁日志，但给出本批独立审查结论。

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

旧段落保留发现当时的代码位置和风险描述，不能理解为最终代码仍有该问题。最终关闭证据为：`8baf09f` 关闭 lease release fencing 与 Host callback 授权；`34542eb` 关闭 cleanup/在途收束、嵌套能力和 direct payload 授权；`caa2ba8` 关闭无 Agent 失败投影；`988fb24` 关闭按 engine operation 配对 model profile；`b34de8c` 关闭资源登记预算；`518bb3f` 关闭生命周期并发与 binding 配置预算。最后一项已在本轮独立复核。

### [已关闭 P1] 旧 RunLease 可清除新 Run 的活动标记（`8baf09f`）

旧候选的迟到 lease release 可能清除同 `run_id` 新 lease 的活动标记；`8baf09f` 已按 lease 身份/fencing 做终态幂等保护，并有定向回归。最终候选沿用该实现，未复现旧风险。

### [已关闭 P1] Host 回调没有执行来源 scope/epoch 的边界复核（`8baf09f`）

旧候选的 Host callback 会信任插件自填来源/记忆引用/Profile；`8baf09f` 已加入默认拒绝的 Core authorizer、scope/epoch/dataset/availability 和 binding Profile 校验，并通过 callback/stdio 定向回归。最终 Host 指纹与该修复一致。

### [已关闭 P1] Cleanup 路径异常与父目录替换没有安全/续做闭环（`34542eb`）

旧候选的 cleanup 路径替换可能逃出受管目录且 blocked 计划不能续做；`34542eb` 已改为 dirfd/no-follow 递归删除、记录 blocked/retry 状态并让 `resume_cleanup()` 续做。安全切片的 6 项回归和本轮生命周期回归均通过。

### [已关闭 P1] Session 无 Agent 的失败事件没有进入 Task/History 终态投影（`caa2ba8`）

旧候选的无 Agent Factory 失败没有进入 Task/History 终态；`caa2ba8` 已补 Service 的 Event+SystemItem 原子路径、API 终态投影和新轮覆盖，并由真实 Tauri 失败→新轮42→取消闭环核对。

### [已关闭 P1] Stop/Uninstall 可在受信进程内调用仍在执行时宣称完成（`34542eb`）

旧候选的 stop/uninstall 会在 trusted 在途调用尚未收束时过早完成；`34542eb` 已追踪调用、取消/等待在途 task、对 cleanup/close 设置有界等待，并在无法收束时保留 engine/资源返回 `restart_required`。本轮 `518bb3f` 再补同 installation transition lock，关闭了旧并发竞态。

### [已关闭 P1] Manifest capability 没有约束嵌套 Host API（`34542eb`）

旧候选只在顶层 engine operation 检查 manifest capability，嵌套 Host API 可越权；`34542eb` 已按 capability 映射 `read_source/search/model` 并补单能力 Host.invoke 回归。

### [已关闭 P1] 直接 engine payload 尚未通过 Host 授权边界（`34542eb`）

旧候选 direct engine payload 只检查类型和 request id；`34542eb` 已将来源、memory ref、Head、Proposal、关联 request/event/watermark 纳入 Host/Core 授权边界，并以 payload20 + Host.invoke 集成回归覆盖。

## 首轮范围状态（2026-09-11）

以下范围已完成首轮核对，未发现新的阻断：

- `src/operant/plugins/registry.py` 的安装/认证撤销/epoch/Run lease、`src/operant/plugins/protocol.py` 的 stdio 双向 RPC、预算/取消/资源监测，以及 Host 资源 keep/delete/重启续做；`34542eb` 的目录句柄/在途收束和 `b34de8c` 的登记预算已完成定向复核。
- `src/operant/api_b2.py`、正式 SDK surface、`src/operant/application/service.py` 与 `src/operant/persistence/sqlite.py` 的 B2 Task/Agent/API 和 canonical Event+Item 原子提交；普通 Thread 作为上下文引用时保持只读。
- 终态优先投影与 cleanup、`clients/gui/` 的 GUI-L1 任务/审批/取消/断线/历史回读，真实 Tauri 的 Session 新轮和既有 Workflow Graph 安全恢复入口；这些范围沿用 `caa2ba8` 切片通过结论。

上述首轮范围和最后 Host 增量均已完成复核：生命周期锁、关闭排队回执和配置预算见下节；最终门禁、两种 Host 指纹和归档路径已核对。MP-2 记忆引擎、后台调度和真实插件模型/检索仍按任务包明确排除。

## caa2ba8 Session/GUI 增量结论（2026-09-11）

此切片已完成，结论为通过（仅针对已提交的 Session/B2/GUI 范围，不能外推到未交还的 Host worker）：

- `src/operant/api_b2.py:88-106,157-257,318-355` 从持久化 Agent/Session/WorkflowRun 和终态事件生成 B2 projection；无 Agent 的 `session.run_failed` 会进入失败 Task，后续新 Agent 以新轮时间覆盖旧失败；历史保留 typed Item 结构并逐字段脱敏，分页使用 canonical cursor。
- `src/operant/application/service.py:3270-3375` 对显式绑定当前 Session 的 Thread 写入 User Item，并在 setup/Factory 失败时以 Event+SystemItem 的同一持久化路径记录失败；普通 Thread 上下文引用不写 canonical history。`src/operant/persistence/sqlite.py:9790-9835` 的 `BEGIN IMMEDIATE` 与 Event+Item 同事务满足原子提交。
- `clients/gui/src/live/LiveContext.tsx:311-351,773-833,1184-1271,1351-1361` 通过正式 B2 Task.actions 决定取消可用性，Agent started/审批/终态触发权威 projection/history 刷新；错误、断线和 manual reconcile 不静默回退或猜测终态。
- `sdk/typescript-client/b2.generated.ts`、`sdk/python_client/b2_generated.py` 与 B2 Schema 的生成/确定性回归已核对；`client-review-increment.json` 的源文件 SHA-256 与 `caa2ba8` 对应，并记录了原生 Tauri Factory 失败→Task failed、同 Session 正式 `gpt-5.6-luna` 新轮结果 42→completed、审批等待取消→`agent.cancelled` 的闭环。上述真实证据不替代 Host worker 的最终集成门禁。

本切片没有新增阻断。B2 Task 页面仍按首屏 API 页读取，完整任务列表的更多页不是本次客户端实测范围；Host cleanup/inflight/fence、direct engine payload 和最终门禁已在本报告其他章节或归档证据中关闭/核对。

## 988fb24..518bb3f 最终 Host 增量复核

### 生命周期互斥与关闭排队

`src/operant/plugins/host.py:798-818,909-925` 为同一 installation 的 `stop()`/`uninstall()` 使用 per-installation transition lock；`resume_cleanup()` 在 `src/operant/plugins/host.py:1144-1193` 对每个 cleanup plan 复用同一锁。`close()` 在 `src/operant/plugins/host.py:1195-1217` 通过 shutdown lock 自身互斥，并在停止 engine 时取得对应 transition lock。Host 已关闭后，排队的 lifecycle 请求由 `_closed_lifecycle_receipt()` 返回明确的 `restart_required`/`ack=failed` 回执，而不是抛出旧的 `stale_epoch`。

独立定向运行：

```text
UV_CACHE_DIR=/private/tmp/operant-uv-cache uv run --no-sync pytest -ra \
  tests/test_plugin_lifecycle_concurrency.py \
  tests/test_plugin_config_enforcement.py
```

结果为 `15 passed`：9 个 stop/uninstall/close 两两并发组合和 6 个配置允许/拒绝组合均通过；没有残留 engine 或 active call。`verification.json` 的最终全门禁另记录 `847 passed、1 Docker 条件 skip`，本轮没有把 Docker skip 当作容器验收，也没有重复运行全套门禁。

另用最终代码的临时 Registry 构造 pending cleanup plan，在 `Host.close()` 后依次调用 `stop()`、`uninstall()` 和 `resume_cleanup()`，结果均为 `restart_required/ack=failed`（resume 返回 1 条同样回执），没有继续写 cleanup 状态。

### Binding 配置与零预算边界

`src/operant/plugins/host.py:514-528` 在 direct engine 入口拒绝关闭的 `maintenance`，并限制顶层 `RecallRequest.token_budget`；`src/operant/plugins/host.py:446-475` 将 binding 的 recall budget 传入 `RestrictedHostApi`，`src/operant/plugins/protocol.py:886-914` 对嵌套 `search` 重新执行同一上限。配置回归覆盖维护开关、recall 等于/超过上限、search 等于/超过上限，6 项均通过。

通用 Host 测试夹具的空 Recall 已在 `tests/test_plugin_host.py:107-124` 和 `tests/test_plugin_cleanup_lifecycle.py:92-114` 明确使用 `token_budget=0`，因此默认零预算只允许零分配请求；非零预算允许/拒绝由配置回归单独覆盖，没有把夹具的上下文配额误当成 MP-2 召回实现。

### 最终证据核对

`docs/design/b2-2/verification.json:2,124-129` 的 `code_head` 与代码候选 `518bb3f` 一致；当前 Git `HEAD` 为 `b05f756`，其相对 `518bb3f` 仅归档文档变化。`host-isolation-smoke.json` 和 `host-inprocess-smoke.json` 中 Host/protocol/registry/payload 四个 SHA-256 均与当前工作树逐项匹配。实际 Host 证据仍明确限定为已知临时 fixture 的 macOS sandbox-exec RPC 和认证安装入口，不宣称 MP-2 记忆检索或模型验收；正式客户端指纹沿用已通过的 `caa2ba8` 切片。

## 当前阻断

本轮没有发现新的代码阻断。此前的同 installation lifecycle P1 已由 `518bb3f` 的锁和 9 组合回归关闭；配置强制和零预算边界也已独立核对。交付文档中的 `verification.json:131-136` 仍保留旧的 `pending_workspace_credits`/remaining 文案，负责人应在最终汇总时改为本次 Reviewer 已完成；这是文档状态同步项，不是当前源码阻断。
