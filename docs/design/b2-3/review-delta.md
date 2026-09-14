## 增量初审结论

初审 5 项修复已由当前代码闭环确认；但仍有 1 项 P1、4 项 P2，暂不能通过 Reviewer。

### 未解决风险

#### P1-01：停用记忆仍可被插件候选引用

位置：

- `src/operant/memory_plugins/manager.py:945-958`
- `src/operant/memory_plugins/manager.py:163-175`
- `src/operant/memory_plugins/manager.py:1030-1035`

`authorize_ref()` 只排除 `deleted/revoked`，仍接受 `inactive` 的当前 published ref。`memory_search` 随后按 `record_id` 取 `_records()`，而 `_records()` 明确包含 inactive 记录。

复现：

1. 发布一条记忆；
2. 执行 `memory_deactivate`；
3. 让插件返回该旧 ref；
4. Host/Core 授权仍成功，管理查询返回 inactive 记录。

这不是重复报告 notebook 索引修复，而是 Core 通用受控读取仍未禁止 inactive ref。

建议：Recall/Host ref 授权必须要求 `head.state == "published"`；管理投影另走显式 `include_inactive` 路径，并按完整 ref 重新过滤。

#### P2-01：Proposal 的 owner/source 未绑定到 Core 真实版本

位置：

- `src/operant/plugins/payload.py:328-342`
- `src/operant/memory_plugins/manager.py:972-978`
- `src/operant/memory_plugins/ledger.py:1136-1158`

当前只检查 dataset、head、scope 和各 source 是否“可授权”，没有检查：

- `proposal.owner.principal_id` 是否等于安装记录 owner；
- `proposal.source_refs` 是否等于目标 `MemoryVersion.sources`；
- 是否至少包含本次 Core 输入 source。

复现：插件返回相同 dataset/head/target、但 `source_refs=()` 或伪造 principal 的 Proposal；当前 Host、Manager、Ledger 均可接受并持久化。

建议：Core 重建 owner；要求 proposal source refs 与目标版本/Core 输入严格匹配，Ledger 再做防御性校验。

#### P2-02：新记录 `save_version()` 未检查 expected CAS

位置：

- `src/operant/memory_plugins/ledger.py:795-813`
- CAS 检查仅出现在 `src/operant/memory_plugins/ledger.py:814-819`

复现：对从未存在的 `record_id` 调用：

```python
ledger.save_version(version_v1, expected_head_revision=99)
```

当前仍会创建 revision 0 的 head 和 version，而不是冲突。正常 Manager 新建路径传入 0，因此主要影响公共 Ledger API/异常调用边界。

建议：新 head 按实际 revision 0 执行 `_check_expected(expected_head_revision, 0)`。

#### P2-03：`project_detach` 同时把项目归档

位置：

- `src/operant/memory_plugins/manager.py:434-442`
- 归档后禁止记忆操作：`src/operant/memory_plugins/manager.py:134-145`

复现：对活跃项目执行 `project_detach` 后，projection 为：

```text
archived=true
installation_id=null
```

且没有 unarchive 命令；项目不能恢复为活跃项目再重新绑定插件。GUI 文案却声明“只移除项目绑定”。

建议：detach 只清除 `installation_id`，保持 `archived` 原值；archive 单独负责生命周期归档。

#### P2-04：`effective_at` 不是各设置的实际生效时间

位置：

- `src/operant/memory_plugins/manager.py:109-115`
- `src/operant/memory_plugins/manager.py:261-314`

每次 Manager `_save()` 都更新同一个全局 `effective_at`，随后所有 global/project/binding/role 设置共用该时间。

复现：记录设置时间后执行无关的 Skill 安装或项目改名，未变化的设置也会显示新时间。

建议：为各设置持久化独立 effective timestamp；Role 使用实际 Role version 时间，而不是 Manager 状态保存时间。

### 初审 5 项已关闭

- 全局关闭：已收集并检查 Host stop receipt；`manager.py:499-519`，回归 `tests/test_b23_management.py:221-234`。
- Ledger 完整幂等摘要：已覆盖 CAS、permission epoch、Proposal 字段；`ledger.py:261-275, 736-745, 1059-1074`。
- 完整 journal 重放与删除 scrub：`manager.py:363-403, 690-700`，导出重放回归 `tests/test_b23_management.py:238-262`。
- 旧 API lazy factory：`service.py:2771-2805`。
- Legacy Core scope 优先且冲突拒绝：`ledger.py:2131-2162`，对应 Ledger 回归已覆盖。

Skill begin/uninstall 锁、Artifact retention capability/sensitivity 屏障、卸载 exact dataset 范围和 API ActionGateway 本轮未发现新的具体 P1/P2。普通 Ledger tombstone 仍未视为物理清理完成。

定向 19 项、集成 876 pass/1 skip、GUI 101/typecheck/build 属于现有证据；本轮未重跑测试。此前本环境 pytest 受无可用临时目录限制，未将其算作代码失败。J1 与真实模型证据仍未齐，当前不是最终验收。

