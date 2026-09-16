---
task_id: B2-5
owner: Codex
status: implementing
scope: B2-5 / MP-4 only
base_head: c2e89758b5d4b3c779353ca1fe863d1fec9cee79
code_head: null
delivery_head: null
governance:
  root: /Users/bigo/agentworkspace/codexworkspace/operant
  version: workflow-20260915.1
  files:
    - path: AGENTS.md
      sha256: 7ffeca81acbd409b37d9504c905194fc361c5afc76b3338296d229bd150cddae
    - path: MEMORY.md
      sha256: f08729775f0b34094cc5a6391aa440cbf00ab43af915165f5d9015be6bea15f0
    - path: memory/communication/README.md
      sha256: 545facda04ff5501c8e8a0fbdb1ed977500392faa1bbc52a8fbda8acd8b6d179
    - path: memory/communication/items/README.md
      sha256: 3e6554b03f45d6adeb37b09be6e1fcbfa6223e19cb25a2b6184f433367f73b02
    - path: docs/design/Operant-Beta-2.0更新计划.md
      sha256: af1d494c17c95ae03abe976fa16c9bba6676e37c95ffa6bc7685d62030ebf719
    - path: docs/design/记忆系统设计草案.md
      sha256: 269d81dda0e3566e4202c8933bbaa8c1b4ac19df99869c7f07fa20252762cd51
implementation:
  worktree: /Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5
  branch: codex/b2-5-memory-governance
  dirty_ref: null
coordination:
  antigravity_task: 8207c7bb-330f-4469-9e88-391597e79ded
  initial_activation: verified_user_prompt_and_antigravity_reply
  delivery_channel: Antigravity original task native input via CUA
acceptance_environment:
  holder: null
  revision: B2-5 current GUI and Core preview; not final acceptance
  resources: []
  paused_writers: []
  release_condition: null
evidence_index: []
review_ref: null
next_action: 独立Reviewer收口与安全定向补测；整理交付证据后提交工作分支
---
# 本次目标与边界

记录身份：Codex；适用对象：所有本批执行者。

仅实施统一计划 B2-5 与记忆设计 §13.7 MP-4：原始历史、冲突/时效/来源治理、有限后台整理与失败处理、候选/冲突收件箱和生成 Client GUI。
治理与当前计划从上述治理根读取；实施树中的旧计划不覆盖 workflow-20260915.1。PR #20 已于本次通过 GitHub 回读 MERGED，合并 SHA 与 base_head 相同；主目录旧分支及已有改动不参与实现。B2-4 Host 性能限制继续保留。
不进入 B2-6/7；普通工作分支提交/推送沿用当前授权，merge/deploy/真实用户库迁移不在本次授权内。

## 文件归属

- Codex root：公共契约/API/迁移/生成 SDK/Manager 与召回接入/GUI 业务与集成/真实验收及文档。
- Codex 子 Agent governance（gpt-5.6-luna/max）：memory_plugins/governance.py、governance_schema.py、ledger.py 非冻结 SQL 部分及 tests/test_b25_governance.py。
- Codex 子 Agent maintenance（gpt-5.6-luna/max）：memory_plugins/maintenance.py 及定向测试；其他接入点由 root 裁决。
- Antigravity 新进程：视觉呈现组件与样式，待工单和实名接收后授予写权。
- Codex 独立 Reviewer：gpt-5.6-luna/max，只读冻结代码及证据，按差异复核；工具无 Fast 参数时不开启。

## 验收表

| ID | 要求与来源 | 负责人 | 检查与环境 | 阻断及结果 |
| --- | --- | --- | --- | --- |
| AC-01 | MP-4.1 原始历史搜索/按需展开，当前与当时区别，权限与私人 Mailbox 隔离 | Codex | 定向历史/权限测试与正式 Query，临时库 | 是；待验 |
| AC-02 | MP-4.2 冲突/替代、有效时间、来源依赖/撤销传播、复核期限、独立证据去重 | Codex | 领域/召回/并发与过期测试 | 是；待验 |
| AC-03 | MP-4.3 已发布维护 Workflow+有限 Executor+现有 Scheduler，版本/cursor 固定，Proposal/水位原子幂等 | Codex | 真实 Scheduler/正式 ModelProfile，有限合成来源 | 是；待验 |
| AC-04 | MP-4.3 取消/retry/DLQ、前台优先独立预算，关闭无迟到提交，原任务结果不受提取失败影响 | Codex | 失败/取消/关闭/重放定向测试与真实后台调用 | 是；待验 |
| AC-05 | MP-4.4 生成 Client GUI，精确 Proposal/version 批量处理、历史/来源/生效时间/后台状态 | Codex | GUI tests/typecheck/build，正式 API/原生壳交互 | 是；待验 |
| AC-06 | 视觉呈现、长文本/宽窄/键盘/对比、错误/断线只读无 Mock | Antigravity / Codex | 实际组件挂载自检，Codex 原生正式验收 | 是；待验 |
| AC-07 | Core/契约影响矩阵 | Codex | 完整 ruff format/check、mypy、pytest、uv lock offline、diff；生成确定性及 GUI 脚本 | 是；待验 |
| AC-08 | User 指定独立审查与交付 | Codex Reviewer / root | Luna/max 独立审查；阻断风险闭环，当前架构/进度同步 | 是；待验 |

