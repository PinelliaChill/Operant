# B2-5 / MP-4 独立审查报告

审阅身份：Codex Reviewer，`gpt-5.6-luna/max`。审阅时间：2026-09-16（Asia/Shanghai）。本报告只审 B2-5/MP-4；没有修改产品源码、没有合并、部署或迁移真实用户库。

## 审阅快照与证据边界

- 基线：`c2e89758b5d4b3c779353ca1fe863d1fec9cee79`；实施树：`/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5`；分支：`codex/b2-5-memory-governance`。工作树仍是 dirty，任务包 `code_head`/`delivery_head` 尚为 null。
- 当前关键源码 SHA-256：`governance.py` `0a734546afef9eaca706caac2112035955e86df36af8a1a43dfd5698fe71ebc8`；`ledger.py` `ae4b8f8419e07ebe141f71c37688d83818650c41f71c57536c7178ff8b474a07`；`maintenance.py` `1e3b6729f0d2e40c214d4e52cb2a1d0ef84e89f2a2d0490feff89b7540447619`；`api_b2_5.py` `0b765d024a40d5d0cf5715f435450eb735e0f1a5068a397fd6665f23476c410b`；`contracts/b2_5.py` `6ea88b5d1456244bcb1a50e53ece7f8d8a6ba5f89313d16f4d57c4e3e152ba99`；`B25Presentation.tsx` `289c75d8983d3f7f32734baf7839dbced551452102ca937c0b068fb15d67a5f9`。
- `live-05.json` 的 source hashes 与上述当前源码一致；其中正式模型为 `gpt-5.6-luna`，真实 Scheduler/Graph/Host 链路完成一次 succeeded 与一次 no_change，明确确认后发布，撤销后下一次发送为 `0` 模型调用，关闭开关阻止新任务。该证据覆盖成功链和部分撤销/关闭边界，不能覆盖下面的崩溃窗口、错误/冲突原生路径或本报告发现。
- 定向验证：`uv run pytest -q tests/test_b25_governance.py tests/test_b25_api.py tests/test_b25_maintenance.py tests/test_b25_scheduler_integration.py` 当前通过（21 项，只有 Starlette/httpx deprecation warning）。B2-5 Schema 生成摘要、记录摘要和 JSON 摘要均为 `8a69102a96d983049967212eed31c864b6959a4f1dae185ba09773b35dd69d20`。这不是完整基础门禁，也不替代错误/冲突原生验收。

## 阻断问题

### P1：普通治理提议仍跨连接分裂提交，崩溃后可以无治理元数据地发布

位置：[governance.py:1213](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1213)-[1240](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1240)。`ledger.propose()` 先在自己的写事务中保存版本和 Ledger Proposal，随后 `_proposal_metadata`、`_relation_rows`、`_dependency_rows` 各自打开新连接。最新 review 的同事务 deadline/source/relationship guard 只保护已经存在的治理行。

复现方式：在 `g.propose()` 的 `_proposal_metadata` 调用处模拟进程崩溃/数据库错误；恢复后 Ledger 仍有 `pending` Proposal，而 `b25_governance_proposals` 和 `b25_governance_dependencies` 都是 0 行。把元数据函数恢复后，对这个 exact Proposal 调 `g.review(..., decision="accept")`，当前会得到 `head.state == "published"`。我在当前源码快照得到的输出是：`{'ledger_pending': 'pending', 'governance_rows': (0, 0), 'accepted_head': 'published'}`。

影响是复核期限、关系和递归来源撤销所依赖的治理事实全部缺失；后续撤销该来源没有这条 dependency 可递归阻断。应把版本、Ledger Proposal、治理来源/关系/依赖放进同一写事务，或在元数据不完整时把 Proposal 标成不可复核并拒绝 review。维护提取路径已有独立的事务提交逻辑，不能据此覆盖普通 `propose` 路径。

### P1：`MemoryVersionRef.content_digest` 没有绑定正文，调用者可以发布伪造内容身份

位置：[contracts/b2_1.py:254](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/contracts/b2_1.py:254)-[282](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/contracts/b2_1.py:282) 的模型校验只检查边界；[ledger.py:560](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/ledger.py:560)-[587](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/ledger.py:587) 原样保存 ref digest；[governance.py:1178](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1178)-[1224](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1224) 接受显式 `version` 后直接交给 Ledger。

复现方式：构造 `content="actual content"`、`ref.content_digest="a"*64` 的 `MemoryVersion`，使用真实 canonical source 调 `GovernanceService.propose(version=...)`，再用 exact review 接受。当前输出为 `stored_ref_digest = aaaa...`、正文 SHA-256 为 `97e9fe2a827cb02d4ba78d17d76cb0089feb95ed5ae8ab61c7af97d9d5e354c8`，且 head 已 `published`。

这会破坏版本不可变身份、精确 Proposal/CAS 和来源引用的完整性；不应信任插件或内部调用者带来的 digest。Core/Ledger 在保存版本前应重新计算正文 digest 并拒绝不一致值（历史兼容数据需单独标明）。

### P1：维护 RunRequest 登记存在崩溃窗口，未登记时会退回普通 Graph Gateway

位置：[maintenance.py:2058](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/maintenance.py:2058)-[2095](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/maintenance.py:2095) 先 `manual_trigger()` 持久化 Scheduler RunRequest，之后才 `register_queued()`；[maintenance.py:1335](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/maintenance.py:1335)-[1344](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/maintenance.py:1344) 查不到登记行时直接调用普通 `graph_gateway.dispatch_workflow(action)`。

