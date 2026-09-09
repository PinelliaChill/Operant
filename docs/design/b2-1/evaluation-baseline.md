# B2-1 MP-0.5 记忆评测基线

状态：已准备合成基线与可复跑脚本；这是旧 Memory 行为/性能记录，不是新插件、Host 或新 FTS 的实现验收。

本报告和脚本在 A 实施 worktree `/private/tmp/operant-b2-1-a` 上核对；固定基线 HEAD 为 `ecb00437e9a44a5e79d54e8cf4944fd0d456bf02`，本次集成运行 HEAD 为 `1e0cfc749125de98c85fe15001f2bf72811361f8`。脚本报告会写入实际 `worktree`、运行时 `source_root`、固定 `base_head`、运行时 Git `head`、fixture 绝对路径和 `--output` 绝对路径；这些路径字段用于确认没有读写治理根旧副本。

## 数据和边界

固定 fixture 位于 [`tests/fixtures/b2_1/memory_baseline.json`](../../../tests/fixtures/b2_1/memory_baseline.json)，包含 31 条合成 Memory 和 42 个明确案例：开发集 30 个、保留集 12 个。开发集使用 `workspace-alpha`/`workspace-beta` 的 2026-01 来源；保留集使用独立的 `workspace-gamma`、`source-gamma-*` 和 2026-02 时间段，不复用开发集事实。案例覆盖中文改写、代码标识符、分支新旧事实、恶意 Memory、私有 Mailbox、无相关记忆、撤销、压缩污染、迟到事件、跨项目边界和跨任务学习。运行前会校验 fixture schema、ID 引用、项目/时间拆分、每案例完整 forbidden 集合和 SHA-256；运行时不随机抽样。

其中 `m_alpha_revoked`、`m_alpha_compression` 和 candidate 条目是 fixture 直接以 `inactive`/`candidate` 状态写入的过滤样本；它们没有经过真实的撤销、压缩、epoch、队列或清理生命周期。因此本基线只能证明当前查询不会返回这些标记为不可用的行，不能宣称撤销传播、压缩污染隔离或卸载删除已经验收。恶意 Memory 同项目返回也只是无模型下的原始数据边界观察，不是提示注入防护验收。

所有有记忆策略写入独立创建的临时 SQLite；`no_memory` 对照直接返回空列表，不构造 Service、不打开 SQLite、不调用检索。脚本不读取用户库、`.env` 或凭据，不访问网络，不调用模型；`model_calls=0`、`model_usage=unknown` 和模型成本保持 `unknown` 是有意记录的结果。`no_memory` 是确定性检索控制，不是端到端真实模型无记忆对照。

## 对照路径

[`scripts/benchmark_memory_baseline.py`](../../../scripts/benchmark_memory_baseline.py) 只调用现有 Service：

- `no_memory` 直接返回 `[]`，记录零检索调用，用于建立无记忆质量/成本下界。
- `old_direct_query` 调用 `ApplicationService.query_memories(case.query, ...)`，落到当前 `SQLiteStore.search_memories()`/已有 FTS5，记录案例返回 ID、相关性和安全边界。
- `old_recent_entries` 调用同一入口但使用空查询 `query=""`，对应当前 `SequentialCodingWorkflow._memory_context()` 的最近项目条目策略（`limit=5`）。它用于记录“最近条目”对任务相关性的代价，不代表新召回算法。

脚本没有实现 Host、插件 RPC、向量索引或新的 FTS；同一 fixture 和权限快照分别运行三条基线。每条样本记录 wall time、进程 CPU 和 Python 峰值分配；报告额外记录 fixture load、bootstrap、首查询 `first_query_ms`、warm p50/p95、案例结果和 `service_query_calls`。`bootstrap_ms` 包含 Service 构建、临时 SQLite 初始化/迁移、角色/会话和 fixture 写入，不包含进程启动；`ru_maxrss` 只记录整次顺序运行的进程 high-water，策略间不可比较。

## 冻结门槛

安全门是硬门：每个策略、每个案例的 `forbidden_hits` 必须为零。禁止返回跨项目、角色受限、candidate、inactive/已撤销或压缩污染条目；出现一例就阻断后续候选交付。

非安全性能门分开冻结：任务检索候选若复用同一直接算法，以 `old_direct_query` 为参考，warm wall/CPU p95 回归上限为 1.25 倍，`first_query_ms` wall 上限为 1.50 倍，warm 峰值分配 p95 为 1.50 倍，绝对余量为 2 ms/256 KiB；这些门不把 `old_direct_query` 与 `old_recent_entries` 的任务质量差异混成 Host 开销。`first_query_ms` 是已完成 bootstrap 后的首个查询，不代表进程或 Service 冷启动。未来 Host 必须使用同一任务检索算法再单独比较启动和查询：可信进程内模式的 bootstrap wall 上限为 1.50 倍、首查询 wall 为 1.50 倍、CPU 为 1.25 倍、峰值分配为 1.50 倍；隔离模式的 bootstrap wall 上限为 3.00 倍、首查询 wall 为 3.00 倍、CPU 为 2.00 倍、峰值分配为 2.00 倍。未来 Host startup 必须独立测量，不能拿 `first_query_ms` 冒充。当前报告将任务门标为 `baseline_characterization_only`、Host 门标为 `frozen_pending_future_host`，因为没有候选 Host 可比较，不能把基线本身写成“通过”。性能值受同机负载、Python/SQLite 版本影响，比较时应固定运行环境。

