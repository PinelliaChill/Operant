# B2-7 MP-6.2 评测收口

记录身份：Codex。当前工作树读点为 `684bea1`；产品评测冻结源为 `b932e30`。本页只汇总已有原始 JSON/XML，不修改原始报告，不把脚本、确定性替身或补充问题改写成原固定问题的真实成功。

## 固定数据与确定性基线

`tests/fixtures/b2_7/memory_evaluation.json` 固定包含 29 条合成记忆和 43 个案例（31 个 development、12 个 Gamma holdout）。[evaluation-deterministic-final.json](evaluation-deterministic-final.json) 对每个案例分别运行无记忆、旧最近条目和新 FTS 三组，共 `43×3=129` 次 trusted in-process 确定性召回；没有 Provider/model 调用，不能作为模型质量或普遍收益证明。

该报告三组都完成，固定集指标如下。数值只描述这份合成 fixture：

| 策略 | development precision/recall | holdout precision/recall |
| --- | ---: | ---: |
| 无记忆 | 0.354839 / 0.354839 | 0.333333 / 0.333333 |
| 旧最近条目 | 0.077419 / 0.268817 | 0.083333 / 0.416667 |
| 新 FTS | 0.774194 / 0.720430 | 0.916667 / 0.875000 |

确定性报告中形成内容先冻结再复用，实际 Pack 选中了冻结记录；这是确定性协议/召回边界证据。Host 性能沿用 B2-4 范围，本批没有性能优化，也不由这些数值声称普遍质量收益或延迟收益。

## 最终真实 Provider 对照

[evaluation-real-final.json](evaluation-real-final.json) 使用已发现的精确模型 `gpt-5.6-luna`，每个策略 4 个固定案例，三组共 12 次真实调用；12 个任务均完成，但答案成功数不同：

| 策略 | 答案成功 | model calls | task success |
| --- | ---: | ---: | ---: |
| 无记忆 | `0/4` | 4 | `4/4` |
| 旧最近条目 | `2/4` | 4 | `4/4` |
| 新 FTS | `3/4` | 4 | `4/4` |

这组小样本只证明本次固定输入下的观察结果。它没有证明普遍质量收益；Host 指标沿用 B2-4，也没有新增延迟优化。报告的 `first_model_wait_ms` 为 `null`，first-token 未测；价格未配置，cost 为 `unknown`，usage 只能按报告中已知/未知状态读取，不能把 unknown 当作零。

最终真实报告的跨任务链仍为 `blocked`（B25 propose 没有得到治理 proposal）。该旧报告不能单独证明后续形成复用完成；修复后的分段证据见下文。

## 真实跨任务形成与复用链

[evaluation-learning-final-04.json](evaluation-learning-final-04.json) 记录了 2 次真实模型完成和 1 次 read_file 工具调用，入口为 `ApplicationService.run_session -> B25 propose/review`，并生成冻结 record。其 Pack 已选中该 record，但原固定复用没有 model completed event，属于 timeout/`agent_failed`，不能记为成功。

后续两个报告保留在同一合成数据库和冻结 record 上：

- [evaluation-learning-retry.json](evaluation-learning-retry.json)：原固定问题的 Pack 选中且完成 1 次 model 调用，但拒绝转述 `inferred` 证据，`answer_success=false`。此前 timeout 的 usage 保持 `unknown`，没有按零计数。
- [evaluation-learning-qualified-recall.json](evaluation-learning-qualified-recall.json)：问题明确要求转述推断证据及其条件后成功，Pack 仍选中同一冻结 record，cutoff 与 freeze 一致。这是问题改变后的补充成功；它不提升 evidence trust，也不计入原固定问题成功。

复用证据链覆盖真实形成、正式治理、冻结、新任务选入和带条件转述；原固定问题仍未成功，不能回填为成功。原计划要求验证形成后复用，没有规定该固定问题必须答对；按最终独立审查，本链满足 MP-6.2 的范围要求，拒答作为真实模型限制保留。

补充报告记录的HEAD为6661ef5，运行时包含未提交的retry问题调整；相同逻辑随后提交为684bea1，仅报告文案和折行另有修整。不能将该报告描述为干净6661ef5产生的结果。

## 失败 attempt 与修复归因

所有早期报告保留，按原因区分：

| 原始证据 | 归因 | 收口解释 |
| --- | --- | --- |
| [evaluation-real-attempt-20260918.json](evaluation-real-attempt-20260918.json) | 评测脚本缺陷 | 旧脚本读取了 ContextRevision 便捷字段而不是实际 `b24_context_memory.body.pack.selected`，并把旧策略无 model completed 误算成 completed；不能作为最终 12 调用验收。修正后的最终报告单独保存。 |
| [evaluation-learning-final.json](evaluation-learning-final.json)、[evaluation-learning-final-02.json](evaluation-learning-final-02.json) | 真实链未形成有效治理记录 | 分别记录 B25 proposal 缺失和 review 失败，不能靠文档改成成功；保留为早期失败链。 |
| [evaluation-learning-final-03.json](evaluation-learning-final-03.json) | 复用选择不合格 | 选中了 `a_recent` 而非形成的 record，故不计入冻结记录复用。 |
| [pytest-projection-fix.xml](pytest-projection-fix.xml) → [pytest-projection-fix-02.xml](pytest-projection-fix-02.xml) | 真实产品大 Projection 修复 | 前者 11 项中有 1 项回执错误码断言失败；产品修复后后者 24 项全部通过。该修复由产品源差单独证明，不等同于评测脚本修正，也不覆盖真实 Provider 原固定问题未成功的事实。 |

产品 Projection 修复范围按冻结源核对；不在本页展开全量 hash。脚本和新 retry 入口已单独通过 `ruff format --check` 与 `ruff check`。

## 复用范围与限制

可以复用的证据仅限于输入、源读点、正式入口、Pack 选择和报告中明确完成的步骤。确定性报告不能替代真实模型；read-only preflight 不能替代 12 次真实对照；qualified recall 不能替代原固定问题；B2-4 Host 性能证据不能被写成 B2-7 的优化结果。所有报告只使用隔离 SQLite/合成 workspace，不含真实用户库、凭据或 Provider URL。

评测收口状态：按 [最终独立审查](review.md)，固定三组对照、确定性矩阵和形成后带条件复用已满足本批验证范围。原固定问题保持负结果；不把它作为被补充问题替换的成功样本，也不承诺普遍质量收益。