复现方式：模拟 `manual_trigger()` 已返回 request、进程在 `register_queued()` 前退出；恢复后用同一 RunRequest 调 `MaintenanceAwareSchedulerGateway.dispatch_workflow`。当前输出为 `{'result': 'ordinary-graph-run', 'delegated_to_normal_graph': ['maintenance-workflow']}`。这绕过了任务包和架构文档要求的“已登记 RunRequest、精确 Workflow/input 快照、Host RunLease、提交前复核”，可能把维护 Workflow 当普通 Graph Agent 执行，且不产生受控 proposal/watermark。

应把登记与入队做成同一可恢复边界，或对已识别的 B2-5 maintenance Workflow 在登记缺失时 fail closed 并进入人工核对；不能把登记缺失当作普通 Graph 请求。`live-05` 的成功链没有覆盖进程在这两个持久化动作之间退出的情况。

## P2 问题

### P2：历史搜索接受未来 cutoff，分页会读入快照之后的新 Item

位置：[governance.py:1589](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1589)-[1645](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1645)。`cutoff_cursor` 只检查非负，没有限制 `cutoff_cursor <= 当前最大 sequence`。

复现方式：先写 cursor 1、2 的两个 Item，以 `cutoff_cursor=999, limit=1` 分页；再写 cursor 3。当前三页返回 `[1, 2, 3]`，三页 cutoff 都是 `999`。原本固定的历史截止点因此被未来写入污染。服务端应拒绝未来 cutoff 或把它规范为本次读取时的当前最大 cursor，并把规范后的值作为后续分页唯一 cutoff。

### P2：旧 `memory_deactivate` 后当前 Projection 仍显示派生子记录可用

位置：[governance.py:1698](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1698)-[1764](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1764) 只查直接 source/dependency 状态；正式召回则在 [governance.py:1802](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1802)-[1833](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1833) 重新检查来源和当前 head，并递归路径在 [governance.py:1854](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1854)-[1898](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/memory_plugins/governance.py:1898) 生效。

复现方式：发布 parent，再发布以 parent 为 `memory_version` source 的 child；通过现有旧停用入口 `ledger.deactivate(parent)` 后读取两条结果。当前输出为 `projection_currently_usable=True, projection_blocked_reason=None, is_recallable=False`。这会让 GUI/Query 显示“当前可用”，而正式发送又阻止同一条记录。Projection 应复用递归依赖/来源授权检查，或在旧停用入口同步写 dependency blocker。

### P2：GUI 关系表单始终发送非法 digest，冲突/替代声明无法提交

位置：[B25Presentation.tsx:250](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/clients/gui/src/features/management/B25Presentation.tsx:250)-[259](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/clients/gui/src/features/management/B25Presentation.tsx:259)。表单固定发送 `content_digest: ''`，Dataset 默认也是 `default`；当前公共契约 [b2_1.py:18](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/contracts/b2_1.py:18) 要求 digest 为 64 位小写十六进制。直接验证会得到 Pydantic `string_pattern_mismatch`，API 在进入治理逻辑前返回 422，因此用户无法从 GUI 声明 `conflicts_with` 或 `supersedes`。

后端 review 已在同一事务验证关系 target 当前存在且为 published，不能靠提交一个空/默认 target 绕过。GUI 应从当前 records 提供精确 target ref/digest，或在没有精确 ref 时禁用关系提交并给出明确提示。

### P2：命令 journal 与业务 mutation/event 仍是分裂提交，未知写结果会留下无事件的已变更状态

位置：[api_b2_5.py:358](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/api_b2_5.py:358)-[382](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/api_b2_5.py:382) 先写 `b25_commands.pending`，业务操作在后续执行；[api_b2_5.py:450](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/api_b2_5.py:450)-[470](/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5/src/operant/api_b2_5.py:470) 才写事件并把 command 置 completed。进程在治理写已提交、事件/journal 完成前退出时，客户端只能 manual reconcile；刷新可看到 Proposal/head 已变，但事件缺失，审计分页和一次性通知不再完整。

这不会自动重放写入，降低了重复副作用风险；仍需将业务结果、事件和 journal 绑定在同一事务，或持久化明确的 `outcome_unknown` 事件并提供可验证的补偿投影。

## 非阻断观察与覆盖边界

- 已核对 Mailbox：治理 canonical history 只查询 `threads/items`，`mailbox_message` 明确拒绝，治理表不复制 Mailbox body。当前 context-impact 只返回 ref/冲突 Proposal ID，不返回正文；但接口只有 `session_id`、没有调用者/session 所属关系校验。当前 Core 不应对不可信网络暴露这些 `/v1` 路由；若未来加公网/多租户入口，这应升级为权限阻断。
- 已核对 exact batch CAS、同事务 deadline/source/关系/旧 head 退役、来源撤销递归传播、同源去重和生成 Client digest；对应定向测试与 `live-05` 成功链通过。该结论不覆盖上述 Proposal 提元数据分裂提交。
- 已核对 `live-05` 的当前源摘要、真实模型调用、GUI 原生成功/窄屏焦点和关闭/撤销结果；错误流断线、关系冲突阻止、未知 Scheduler 登记窗口尚无当前原生证据。维护 `agent.stream_error` 当前标为 retryable，但 Graph status 会把非成功结果送入 DLQ；如果产品要求自动消耗 `max_attempts`，还需补一条真实 transient-error/replay 验收，本报告暂列为覆盖缺口而非新增 P1。
- 本报告不是最终 release approval：仍需 root 对三项 P1/P2 修复或明确取舍，并在最终冻结 SHA 上复核；当前任务包仍未写入 `code_head`/`delivery_head`。
