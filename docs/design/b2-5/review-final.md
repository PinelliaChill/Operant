# B2-5 / MP-4 独立差异复审（最终）

审阅身份：Codex Reviewer，`gpt-5.6-luna/max`。审阅日期：2026-09-16（Asia/Shanghai）。范围只包括 B2-5/MP-4；审阅者没有修改产品源码、没有合并、部署或迁移真实用户库。`maintenance.py` 按冻结版本复核，早期 WIP 版本不作为结论依据。

## 快照和证据边界

- 基线：`c2e89758b5d4b3c779353ca1fe863d1fec9cee79`；冻结产品提交：`02c50bdbc53909fe26c4bf0e2929e2d6e91a8230`；实施树：`/Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-5`；分支：`codex/b2-5-memory-governance`。
- 当前 `HEAD` 与冻结提交一致。`governance.py`、`governance_schema.py`、`ledger.py`、`maintenance.py`、`api_b2_5.py` 以及 B25 GUI 相关源码的当前 SHA-256 与 `frozen-source-hashes.json` 一致；`live-06.json` 和 `live-07.json` 的 118 个源码摘要也全部与当前源码一致。
- `gates-final.json` 记录了冻结提交上的 `1017 passed`、1 项需要显式 Docker 镜像的 skip、ruff 339 文件、mypy 118 个源文件、离线 lock 检查和 GUI 116 测试/typecheck/build/native build 通过。本 Reviewer 没有重复运行完整门禁，以下结论使用该归档证据并做定向源码核对。
- `live-07.json` 使用已发现的真实模型 `gpt-5.6-luna` 完成 `succeeded`，`source_cursor=1`、`processed_cursor=1`，产生一个待人工确认的 Proposal；重复整理为 `no_change`，来源撤销和关闭开关均阻止后续不允许的调用，正式 Recall 和 context-impact 检查也完成。
- `live-06.json` 的真实超时失败保留为负向证据：`maintenance.TimeoutError`、`dead_letter`、`proposal_ids=[]`、`processed_cursor=0`。它没有把未知结果自动重放，也没有提交候选或推进水位。
- `native-review-acceptance.json` 与冻结提交匹配，覆盖当前 published v2 的完整 target ref/digest、`supersedes` 提交、两项精确批量接受、旧 head 退役和 SQLite 回读；同一证据还显示恢复中的 pending command 会呈现待核对警告而不会静默重放。Schema 和生成 Client 的确定性证据见 `protocol-determinism.json`。

## 初审问题闭环

| 初审项 | 当前源码与复核 | 状态 |
| --- | --- | --- |
| P1：普通提议的 Ledger 与治理元数据跨连接提交 | `ledger.py:1027-1254` 将 `transaction_guard` 放入同一个写事务，并在缓存、插入成功和同身份冲突路径都执行；`governance.py:1268-1298` 在该连接中写 proposal metadata、关系和依赖。metadata 注入失败的回滚测试覆盖版本、Ledger Proposal、治理 Proposal 和依赖表。 | 已闭环 |
| P1：正文与 `content_digest` 可不一致 | `ledger.py:560-565` 在写入版本前重新计算正文 SHA-256，摘要不一致直接拒绝。所有新提议、维护提交和生成的精确 ref 都经过这个边界。 | 已闭环 |
| P1：Scheduler 登记窗口退回普通 Graph Gateway | `maintenance.py:1335-1356` 对已发布定义中的 `metadata.maintenance=true` 在登记缺失时 fail closed，并以 `outcome_unknown` 交给调度恢复；已登记路径 `:1357-1398` 使用精确 snapshot、实际 GraphSchedulerActionGateway、Host 优先级和绝对 workspace。新任务使用 `NON_IDEMPOTENT`（`:2097-2102`），重试只接受显式 dead-letter replay（`:2150-2177`）。live-06 真实超时也停在 dead-letter/零水位，live-07 真实成功链完整通过。 | 已闭环 |
| P2：历史 `cutoff_cursor` 可在未来 | `governance.py:1661-1665` 和 detail 路径 `:1731-1737` 拒绝大于当前最大 cursor 的 cutoff。 | 已闭环 |
| P2：旧停用后的递归派生记录仍显示可用 | `governance.py:1805-1818` 在 current projection 中调用 `version_dependencies_valid(..., project_id, context)`，并将依赖结果纳入 `currently_usable`；来源撤销的递归更新仍在 `:1537-1605`。live-07 的来源/时效失效提示与 Recall 结果一致。 | 已闭环 |
| P2：GUI 关系表单发空 digest/default dataset | `b25-state.ts:673-683` 要求 dataset、record、version、digest 全部与当前 published record 精确相等；`B25Presentation.tsx:249-256` 无精确匹配时阻止提交并提示刷新，选择项来自 `:917-933` 的当前 published records。native-review-acceptance 已用完整 v2 ref 提交 `supersedes` 并回读 head 状态。 | 已闭环 |
| P2：API journal、业务写入和事件分裂导致未知写结果无事件 | `api_b2_5.py:38-64` 将完成事件和 command receipt 放入同一事务；`:80-104` 启动时将 pending 一次性转为 `outcome_unknown` 并写事件；`:423-457` journal 只保存 dataset/action 等元数据，`:433-442` 对 pending/outcome_unknown 明确返回人工核对并禁止自动重放。native-review-acceptance 已观察到该警告。 | 已闭环 |

以上七项均能在冻结源码中找到对应防护，初审报告中的复现不再成立。没有发现新的可复现 P1 或 P2 阻断。

## 非阻断观察和覆盖边界

1. `gates-final.json` 的唯一 skip 是显式 Docker 镜像缺失；它不改变本批 Core、协议、GUI 或真实模型证据，但仍是容器集成覆盖边界。
2. `live-06` 的失败安全性已经有真实证据，`live-07` 的成功链也已完成；二者不能互相覆盖，报告保留两种结果以避免把超时失败改写成成功。
3. `api_b2_5.py:305-365` 的 context-impact 返回 metadata-only ref、时效/来源状态和冲突 Proposal ID，当前依赖本地 Core 边界。若未来把这些 `/v1` 路由暴露给不可信多租户调用者，仍需在入口补 session 所属关系校验；按当前架构边界这不是本批阻断。
4. 最终 native 关系与批量 CAS 路径已由 `native-review-acceptance.json` 覆盖；视觉 WCAG 数值审计仍不属于本 Reviewer 的结论，既有视觉交接证据按任务包单独维护。

## 最终意见

在 `02c50bdbc53909fe26c4bf0e2929e2d6e91a8230` 上，初审的 3 个 P1 和 4 个 P2 已完成源码与证据闭环；本独立复审没有新增 P1/P2。产品源码未被本 Reviewer 修改。是否按项目流程最终冻结、交付或合并，仍由 root 依据完整门禁和项目授权作最后裁决。
