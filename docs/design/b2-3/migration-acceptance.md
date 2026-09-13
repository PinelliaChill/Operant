# AC-03 合成 v14 → v15 迁移验收

记录身份：Codex；日期：2026-09-13。实施 worktree：`/private/tmp/operant-b2-3`。

本次只在 [`run-1340a647587d48a2b6da4c5f1004f00d/legacy-v14.sqlite3`](/private/tmp/b23-migration-acceptance/run-1340a647587d48a2b6da4c5f1004f00d/legacy-v14.sqlite3) 创建合成数据库，不读取或写入任何用户库。测试先通过现有 `SQLiteStore.migrate(target_version=14)` 建立 v14，再写入两个旧 Memory 记录（目标项目同一记录的版本 1、2，另一个项目的版本 1、2）和两条 canonical Item。随后调用现有 `SQLiteStore.initialize()` 升级到 v15，并保留原 Core 记忆与历史 Item。

定向命令：

```text
PYTHONPATH=src /private/tmp/operant-b2-2/.venv/bin/python -m pytest -q tests/test_b23_legacy_acceptance.py -o addopts=''
```

已通过的阶段：

- 合成数据库 v14 建库及旧数据写入；目标项目有 2 个 `memory_versions` 版本，历史有 2 条 Item。
- v14 → v15 连续迁移，`schema_version() == 15`，`schema_migrations` 为 1–15。
- 迁移前后 Core `memory_versions` 数量与 canonical Item 数量保持不变。
- 正式 `memory_migrate` 仅导入目标项目的 2 个历史版本；新 dataset 有 1 个 head、2 个 legacy candidate 版本，均为 `legacy_unverified`，无已发布 head，owner namespace 与 dataset 精确一致。
- 旧 `/v1/memories` 写入返回 HTTP 400 `schema_upgrade_required`；v15 数据库无隔离授权时拒绝降级到 v14。

首次运行暴露的 Manager 查询问题已由负责人修正。原实现 `src/operant/memory_plugins/manager.py:536` 执行：

```sql
SELECT * FROM memories WHERE project_scope = ?
```

现有 v14 schema 的 `project_scope` 位于 `memory_versions`，父表 `memories` 只有 `id/current_version/created_at`，首次运行实际错误为：

```text
sqlite3.OperationalError: no such column: project_scope
```

修正后 Manager 通过 `memory_versions` 与 `memories` 的显式 join，按注册 workspace scope 过滤并保留历史版本；本次定向命令结果为 `1 passed`（1 个 Starlette deprecation warning）。

机器可读记录见 [`migration-acceptance.json`](migration-acceptance.json)。本记录只覆盖合成库迁移门，不外推到真实用户库迁移。