## 取舍与证据

按收益将治理入口集中到现有项目管理流程，复用 Ledger、Scheduler、生成器与当前权限，避免平行发布指针或恢复系统。性能只记录本批明显退化，不重开 B2-4 微小性能优化；安全、数据正确性及真实验收不降级。
每份证据保留 evidence_head 与输入摘要，冻结后仅做适用门禁；无变化场景复用有效证据。逐项实际证据见下方当前检查点；未完成门禁保持待验。

## 当前集成检查点（Codex，2026-09-16）

- SQLite v17、B2-5 API/确定生成Client、来源依赖与精确批量治理、GUI父组件及视觉已接入。真实基线Provider discovery/Session/read_file与原生只读预检通过，仅为预检证据，见preflight.json、model-preflight.json、native-preflight.json。
- Antigravity实际挂载自检已实名交权，1440×900、768×1024、430×932及长文本/焦点见治理COM；最终业务验收不由该报告替代。
- root已接管maintenance.py生产集成，补正式Scheduler Action Gateway/Graph Run/Attempt、Host RunLease、提交前epoch/配置/模型/包复核、前台优先、后台开关和无新来源零调用。当前模型验收尚未全通过，失败记录live-01-diagnostic.json、live-02.json、live-03.json保留。
- 受限执行器不以名称前缀准入：逐项验证B2-5已登记RunRequest、精确Workflow版本和完整输入快照，再复用原Graph授权。此前前缀方案被自动审批拒绝且未执行；本实现采用更严格的登记匹配。
- native-target隔离原生app构建已完成；独立Reviewer曾因工作区消费上限中断，需原任务恢复/完成后才能过独立审查门。

## 收口证据更新（Codex，2026-09-16）

- `live-05.json` 当前全部src摘要匹配，真实 discovery/正式ModelProfile/Scheduler/Graph/Host 提取、重复幂等、空来源零调用、人工确认、Session召回、撤销下一发送阻断全链路通过；旧失败及脚本误判记录保留。
- `native-acceptance.json` 记录隔离构建摘要，原生中文提交/审阅/冲突拒绝/纠正v2、历史、实际上下文影响、后台状态、窄屏焦点/断线只读/重连证据；Antigravity视觉自检见原COM。
- 首轮Python完整门禁993通过、5失败、3跳过；4个失败为既有最新版断言仍写死v16，修正后105项迁移套件通过；1个loopback被sandbox端口权限拦截，按原用例升权本地随机端口补跑通过。只重跑受影响测试，避免重复11分钟全套；不把sandbox失败或Docker跳过记为通过。
- GUI115项、typecheck/build、ruff、mypy118源文件、离线lock、diff通过；生成契约确定性见 `protocol-determinism.json`。最新补充安全测试与独立审查仍待交接，不提前标记本批完成。
- root已持有maintenance/governance/ledger所有产品集成写权；governance子任务最终只补定向测试（9通过），gui_integration最后只补Scheduler安全测试，Reviewer保持独立只读。

## 独立审查修复阶段（Codex，2026-09-16）

`review.md` 为指定 gpt-5.6-luna/max 初审，真实发现3项P1及4项P2，仍属阻断，尚未最终通过。原报告不覆盖改写。

- Codex root修复维护登记缺失路径：从持久发布定义的显式维护标记识别并fail closed，不以命名前缀作为执行准入；维护Scheduler采用NON_IDEMPOTENT以禁止未知模型结果自动重放，DB候选提交仍幂等。12项Scheduler定向检查通过，含Policy DENY/ASK、快照篡改、登记丢失、开关与ModelProfile变化。
- root修复GUI关系输入：选择服务端已发布record/version全ref，不再发送空digest；纯函数测试拒绝空digest/过时版本。新增崩溃命令可核对Projection，重启时一次性生成outcome_unknown事件并保持未知命令禁止重放；API 5项通过。
- governance子Agent负责普通propose原子元数据、Ledger正文digest校验、未来cutoff拒绝和派生记录可用状态一致性；完成后root集成。
- 上阶段 `live-05` 和 `native-acceptance` 保留为修复前历史证据，不能自动覆盖本阶段产品差异。待全部修复后重新冻结，按受影响链路补真实模型与原生关系提交验收，并由原Reviewer差异复审。
