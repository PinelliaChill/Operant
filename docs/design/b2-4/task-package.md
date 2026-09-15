---
task_id: B2-4
owner: Codex
status: implementing
scope: B2-4 / MP-3 / GUI-L3 / J2 only
base_head: 7f8273ab2cfc669ec8c34044c320e436ada2dea1
code_head: 7a2d916
delivery_head: null
governance:
  root: /Users/bigo/agentworkspace/codexworkspace/operant
  version: workflow-20260913.2
  files:
    # Original governance hashes were computed at task creation and are not present in this log.
    # Recovery retains the task body; governance hash values remain unverified.
implementation:
  worktree: /Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery
  original_worktree_missing: /private/tmp/operant-b2-4
  git_status: isolated persistent worktree; original branch registration preserved
  branch: codex/b2-4-recovered
  dirty_ref: null
coordination:
  antigravity_task: d6db978d-d81d-45ba-b02a-2d85428f1903
  initial_activation: user_prompt_and_antigravity_acceptance_verified
  delivery_channel: Antigravity native message input via CUA
acceptance_environment:
  holder: null
  revision: null
  resources: ["j2-current/native03-freeze.json (segment ended)"]
  paused_writers: []
  release_condition: end acceptance segment before any fix
evidence_index: []
review_ref: "cli:01a09abd-f0d9-76e0-828e-b75a1f014e9e"
next_action: close remaining J2 evidence gaps, resolve Host performance gate, finish current independent review
---
# B2-4 唯一任务包
记录身份：Codex；适用对象：所有 Agent；2026-09-13。

本次仅完成统一计划 B2-4、记忆设计 §13.6 MP-3.1–5 与 J2。治理规则按上方绝对入口按需读取；B2-3 已交付且源码树干净，复用其冻结生命周期证据，不重做 MP-0–2。治理根旧分支和用户改动保留。禁止 push、merge、deploy、真实用户库迁移及 B2-5+。Fast 工具未提供可选开关，子 Agent/独立 Reviewer 使用 gpt-5.6-luna/max，不冒称 Fast。

## 恢复状态（2026-09-14，Codex）

原 /private/tmp 实施树与验收环境缺失。本文件由原任务包正文和已确认成功的增量恢复，仍是同一 B2-4 任务包；初始动态治理哈希未恢复，不宣称正文已逐字还原。下表历史结果须结合恢复证据重新核对。

- 持久源码位置为上述 recovery export，基线仍为 7f8273a；治理根既有源码与 Git worktree 登记未覆盖或清理。
- 恢复时 172 个 Core/SDK/工件增量文件 SHA 一致；api_phase23 及工件测试与后续 Reviewer 读回 SHA 一致。Graph/Team 持久层已精确恢复。
- 独立环境按 uv.lock 安装完成；恢复树记忆运行、检索、Token计数 27 项通过，完整门禁合计952项通过、1 Docker条件跳过，回环测试在允许127.0.0.1绑定后定向通过。
- CSS 与视觉组件由原任务公开工具记录恢复；Antigravity 原实名交付见治理 COM-20260913-002，GUI106项/typecheck/build通过，主入口产物匹配原最终SHA；当前渲染尚待验证。
- 工件增量 Reviewer 已关闭误报P2，确认无新增P1/P2；性能硬门仍未通过。B2-4 未完成，不进入后续批次。
- 恢复清单见上两级目录 recovery-status.json、assembled-hash-audit.json、manifest.json；历史原路径仅作来源定位。

## 文件归属
| 路径 | 唯一写入者 | 交付对象 |
| --- | --- | --- |
| src/operant/memory_plugins/retrieval.py、tests/test_b24_retrieval.py | Codex（原 retrieval 已交权） | User |
| clients/gui/src/features/collab/LiveGraphTeamView.tsx、clients/gui/src/features/collab/b24-*、clients/gui/test/b24-* | Codex（原 GUI 已交权） | User |
| clients/gui/src/features/collab/B24Presentation.tsx、clients/gui/src/styles/b2-collaboration.css | Codex（Antigravity 已实名交权） | User |
| src/operant/application/token_counting.py、tests/test_b24_token_counting.py | Codex（原 token_budget 已交权） | User |
| 公共Schema/SDK生成/API、Ledger/迁移、Manager、Context/Runtime集成、GUI上下文接入、证据与权威文档 | Codex负责人 | User |

