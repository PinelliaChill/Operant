## 增量结论

上次 5 项全部关闭；本轮未发现剩余 P1/P2。P1-01 正式撤回，不再作为阻断。

| 项目 | 结论与证据 |
|---|---|
| P1-01 inactive ref | 已关闭。`manager.py:989-1003` 要求 published 精确 ref；`_pending_refs` 仅在 confirm/extract 的 Core staged 路径写入（`1109、1225`），并在 `1261` 清理。恶意 recall 用例 `tests/test_b23_management.py:346-378` 实际被 Host 拒绝。 |
| P2-01 owner/source | 已关闭。Manager 将 Proposal owner/source 与真实 installation/version 绑定：`manager.py:1016-1030`；Ledger 对构造和 supplied Proposal 校验：`ledger.py:1061-1075、1190-1195`。伪造 principal、删除/替换 source 的用例均拒绝。 |
| P2-02 新 head CAS | 已关闭。新 head 和“已有 version 但缺 head”路径均检查 expected revision 0：`ledger.py:769-798`；对应回归 `tests/test_memory_ledger.py:193-204`。 |
| P2-03 project_detach | 已关闭。detach 只清除 `installation_id`，不修改 `archived`：`manager.py:477-486`；可重新绑定且源码保留，见 `tests/test_b23_management.py:413-424`。 |
| P2-04 effective_at | 已关闭。设置按 `scope:key` 维护 fingerprint 与独立时间：`manager.py:90-99、135-149、339-342`；Role 使用 `role.created_at`：`343-357`。无关改名不改变时间，见 `tests/test_b23_management.py:428-455`。 |

Manager 全局关闭回执、完整 journal 重放/删除 scrub、旧读 lazy factory、Legacy Core scope 优先也与当前修复记录和代码一致；本轮不重复升级。

本轮只做增量代码核对，未重跑测试。J1、真实模型及最终冻结证据仍未齐，因此不代表最终交付完成。

