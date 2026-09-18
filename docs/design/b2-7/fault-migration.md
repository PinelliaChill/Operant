# B2-7 故障与迁移证据

记录身份：Codex（fault_migration）；适用对象：B2-7 执行者与 Reviewer。

本批只补 MP-6.1/6.3 的确定性缺口，并提供一条隔离迁移演练入口。所有测试和演练都使用 pytest 临时目录或显式新建的 `--run-root`，没有打开配置数据库、真实用户库、`.env` 或凭据。

## 证据边界

| 场景 | 已有有效证据 | 本批补充 | 证据性质与限制 |
| --- | --- | --- | --- |
| 冷启动/恢复 | `test_registry_restart_does_not_auto_instantiate_or_reinstall`、`test_notebook_reinstall_rebuilds_exact_recall_after_host_restart` | `test_b27_registry_restart_expires_old_lease_and_requires_new_run` | 确定性 Registry/正式 Core 链路；没有真实桌面冷启动 |
| 并发/隔离影响 | `test_inprocess_concurrency_is_admitted_before_plugin_execution`、`test_concurrent_lifecycle_requests_have_stable_results`、SQLite v11 并发初始化 | `test_b27_safe_restart_fences_one_installation_but_other_run_continues` | 确定性 asyncio/SQLite；每个插件安装独立 binding，未宣称跨进程真实压力结果 |
| 插件崩溃 | stubborn in-process、stdio deadline/resource tests | `test_b27_isolated_worker_crash_requires_explicit_respawn` | 真实子进程 + runner shim；不是 sandbox-exec 隔离证据 |
| 认证撤销/停用 | `test_inflight_result_is_rejected_after_certification_revocation`、callback/payload authorization、disable epoch tests | — | 确定性 Host/Registry；未宣称外部签发服务验收 |
| keep/delete/清理 | Host inventory、cleanup hook、目录竞态、B2-3 Core ledger/namespace keep/delete 与无包数据删除 | — | 确定性正式 Repository/Host；不等同磁盘安全擦除 |
| 旧 Run/旧包恢复 | Registry crash fencing、Workflow checkpoint/manual reconcile | `test_b27_missing_package_rejects_original_run_recovery` | 旧 lease 过期，缺包返回 `package_unavailable`；没有自动重放 |
| v18 升级/回退 | v14→v18 历史升级、v11/v13/v14 受限回退 | `test_b27_versioned_packages_coexist_and_replace_after_keep`、`test_b27_v18_upgrade_failure_is_atomic`、`test_b27_v18_rollback_requires_isolated_empty_b2_6_database` | 合成插件版本与 SQLite；安装实例目录固定，活动旧 Run 受保护，keep 后可同 dataset 替换；空新增表可回退，有 B2-6 证据则拒绝 |
| 真实隔离进程 | — | `scripts/b27_migration_drill.py` | 仅在真实 macOS `sandbox-exec` 探针通过时记录 `passed`；shim/Mock 结果不会升级为隔离验收 |

## 新增确定性结果

命令：

```bash
./.venv/bin/python -m pytest tests/test_b27_fault_migration.py
```

结果：`7 passed`。

相关插件和迁移回归（`tests/test_b27_fault_migration.py`、`tests/test_plugin*.py`、`tests/test_*migration.py`）在加入版本切换用例后运行结果为 `137 passed`（74.40 秒）。

新增用例验证以下边界：

1. Registry 重启把崩溃遗留 lease 标记为 `expired`、提升 binding epoch；旧 lease 不能调用，显式新 Run 可以继续。
2. 旧 lease 对应的 package 缺失时，Host 返回 `package_unavailable`，不自动重装或伪造恢复成功。
3. 一个不合作的进程内插件触发 `restart_required` 时，另一安装上的活动 Run 仍能完成；释放旧调用后再安全关闭 Host。
4. stdio worker 首次调用进程退出时请求失败，下一次调用显式拉起新 worker 并完成 typed lifecycle 响应。
5. v18 升级在注入故障后事务回到 v17，B2-6 表和注入表均不残留。
6. v18 只能在 `isolated=True` 且 B2-6 新表为空时回退；有经验证据时保持 v18 并返回 `MigrationError`。
7. 相同 plugin id 的 v1/v2 安装实例可共存；活动旧 Run 阻断旧包卸载，旧 Run 结束并 `keep` 后，保留 dataset 可显式安装/选择 v2。

## 隔离迁移演练

入口：`scripts/b27_migration_drill.py`。

```bash
./.venv/bin/python scripts/b27_migration_drill.py \
  --run-root /绝对路径/新建且不存在的/b27-run \
  --output /绝对路径/b27-run/evidence.json
```

脚本会：