子 Agent 先定向测试，不重复完整门禁；不提交共享树或操作用户服务。交接给改动、验证、限制、停止写入。Reviewer 只读独立审查，问题有要求来源或复现。

## 验收表
| ID | 要求与来源 | 负责人 | 影响类别/检查或复用理由 | 环境/预算 | 阻断？理由 | 结果/证据 |
| --- | --- | --- | --- | --- | --- | --- |
| AC-01 | 中文短词/代码标识符、FTS候选合并、条件过滤/去重多样性；MP-3.1 | Codex | Core定向召回测试及冻结fixture | 临时库；同数据/权限 | 是：任务召回 | 定向检索与质量对照通过；保留集0.9167≥0.75，见performance-recovered-baseline.json；计划复用27项回归和A/B/A结果一致 |
| AC-02 | 自动/显式Memory Pack与ContextRevision，版本/来源/条件/原因；MP-3.2 | Codex | Schema/Manager/Composer正式入口 | Session/Workflow/Graph统一链 | 是：上下文真实 | Memory Pack/Context原子记录与显式引用回归通过，见gates/recovery-memory-final.log；当前真实单/双Agent及原生检查器读回通过，见j2-current；其余联合边界按AC-08审计 |
| AC-03 | 常驻/动态/Skill/历史/工具/包装/输出共同预算；精确或保守估算/切模型重算；MP-3.2/3 | Codex | Core预算/工具Schema/关键条件定向检查 | 正式ModelProfile | 是：预算完整 | 预算与计数测试通过，包含于gates/recovery-complete-gates.json；未知模型保守估算，不伪报精确 |
| AC-04 | Run截止点冻结、回合/阶段刷新、当前撤销/权限优先、污染摘要清理或停止；MP-3.4 | Codex | 并发/恢复/Provider发送前复核 | 临时库；越权/撤销零泄漏 | 是：安全语义 | 冻结/刷新/撤销/角色收紧回归通过；nextsend-real.json同Session真实下一发送验证移出/刷新生效，撤销已发送记忆后PermissionError且0Provider，源码前后一致 |
| AC-05 | 同算法直接/进程内/隔离性能；保留集不退化及MP-0固定门；MP-3.5 | Codex | scripts/benchmark及原b2-1/evaluation-baseline.md门 | 相同数据权限模型调用数，冷/热/CPU/RSS/RPC/Token/成本分别记录 | 是：性能门 | 保留集0.9167、零泄漏；Host性能未达门，见performance-recovered-baseline.json与performance-current-status.json，继续优化 |
| AC-06 | GUI-L3模板/成员、支持编辑发布运行、群聊/定向消息、任务/工件板、Agent个人页 | Codex | 正式API/生成Client/GUI业务，服务端状态守卫 | 临时项目/Graph/Team；无需手填内部ID | 是：协作闭环 | 当前API13项目录回归、GUI108项/typecheck/build通过；native03实际Graph→Team恢复、任务rev2→3→4、消息Mailbox与SSE Cursor10通过，见j2-current/native03-native-readback.json；其他协作证据按范围复用 |
| AC-07 | 上下文检查器实际条目版本/来源/条件/Token/原因/移出本次/刷新 | Codex | 正式Projection与GUI交互 | Session真实Provider上下文读回 | 是：可解释 | 当前真实原生检查器读回版本/来源/条件/Token；移出后Manifest rev2，刷新后rev3且保留排除，历史上下文不变，见j2-current/native-context-controls.json |
| AC-08 | J2单Agent与真实多Agent共用记忆；无插件普通任务/强依赖显式失败 | Codex | 正式discover/ModelProfile/Session/Graph，明确workspace/tool policy | 合成任务；只读工具；受控调用预算，记录精确model ID | 是：联合硬门 | 当前gpt-5.6-luna正式单/双Agent调用通过：单RIVER_42/42、双CEDAR_29/42及私信模型输入隔离，见j2-current；nextsend-real.json中默认memory_plugin_mode=True/零安装普通Session成功，强依赖memory-standard返回明确409且0Provider |
| AC-09 | 视觉实际挂载宽窄/长文本/焦点/颜色，断线失败明确无Mock，原生壳 | Antigravity自检 / Codex最终 | GUI与真实Tauri；B2-3不变生命周期证据按摘要复用 | 独占源码/构建/临时Core/窗口，无热更新写入 | 是：客户端验收 | Antigravity实名交付见治理COM-20260913-002；当前GUI108/typecheck/build通过；native03在1200/800/640宽度检查布局、长文本/焦点，断线显式Load failed且运行禁用，无Demo回退；见j2-current/native03-native-readback.json |
| AC-10 | 影响矩阵完整基础门禁、GUI脚本、SDK确定生成 | Codex | ruff format/check、mypy src、pytest、uv lock --check --offline、git diff --check；GUI test/typecheck/build；SDK | 集成冻结代码一次完整，缺陷只重验受影响项 | 是：工程门 | 当前9295098完整门禁969pass/1Docker条件skip，所有基础检查通过、前后源码不变，见gates/current-03-results.json；其后仅GUI增量108项/typecheck/build，按矩阵复用不变后端证据 |
| AC-11 | 指定gpt-5.6-luna/max独立Reviewer、问题闭环 | Codex Reviewer | 只读代码/证据审查；Fast仅可用时 | 原Agent增量复核有效证据 | 是：独立门 | 工件增量原Luna/max独立审查关闭，见review-artifact-closure.md；最新性能/QueryPlan增量最终审查待完成 |
| AC-12 | 当前实现文档/进度/交接、源码和版本读回，完成停止 | Codex | 文档回读/链接/敏感值/diff；治理忽略文件对比快照 | 不推进B2-5、不外发仓库、不迁移用户库 | 是：交付边界 | 本地检查点57ab2e6、9f0938c；文档持续同步；性能及最终验收未完成，不进入后续批次 |

