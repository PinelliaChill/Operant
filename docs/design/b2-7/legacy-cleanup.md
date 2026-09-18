# B2-7 / MP-6.4 旧旁路收敛证据

记录身份：Codex（legacy_cleanup）；适用对象：B2-7 执行者与 Reviewer。

本条记录对应 B2-7 worktree `codex/b2-7-acceptance-candidate`，基线
`5b7833aa0a9c3d7c1cddc44689654d2c1509297c`。工作树同时包含 root 的候选打包和故障/迁移改动，以下结果只说明本条旧旁路改动及其受影响测试；冻结集成 SHA 后需重跑完整门禁。

## 收敛结果

- `ApplicationService.save_memory/create_memory/update_memory/confirm_memory/activate_memory/deactivate_memory` 保留旧签名以便识别旧客户端，但统一返回 `schema_upgrade_required`，不能通过任何开关回到 Core 写入。
- `ApplicationService.query_memories` 只使用已选 `MemoryManager` 的兼容查询；没有插件时明确报告不可用。显式旧 `get_memory` 和版本读取仍保留只读兼容，供历史上下文解释与迁移使用。
- `SequentialCodingWorkflow` 删除旧 `_memory_context`、`_knowledge_candidate_events` 和 `_verified_commands`，不再自动召回、写入候选或通过关键词/命令结果晋级。旧 `persist_memory_candidates=True` 请求明确返回升级错误，默认值改为关闭。
- Runtime 按已注入/选中的 Manager 判定插件链路，`memory_enabled=False` 时不注入 Skill 内容；不再读取 `memory_plugin_mode`。API/CLI 的旧模式赋值已删除。
- CLI 的旧 `memory add/confirm/deactivate` 命令保留入口但明确退出并提示 B2-3 Proposal/CAS；`allow_conservative_activation` 失效选项已移除。旧 `memory search` 无插件时明确提示使用 `memory manage`。
- 评测安全夹具改为测试内 `store.create_memory(Memory(...))`，并用仅限该测试的窄查询适配器保留隔离、敏感文件过滤、Memory 指标和成本断言；没有把旧 Service 写入口带回产品。

## 调用面扫描

扫描命令：

```bash
rg -n --glob '*.py' \
  'save_memory\(|create_memory\(|update_memory\(|confirm_memory\(|activate_memory\(|deactivate_memory\(|query_memories\(|persist_memory_candidates\s*=|memory_plugin_mode' \
  src tests
```

结果：

- `memory_plugin_mode` 在 `src` 和 `tests` 中无剩余引用。
- `persist_memory_candidates` 只剩 Workflow 的兼容拒绝分支、一个 `False` 评测调用和对应回归测试；没有写入实现。
- 生产旧写接触点只剩 API 对旧 `/v1/memories` 路由的兼容包装，最终由 Service 统一返回升级错误；CLI 旧写命令同样显式拒绝。
- `store.create_memory/update_memory` 出现在迁移、持久化和上下文只读历史夹具中，属于明确的测试/迁移输入，不是应用自动写入。
- `application/evaluation.py` 的评测入口仍由 root 负责后续正式插件/真实对照接入；当前 Service 查询已不再回退到 Core FTS，旧评测夹具已隔离。

## 定向测试

以下命令均在 B2-7 `.venv` 中执行，避免 root 更新 `pyproject.toml` 后 `uv run` 离线重新解析 `hatchling`：

```bash
./.venv/bin/python -m pytest tests/test_memory.py tests/test_workflow_memory.py tests/test_evaluation_runner.py -q
```

结果：`12 passed`。

```bash
./.venv/bin/python -m pytest tests/test_cli_week3.py -q
```

结果：`3 passed`；覆盖旧写命令升级拒绝、插件缺失搜索提示和已移除选项。

```bash
./.venv/bin/python -m pytest tests/test_api.py tests/test_b23_session_context.py tests/test_b24_memory_runtime.py tests/test_b26_runtime.py -q
```

结果：`20 passed`；仅有既有 Starlette/httpx 弃用警告。

```bash
./.venv/bin/python -m pytest tests/test_b23_legacy_acceptance.py tests/test_b23_management.py tests/test_b26_writer_integration.py -q
```

结果：`21 passed`；仅有既有 Starlette/httpx 弃用警告。

```bash
./.venv/bin/python -m pytest tests/test_service_workflow.py tests/test_phase1b_context.py tests/test_evaluation_workflow.py -q
```

结果：`42 passed`；仅有既有 Starlette/httpx 弃用警告。

```bash
./.venv/bin/ruff check \
  src/operant/api.py src/operant/cli.py \
  src/operant/application/service.py src/operant/application/workflow.py \
  tests/test_evaluation_runner.py tests/test_memory.py tests/test_workflow_memory.py \
  tests/test_b24_memory_runtime.py tests/test_b26_runtime.py tests/test_b26_writer_integration.py
./.venv/bin/mypy \
  src/operant/api.py src/operant/cli.py \
  src/operant/application/service.py src/operant/application/workflow.py
git -c core.fsmonitor=false diff --check
```

结果：Ruff、mypy、diff check 均通过。

CLI 入口检查：

- `memory add --kind project --content legacy --session-id session_test`：退出码 2，提示旧版写入已下线并转 B2-3 Proposal/CAS。
- `memory confirm memory_legacy --session-id session_test`：退出码 2，提示使用 `memory_confirm`。
- `memory deactivate memory_legacy --session-id session_test`：退出码 2，提示使用 `memory_deactivate`。
- `memory add --help`：不再展示 `--allow-conservative-activation`，所有保留旧参数均标明本命令会拒绝写入。

限制：以上是确定性本地测试和兼容边界证据，不代表真实 Provider、完整桌面壳、生产 Target 或候选包最终验收；冻结集成版本后由 root 重跑完整 B2-7 门禁。
