本片无可复现 P1/P2。源码层面满足本片测量重构条件，但不代表性能门通过。

- timing/allocation 不创建 `RpcCounter` 或统计包装，直接走生产 `Manager.search`：`scripts/benchmark_b24_memory.py:691-771, 835-1015`。
- observation 使用独立 SQLite/Registry/Host，启动计数在 `1042-1047` 处分离；`_assemble_passes` 校验配置、请求、质量及逐样本返回 ID：`774-813`。
- forbidden 覆盖全部重复轮次：`543-594`；固定阈值仍从旧基线读取，失败继续报告 `not_passed`：`1239-1352`。
- 既有16项测试仅作证据核对，本次未运行。新增测试覆盖 clean trusted pass、三 pass 漂移拒绝及早期 forbidden：`tests/test_b24_benchmark.py:210-309`。

非阻断测量缺口：

- direct 的 `configuration_key` 仅含 fixture/role/agent（`730-734`），未包含报告中的 `POLICY` 字段（`1475-1480`）；当前无运行时漂移证据，但未来策略变更可能无法被三 pass 校验发现。
- clean-observer 回归只实际覆盖 trusted in-process，isolated 分支仅有源码 guard，未有对应测试证据。
- 文档已明确 synthetic fixture、零模型调用、stdio 字节为重编码而非线缆捕获；原性能失败仍有效。

当前 SHA：

- `scripts/benchmark_b24_memory.py`
  `49b545f421580c4348d46d9fbb879f220d8b30a276cd4fbe9eb35af08f5d80ff`
- `tests/test_b24_benchmark.py`
  `a11a04e6bf75058d33b6be4758d39c53124933eca4da4a4762847dd67b06f671`
- `docs/design/b2-4/measurement-review.md`
  `954619d1e44ec326c717dd2f8a2dd8784df639411c75d13c23a5fea8f61817cb`
- HEAD：`06351e92a499e9d00b014aff22c1fe8f125ccfa4`