## 证据与环境
每份证据记录 evidence_head、相关源码/依赖/Schema/构建摘要、命令/入口/模型/环境与实际结果及限制。正式验收前填充独占资源并暂停相关写入；发现缺陷先结束该段再修复。证据复用遵守治理沟通规则 §5，不把Mock/skip/报告当实测。
视觉工单：治理根 memory/communication/items/COM-20260913-002.md（UI-COLLAB-03）。

## 当前性能诊断归属（2026-09-14，Codex）

产品源码与公共契约由 Codex 持有。原 signature_allocation 子 Agent 在实施树同级 signature-diagnostic/ 验证 raw-stat 对当前产品扫描器的收益；原 performance 子 Agent 在 native-scan-diagnostic/ 验证单次 C 调用的完整扫描。两者仅写诊断目录，测量前协调，禁止改变门槛或省略权限/完整性检查。收益与变化检测未证明前不接入产品。

2026-09-14 Codex：已从原任务终态核实性能Agent曾因workspace额度停止；原performance已续接处理同一C原型，raw-stat支线保持停止并保留记录，先集中验证完整C扫描。该诊断仍未接入产品。

## B2-4 验收命令授权（2026-09-14，Codex）

User 已明确同意安装 `/Users/bigo/.codex/rules/operant-b2-4-verification.rules`，仅放行固定实施目录的 pytest、ruff format --check、ruff check、mypy 四条前缀。安装内容已与批准稿逐字核对；当前会话是否热加载尚未验证。B2-4 完成时移除此独立规则文件。禁止推送、合并、部署、迁移真实用户库的约定不变。

2026-09-14 Codex：恢复原生壳已按 Cargo.lock 离线构建，内嵌61项已核对GUI资产，无Vite/HMR；独立验收应用标识为 dev.operant.b24.verification，本地adhoc签名验证通过。详见实施树上级 native-build-recovered.json；尚未启动，也未据此宣称原生业务验收通过。

