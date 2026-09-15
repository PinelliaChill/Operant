# B2-4 AC02/03/04/08 只读验收缺口审计

记录身份：Codex
日期：2026-09-15
范围：只读核对 `D=/Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery`、任务包、统一计划 B2-4/J2 与 MP-3。未修改产品源码，未操作原生窗口、Core 或测试服务，未重跑全套测试。

## 权威要求与判断原则

- 本批验收表：`D/docs/design/b2-4/task-package.md:62-76`。
- 无插件基础任务、下一 Provider 请求的关闭屏障、召回与预算：`docs/design/Operant-Beta-2.0更新计划.md:150-164`。
- MP-3.2～3.4：`docs/design/记忆系统设计草案.md:628-633`。
- 报告、静态单测和旧批次记录只能证明其记录的源码/入口/环境；不能自动替代当前源码上的真实请求证据。

## 逐项结果

| ID | 当前判断 | 已有路径证据 | 尚缺或限制 |
| --- | --- | --- | --- |
| AC-02 | 基本闭合；真实 J2 已证明自动上下文链，显式合流的真实请求字段粒度仍可加强 | `D/docs/design/b2-4/j2-current/single.json:42-50` 有正式单 Agent 的 ContextRevision、memory_sent 和成功 Provider 链；`D/docs/design/b2-4/j2-current/multi.json:16-43`、`D/docs/design/b2-4/j2-current/multi-readback.json:19-65` 有双 Agent 的 Context、Memory sidecar、同链和私有隔离；`D/docs/design/b2-4/j2-current/native-context-controls.json:6-60` 回读版本、来源、条件、原因、Token 以及 exclude/refresh 后的 Manifest；`D/tests/test_b24_memory_runtime.py:107-141` 验证正式 Session 持久化包含 `automatic_and_explicit` 的 Memory Pack。 | J2 的真实报告没有把“自动候选”和“显式引用”拆成两次独立 Provider 请求并逐项展示原因；现有显式合流回归使用确定性 Provider。若验收要求真实请求级拆分，需要补一条带显式引用的当前 J2 readback；这不是当前 GUI 缺口。 |
| AC-03 | 定向实现与测试证据闭合；真实 J2 报告的分项展示有限 | `D/tests/test_b24_token_counting.py:79-109` 覆盖精确 tokenizer、wrapper、工具 Schema/调用和输出预留；`:112-153` 覆盖未知模型保守 UTF-8 估算及不完整 wrapper 不报 exact；`:156-182` 覆盖切模型重新计数；`:185-216` 覆盖工具调用/Schema变化；`D/docs/design/b2-4/j2-current/multi-readback.json:19-65` 有真实模型的聚合输入/记忆/输出预算读回；任务包 `D/docs/design/b2-4/task-package.md:67` 记录预算回归已通过。 | J2 readback 展示的是聚合计数，没有一条真实请求同时列出常驻、动态、Skill、历史、工具、wrapper、output 各分项；分项语义由定向测试覆盖。若 Reviewer 要求真实请求逐项账本，应补记录，不需要改 GUI。 |
| AC-04 | **未闭合，阻断** | `D/tests/test_b24_memory_runtime.py:68-93` 覆盖冻结召回及撤销复核；`:145-172` 覆盖禁用旧上下文时 clean Context 与新 Session 空召回；`:175-199` 覆盖命令层 exclude/refresh 的安全点和 revision；`:224-242`、`:287-306` 覆盖历史/共享 Graph 证据撤销后的阻断；`:372-402` 覆盖显式排除、角色收紧和当前权限优先。原生记录 `D/docs/design/b2-4/j2-current/native-context-controls.json:13-60` 证明 Manifest revision2/3 和排除保留，但明确写出 `No subsequent Provider request after controls in this UI segment`。 | 缺少同一真实 Session 在 UI exclude/refresh 或 revoke/permission 之后发起的**下一次 Provider 请求**，并读回该请求的 ContextRevision/MemoryPack，确认受影响记忆已剥离；若无法安全剥离，应记录 clean Baseline 或暂停。现有后端回归不能替代这条真实下一发送证据。 |
| AC-08 | **部分闭合，当前 J2 证据仍缺口** | 要求见 `D/docs/design/b2-4/task-package.md:72`。旧 B2-2 实际记录 `D/docs/design/b2-2/environment.md:14-15`、`D/docs/design/b2-2/client-acceptance.md:5-18` 证明无安装插件环境下使用正式 `gpt-5.6-luna`、只读工具与历史/错误恢复；其交接 `D/docs/design/b2-2/handoff.md:30-31` 明确普通 Core 默认无插件及恢复边界；当前源码 `D/src/operant/api_phase23.py:274-318` 在强依赖 Graph 缺少插件/项目或插件 ID 不匹配时抛出 `required memory plugin is unavailable`；`D/tests/test_b2_2_api.py:211-221` 验证带 `required_plugins=("memory",)` 的 Graph 编译被拒绝而普通 Graph 可编译。 | 旧 B2-2 真实记录不是当前 J2 的一次新请求，且当前 `api_phase23.py` 已有后续增量；当前树没有一份直接证明“普通无插件任务仍真实运行”与“当前 API 启动强依赖 Graph 显式失败且不自动安装/替换引擎”的同环境组合证据。若不复用旧证据，需由负责人用当前源码、绝对 workspace、正式 ModelProfile 做一次受控普通任务，并做一次无 memory manager 的强依赖 Graph HTTP/API 失败检查；本审计不执行。 |

## 交接结论

1. AC-02/03 的实现和既有回归可继续复用；若要求更细的真实请求账本，只需补证据记录。
2. AC-04 仍是硬缺口，优先补“控制操作后下一次 Provider 请求”的真实 readback，再决定 clean Baseline/暂停语义是否触发。
3. AC-08 的静态 fail-closed 守卫和旧 B2-2 实际无插件记录已存在，但当前 J2 仍缺同环境普通任务与强依赖显式失败的成对证据；不能仅凭架构说明或编译单测宣称已闭合。