## 运行和结果

在仓库根目录运行：

```bash
cd /private/tmp/operant-b2-1-a
uv run --offline python scripts/benchmark_memory_baseline.py --repetitions 3 \
  --output docs/design/b2-1/evaluation-results.json
```

不传 `--output` 时报告输出到 stdout。`--repetitions` 至少为 1；默认 3。报告包含 fixture canonical/file hash、开发/保留集质量摘要、每个策略的直接结果/成本、`fixture_load_ms`、`bootstrap_ms`、`first_query_ms`、`warm_wall_p50_ms`、`warm_wall_p95_ms`、CPU p50/p95、峰值分配 p50/p95 和带不可比范围说明的 RSS high-water。

2026-09-09 在 `/private/tmp/operant-b2-1-a@1e0cfc749125de98c85fe15001f2bf72811361f8` 用 `uv run --offline`、`--repetitions 3` 实测，fixture 规范化 JSON SHA-256 为 `6e250c0a28de45880ec43a064cba3dbaf8d2ddc1b86a84700beca86172481f51`，fixture 文件 SHA-256 为 `d4ab1378850a4dbc0691a0cb12f79e3cbb9ec5af225887e849889ad1e3858895`；三条路径安全硬门均通过（0 forbidden hit）。原始结果见 [`evaluation-results.json`](evaluation-results.json)，文件 SHA-256 为 `85e895e5b83be582da7cbb788aef2ca16b4bb1cb66fbdbad0fb9e8f459504f25`：

本次实际读入 `/private/tmp/operant-b2-1-a/tests/fixtures/b2_1/memory_baseline.json`，输出 `/private/tmp/operant-b2-1-a/docs/design/b2-1/evaluation-results.json`。报告中的 `implementation.worktree`、`implementation.source_root`、`implementation.base_head` 和 `implementation.head` 均回读为目标 A worktree、固定基线和运行 HEAD。

| 策略 | 开发集 macro P/R | 保留集 macro P/R | bootstrap / first-query ms | warm wall p50/p95 ms | warm CPU p95 ms | warm 峰值分配 p95 KiB |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `no_memory` | 0.2667 / 0.2667 | 0.4167 / 0.4167 | 0.0001 / 0.0013 | 0.0005 / 0.0005 | 0.0050 | 0.3594 |
| `old_direct_query` | 0.7000 / 0.7000 | 0.7500 / 0.7500 | 673.7747 / 3.2377 | 2.5832 / 3.2582 | 2.9900 | 7.9902 |
| `old_recent_entries` | 0.0889 / 0.2944 | 0.0833 / 0.3750 | 658.9330 / 2.7521 | 2.5676 / 2.8272 | 2.7150 | 23.1211 |

本次基线结果以 raw JSON 为准：`no_memory` 返回 0 条且零 Service query call；有记忆策略各执行 126 次 Service query call，实际 SQLite SQL 次数保持 `unknown`。bootstrap 值包含 Service/SQLite/fixture 写入初始化，不是进程启动；`first_query_ms` 是 bootstrap 后首个查询。RSS 值是整次顺序运行的进程 high-water，不能用于策略间比较。以上结果没有证明任何新算法收益。模型调用为 0，Provider usage、Token 和模型成本为 `unknown`。wall/CPU 数字是当前机器的观测值，复跑时会随负载变化。

本地确定性检查：

```bash
uv run pytest tests/test_b2_1_benchmark.py
```

测试确认固定拆分和覆盖范围、临时库/零模型调用/unknown usage、三条基线的测量字段，以及安全硬门和性能容忍值。耗时数字本身不作跨机器断言。

## 待后续补测

以下项目在 B2-1 明确保持 pending：未来 Host 可信进程内模式、未来 Host 隔离模式、新任务 FTS、真实模型 usage/Token/端到端成本，以及生产用户数据。`no_memory` 仅是已完成的确定性检索控制，不能替代真实模型无记忆端到端对照。Host 两模式需要对应契约、实现和正式入口就绪后，使用同一直接算法、固定权限/预算和隔离数据集另行测量；本基线不会读取或回写真实知识。

本报告在已提交的 `1e0cfc749125de98c85fe15001f2bf72811361f8` 上重新生成，所记录 HEAD 含实际指标脚本；没有用未提交的脚本冒充该 HEAD。