2026-09-15 Codex：原生Live预检已显示Core已连接；中断后服务失联时命令禁用且无Demo回退，恢复同一隔离测试库后重连成功。证据位于上级 native-preflight-01/result.json，仅是环境预检，不当作J2。typed recall适配保留完整Schema重验，12项扩展等价/非法对象回归通过，见adapter-validation.json。性能脚本改为两个完整独立运行，原performance定向测试后由Codex冻结测量。

2026-09-15 Codex：分pass正式性能测量完成，源码前后SHA一致，三模式时延与分配结果ID全一致。直接检索全部门通过；可信Host warm wall 5.1376ms（上限4.5118）、首查询4.604ms（上限4.5711）、CPU p95 3.540ms（上限2.9313）未过，隔离CPU p95 4.956ms（上限4.690）未过，其余通过。见performance-split-01-report.json/status.json；不重复跑同一版本寻求偶然通过，继续定位Host开销。原生预检服务已正常停止，应用保留用于后续固定J2。

2026-09-15 Codex：冻结源码真实单Agent成功返回RIVER_42/42；双Agent均实际发送CEDAR_29、调用read_file并回答42，定向消息隔离通过，合计228/4096输出Token。原生检查器读回4998/29139输入、1452/2000记忆、版本与来源；移出后Manifest revision2记录excluded，刷新后revision3保留excluded，历史上下文仍可见。详见j2-current/。原生发现正式Graph只经legacy发现导致新窗口列表空，已结束该段验收；Core新增有界Graph摘要目录，API13项通过、SDK双生成digest一致，GUI恢复运行接入由原gui_collab负责。当前完整基础门禁current-delivery-gates-03进行中；Host性能仍未通过。

2026-09-15 Codex：native03固定构建已完成Graph目录增量真实原生验收，源码/GUI前后哈希一致，测试Core与验收窗口已关闭，见j2-current/native03-native-readback.json。原Luna/max CLI Reviewer已恢复审查9295098与3个GUI文件；原performance恢复独立诊断，gui_collab仅做只读验收缺口审计。性能门仍未通过，B2-4继续实施。

2026-09-15 Codex：native04实际自收Artifact发布rev1/recipients，切换非接收者工件板为空且清空发布选择/标题，正式HTTP双身份读回一致，见j2-current/native04-result.json。原Luna/max本次目录审查指出全局运行返回与全局100条截断问题；已改为显式workspace过滤及稳定Cursor翻页，API13项和GUI109项/typecheck/build/mypy通过，见directory-pagination-validation.json；待独立复核和最新原生分页验收。

2026-09-15 Codex：目录修复已保存7a2d916，Luna/max原会话复核因workspace额度耗尽终止，尚无关闭结论，见review-current-status.json。只读验收审计明确AC04尚需同Session下一Provider请求验证，AC08尚需同环境无插件普通任务/强依赖拒绝配对，见acceptance-gap-audit.md；已交脚本任务。native05已从第二页找回真实Graph/Team，但Enter造成重复prepare同一Team（2Session/1Team/4Context保持），已结束该段并修GUI显式按钮与终态/已绑定守卫，110GUI测试通过；最新native06待验。完整基础门禁gates04继续运行。

2026-09-15 Codex：current-delivery-gates-04已完成：969pass/1Docker条件skip、format/lint/mypy/offline-lock/diff全通过，相关源码前后哈希不变，原始日志见gates/current-04-*。GUI键盘守卫增量110tests/typecheck/build通过，非Core变更复用该后端门禁。native06构建已准备，但启动前healthz自动审批因workspace额度耗尽失败，未完成验收；已核对并停止测试Core34227，见j2-current/native06-status.json。

2026-09-15 Codex：nextsend-real正式gpt-5.6-luna/low补验通过：3实际调用/42输出Token；baseline选入显式记忆，移出+刷新后新记忆入请求且旧marker缺席，撤销已使用记忆后同Session PermissionError/0Provider/无新Context；正式默认模式零插件普通任务成功，强依赖memory-standard明确409且0Provider。见j2-current/nextsend-*，源码与脚本SHA前后不变。native06分页与Enter守卫通过，Core日志0次重复POST，临时Core/窗口已关闭。Host性能与最终独立复核仍待关闭。
