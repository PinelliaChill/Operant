# H-01/H-02 子任务与定向消息可靠性验收

> 2026-10-02；记录身份：Codex 可靠性执行 Agent。隔离工作树 `codex/beta-reliability-task2`，基线 `600d97c`。本节只记录 H-01/H-02 的本批修复和证据，不替代任务 2 总验收或任务 6 组合验收。

| 要求 | 输入与操作 | 预期及证据 | 本批结果 |
| --- | --- | --- | --- |
| H-01 取消传播 | 两个 Core 实例共用隔离 SQLite；原 owner 的子模型请求停在确定性 Provider；另一 Core 调用 `cancel_tree` | 原 owner 持久写入 `agent.cancelled`，租约释放，子任务状态保持 `cancelled`；`tests/test_workbench_agents.py::test_other_core_keeps_live_child_owner_and_cancels_it` | 通过 |
| H-01 取消与完成竞争 | 在 `agent.completed` 入账前和入账后分别请求取消，另覆盖父 Thread | 同一 SQLite 写序裁决终态：取消先入账则只有 `agent.cancelled`，完成先入账则保留子任务 completed；Agent、Thread、子任务和租约一致；`test_cancel_wins_before_agent_completion_is_committed`、`test_cancel_after_committed_completion_preserves_completed_child`、`test_parent_cancel_before_completion_is_committed` | 通过 |
| H-02 消息去重 | 投递同一发送者幂等键；取消收件 Thread 后重试同键，再以新键尝试 | 同键返回原 message ID 且消息总数不变；新键拒绝；`test_message_retry_returns_original_after_threads_cancelled` | 通过 |
| H-02 并发唤醒 | 两个 Core 对同一已完成子任务和待消费私信同时调用 `wake` | 仅一个预占 Session 租约和持久 wake 计数，模型只补跑一轮，消息消费一次；`test_two_cores_reserve_only_one_private_message_wake` | 通过 |
| H-01/H-02 已提交终态对账 | 模型结果已入 Session 事件账本但子任务投影仍显示运行中，模拟写投影前崩溃 | 重启只根据同一 Agent 的终态事件修复投影；未知结果不重放；`test_committed_child_result_is_reconciled_after_worker_exit` | 通过 |
| H-01/H-02 模型中断 | 正式 workbench HTTP 创建子任务；独立 Core 进程停在模型请求期间后被强杀，另一 Core 同库重启 | 子任务变 `interrupted/manual_reconcile`，新进程不重发模型请求；`test_workbench_recovery_process.py[model]` | 通过，确定性 Provider |
| H-02 工具未知结果 | 正式模型工具链投递私信，消息已提交但工具回执未完成时强杀 Core 并重启 | 消息只有一条；工具回执阻止新 Session Run 自动重放，要求人工对账；`test_workbench_recovery_process.py[tool]` | 通过，确定性 Provider |
| H-01 审批等待 | 子任务请求 `git add`，持久审批为 pending 时强杀 Core 并重启 | 审批仍 pending，Git 暂存为空；新 Run 被持久审批栅栏阻止，不重复执行；`test_workbench_recovery_process.py[approval]` | 通过，确定性 Provider |

可复跑命令（从本工作树运行）：

```bash
UV_CACHE_DIR=/private/tmp/operant-beta2-uv-cache PYTHONPATH=src \
  uv run --no-sync pytest -q tests/test_workbench_agents.py \
  tests/test_workbench_recovery_process.py
```

最终合并定向结果为 **25 passed**，进程测试实际使用临时回环 Core、正式 workbench HTTP、独立 SQLite、SIGKILL 与同库重启。持久证据见治理根 `.operant/beta-task2/evidence/workbench-recovery/pytest.log` 和 `pytest-tmp/` 下每个场景的 `core.sqlite3`、`worker.log`、`restart.log`。同一源码的 `ruff format --check .`、`ruff check .`、`mypy src` 与 `git diff --check` 通过。确定性测试没有调用真实模型，也没有接触真实用户库。

真实 Provider 的 H-01 取消/重启补验脚本为 [`acceptance_reliability_real.py`](acceptance_reliability_real.py)。主线程在已发现的精确模型 ID 与已授权合成任务环境下注入 `OPERANT_BASE_URL`、`OPERANT_API_KEY`、`OPERANT_BETA2_MODEL_ID`，再运行：

```bash
UV_CACHE_DIR=/private/tmp/operant-beta2-uv-cache PYTHONPATH=src \
  uv run --no-sync python docs/design/beta-reliability/acceptance_reliability_real.py \
  --evidence-dir /absolute/isolated/evidence/real-child-cancel
```

脚本先通过正式 Provider Discovery 核对精确模型 ID，再以无工具权限、最多 1 轮和 1024 输出 token 的冻结 Profile 创建一个合成子任务。验收 Core 在正式 Provider 的首个**非空真实** `model.delta` 原样持久后暂停继续读取；HTTP 观察到该事件才取消。取消时关闭底层流，不制造或改写模型响应。这是固定中断点的受控真实调用，不代表自然网络时序的全面验收。脚本等待 owner 写入 `agent.cancelled` 并释放租约，再停启 Core 同库核对唯一子任务、取消终态及事件数不增长。它保存 SQLite、Core 日志和不含凭据的 `result.json`，并保证结束服务。若 Provider 未产生可观察的 delta 或不可用，保持未验收并报告实际原因。

首次无门闩真实尝试保留在治理根 `.operant/beta-task2/evidence/reliability-real/`：该模型的首个 delta 后约 68 毫秒已自然完成，HTTP 取消落在模型完成与终态投影之间，暴露并促成上表的终态竞争修复；不能算模型执行中取消通过。随后 `.operant/beta-task2/evidence/reliability-real-final/` 在仅有 `agent.started`、无 delta 和取消请求时执行进程中断，未生成结果，属于**未验收**而非产品失败。复验须使用新的独立目录，保留这两次历史证据。

本批 H-01/H-02 的确定性可靠性场景通过；真实模型取消/重启、日常多 Agent 组合与桌面入口仍由主线程补验，不能从确定性 Provider 结果外推。