- 用 `SQLiteStore.migrate(target_version=14)` 创建 `legacy-v14.sqlite3`，写入合成工作区、历史和两版项目记忆；
- 复制为 `upgrade-v18.sqlite3` 后升级到 v18，输出迁移前后计数和 canonical rows hash；
- 在独立空库演练 v18→v17 回退，并在另一独立库写入 `b26_commands` 后确认回退被阻断；
- 通过真实 `SandboxProbe`/`/usr/bin/sandbox-exec` 尝试启动最小 stdio worker；探针失败时输出 `blocked`，绝不退回普通子进程冒充隔离。

早期脚本入口的一次未提升权限运行（`evidence_head=5b7833aa`）因外层执行沙箱阻断真实探针；随后提升权限重跑得到 run6 结果。该 run6 记录保留为历史诊断，不作为最终固定源的唯一证据：

- `migration.status=passed`，来源 v14、目标 v18，`workspace_initializations=2`、`threads=1`、`items=1`、`memories=1`、`memory_versions=2` 在迁移前后相同；canonical hash 前后相同。
- 提升权限运行的历史合成升级库：`/private/tmp/operant-b27-drill-qeo02X/run6/upgrade-v18.sqlite3`。
- 对应历史合成旧库：`/private/tmp/operant-b27-drill-qeo02X/run6/legacy-v14.sqlite3`。
- 空新增表回退：`passed`；有 B2-6 经验证据回退：`blocked`，回退尝试后 schema 仍为 v18。
- 提升权限运行的 `real_isolation.status=passed`：真实 `/usr/bin/sandbox-exec`、`mode=isolated`、typed lifecycle `ready`，并在 Host 托管 state 目录观察到 `real-isolated-worker-ran`。未提升权限的同一运行入口为 `blocked`，原因是外层 `sandbox_apply: Operation not permitted`（退出码 71）；该结果不覆盖提升权限后的真实 OS 证据。

上述 run6 输出不包含模型配置或凭据；它是早期脚本演练产物，保留用于解释外层沙箱阻断与真实 `sandbox-exec` 的差异。

最终固定源证据见 [migration-final.json](migration-final.json)：在 `002f98c` 固定源上，v14 合成库升级到 v18，迁移前后 canonical rows 的计数和 hash 保持一致；空库 v18→v17 回退通过，含 B2-6 证据的库回退被阻断且保持 v18；同一文件还记录了提升权限后的真实 `/usr/bin/sandbox-exec`、typed lifecycle `ready` 与托管 state 写入。最终合成升级库为 `/private/tmp/operant-b27-migration-final/upgrade-v18.sqlite3`，旧库为 `/private/tmp/operant-b27-migration-final/legacy-v14.sqlite3`。

随后 [native-upgrade.json](native-upgrade.json) 在 `2bf45c4` 实际启动 release Tauri WebView/Core，使用同一 v14 合成副本升级到 v18，原有 workspace、thread、item、memory、memory version rows 均标记为保留；该记录同时验证显式迁移提示、隔离 notebook 安装/绑定和迁移后的查询边界。源差核对以产品冻结 `b932e30` 为准：`002f98c..b932e30` 的产品源码变动仅为 `src/operant/api.py`、`api_b2_5.py`、`api_b2_6.py`、`application/service.py`、`memory_plugins/manager.py`；migration、registry、host 源码未变。脚本、fixture 和定向测试的变化单独计入评测证据，不把它们写成迁移实现变化。

最终产品侧补充证据见 [native-projection-final.json](native-projection-final.json) 与 [tui-projection-final.json](tui-projection-final.json)：产品冻结 `b932e30` 上 native 16-record typed receipt/exact review 和 TUI 四步均完成。它们补强最终产品投影/客户端回执边界，不改变本文件对迁移回退范围的限制。

## 未覆盖与产品缺口

- `PluginRegistry` 没有一个跨 package、binding、dataset 和 Run 的原子升级命令；本批已验证显式安装 v1/v2、固定 installation 目录、活动旧 Run 保护，以及旧安装 `keep` 后同 dataset 的 v2 替换。升级编排仍需由上层按这些阶段提交，不能把中途失败伪报为完成。
- 当前不新增公共契约：现有显式 install/bind/enable/Run lease/keep 顺序足以验收本批“旧 Run 固定旧包、新 Run 选择新包”的受限语义。若后续需要单请求原子升级，最小增量应是带 `old_installation_id`、`new_installation_id`、`dataset_id`、`expected_binding_epoch` 和 `idempotency_key` 的 Core 编排命令，并持久化 prepare/quiesce/cutover/blocked 状态；不能让客户端自行拼接为第二套权威。
- 本故障/迁移演练未执行真实 Provider、生产 Remote Target 或 Docker 容器验收；真实 Tauri 升级、native projection 和 TUI 四步已有上述独立证据，不能互相替代。
- 未提升权限的执行环境会拒绝 `sandbox-exec`，但提升权限运行已获得一次真实隔离进程证据；该证据只覆盖本机 macOS、当前解释器和最小合成 worker，不能外推到其他平台或生产插件。
