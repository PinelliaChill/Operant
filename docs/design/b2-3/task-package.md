# B2-3 / MP-2 任务包

记录身份：Codex；适用对象：所有 Agent；2026-09-12。

- 唯一负责人及公共契约持有人：Codex。
- 治理根：`/Users/bigo/agentworkspace/codexworkspace/operant`；根 AGENTS 与协作规则 workflow-20260910.3（含 Antigravity 条件静默规则）。入口均已核对可读；不复制治理历史或凭据。
- 实施：`/private/tmp/operant-b2-3`，`codex/b2-3-memory-lifecycle`，基线 `d257abe9eccd2b4792f73e2ff752c040082c26ca`；创建时干净。治理根旧分支及既有修改保留。
- 前置：B2-2 交付及源码已核对；运行验收518bb3f，文档d257abe；MP-0 typed Host SDK冻结，MP-1 Host复用。引用前置证据只证明其原范围。
- 授权：仅B2-3，统一计划§4–6、§8.2及记忆设计§13.5 MP-2.1–6。包括GUI-L2基础与GUI-L4已有Skill管理。B2-4召回优化/Graph/Team、发布签名、推送合并、真实用户库迁移均排除。
- 任务入口：本包及同目录接口/证据；视觉交接 `COM-20260912-001`，子Agent向Codex简短交付并释放路径。

## 文件归属

| 路径 | 执行身份 / 责任 |
| --- | --- |
| src/operant/contracts、persistence/sqlite.py、api*、application/service.py、运行集成、sdk/protocol及生成Client | Codex负责人，唯一公共契约/迁移持有人 |
| src/operant/memory_plugins/ledger.py、tests/test_memory_ledger.py | Codex数据执行子Agent；不改公共Schema/SQLite迁移号 |
| plugins/memory-standard、plugins/memory-notebook、sdk/memory_plugin、tests/test_memory_packages.py | Codex插件执行子Agent；共用冻结Host SDK，不改Core |
| clients/gui业务组件及测试 | Codex GUI子Agent（接口就绪后启动）；不写视觉工单路径 |
| clients/gui/src/styles/b2-memory.css | Antigravity，经沟通区实名领取/交还；Codex验收 |
| docs/PROJECT_ARCHITECTURE.md、本包、CLI/集成测试/证据 | Codex负责人 |

## 验收表

| ID | 要求与来源 | 负责人 | 方法/环境/预算 | 阻断及原因 | 结果/证据 |
| --- | --- | --- | --- | --- | --- |
| AC-01 | 两个独立真实包、Schema/策略不同、共用Host SDK；MP-2.1/3，计划§6 | Codex | 安装与实际RPC，两种Host模式；临时库/目录 | 是，核心范围 | 真实包与两Host模式已调用；标准/笔记原生安装保存查询重装已验；最新精确键在真实macOS沙箱exact1/partial0/value0，见isolated-exact-acceptance.json |
| AC-02 | 唯一head、候选不覆盖、Proposal/CAS、确认/纠正/停用/来源；MP-2.2/3 | Codex | 并发/迟到/幂等/证据状态定向测试与正式API | 是，数据正确性 | Ledger/Manager定向与原生pending→旧查询→确认通过；见test_memory_ledger.py、test_b23_management.py及desktop-acceptance.md |
| AC-03 | 隔离旧库迁移、legacy_unverified、旧写代理/拒绝、新库拒旧二进制；计划§8.2 | Codex | 合成v14旧库副本迁移读回，数量/历史/所有权核对；不接触用户库 | 是，迁移门 | 合成v14旧库→v15→显式迁移通过；2旧版本/1记录未发布，历史与他项目隔离；实际B2-2旧代码拒绝v15，见migration-acceptance.json |
| AC-04 | 关闭完整屏障、全局优先、再开无补扫、无插件普通任务；MP-2.4，计划§6 | Codex | 在途/队列/索引/Provider下一请求与普通任务定向及真实验收 | 是，关闭语义 | 在途取消/全局拒绝/blocked回执与确定性正式Session/工具/Skill/Context隔离通过；真实普通模型任务尚未通过 |
| AC-05 | keep/delete、Core namespace、专属目录、共享/Run/历史等例外、tombstone、清理续做、保留数据导出/删除/重装；MP-2.5，计划§6 | Codex | 临时数据，故障/重启/缺Hook测试，桌面闭环 | 是，生命周期门 | 定向keep/export/reinstall/delete专属行与tombstone通过；原生keep/导出/重装通过；原生永久delete待本次CUA确认 |
| AC-06 | 项目/有效配置与来源/作用域、知识候选搜索修改、插件管理、已有Skill基础管理、Artifact保留；计划§4/5 GUI-L2/L4 | Codex | 正式API+生成Client+GUI行为检查 | 是，管理闭环 | 管理与生成Client已接入；项目创建改名归档解除/知识/Skill安装启用/Artifact pin与审计/保留拒绝已原生验证；Skill剩余/视觉返修待补 |
| AC-07 | additive App Protocol/Python与TS生成Client/CLI；MP-2.6，AGENTS协议要求 | Codex | 确定生成、旧协议兼容、现有SDK脚本 | 是，契约门 | 现有产物与双次生成一致；正式Python Client/CLI与原生TS Client通过，见sdk-generation.json、client-smoke.json |
| AC-08 | J1真实Tauri安装保存查询关闭keep/delete保留管理重装；计划§4/6，AGENTS客户端 | Codex | debug原生壳，宽窄屏/焦点/对比度/错误断线/Action Gateway | 是，联合门 | 部分通过，见desktop-acceptance.md；永久delete、最终宽窄焦点与剩余Skill待验，不能标J1完成 |
| AC-09 | discovery精确模型/正式ModelProfile任务，关闭后普通聊天工具历史；AGENTS模型门 | Codex | 绝对隔离workspace、单调用120s/有限token，失败按协作规则停止 | 是，真实模型门 | 未通过：正式Session/Run ProviderError，HTTP422 invalid_model_error；discovery刷新ConnectError，见model-smoke.json、provider-validation.json |
| AC-10 | 完整基础门禁及GUI/SDK现有脚本；AGENTS开发与验证 | Codex | uv ruff format/check、mypy src、pytest、lock --check --offline、diff --check；GUI test/typecheck/build | 是，硬门禁 | 最新集成888pass/1skip，GUI101测试/typecheck/build及全部基础门禁exit0；source_unchanged=true，见gates/reviewer-final/results.json |
| AC-11 | 当前架构同步、独立gpt-5.6-luna/max审查、版本证据与进度；User/AGENTS | Codex | 同一Reviewer增量复核；工具无Fast开关，不宣称Fast | 是，交付门 | 当前架构已同步；User已授权源码审查；同一Luna/max确认两轮问题全部关闭，无剩余P1/P2，见review-code-closed.md；尚需冻结门禁/J1证据确认，UI-MEM-02待接收 |

## 环境与停止规则

开工先检查Python/依赖、Provider discovery、Tauri及macOS沙箱最小可用性；不替代最终验收。复用B2-2依赖目录，解释器/锁未改；临时Core端口18000/Vite3000先检查占用，用户8000不触碰。运行库只在临时目录；凭据仅注入目标进程，不输出、不复制。
子Agent按需读取本包+相关入口，定向检查，简短交接；额度中断优先恢复原Agent。源码集成冻结后完整门禁；昂贵检查只重验变化/存疑范围。无条件变化不重复失败外部检查。阻断项通过、同一Reviewer复核及J1证据齐全才标完成；随后更新治理根进度并停止B2-3。
