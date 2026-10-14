# Beta 任务 6：日常组合与分发验收

> 2026-10-05；记录身份：Codex 独立验收 Agent；适用对象：Beta 任务 6。开工基线 `main@329a3bf4142913f0ccc039d7fd097b8d873904e8`，工作分支 `codex/beta-daily-task6`。本节按本轮工作树源码与真实证据记录功能验收；最终完整 Python 门禁仍待冻结 CI，不把失去句柄的本机运行算通过。范围与完成标准见[Harness 待实现目标](../Operant-Harness待实现目标.md#3-beta-完善六次任务)。

## 总需求清单

「历史验收」只表示任务 1～5 已记录的对应单项场景。本轮新增组合、六项默认能力实际效果和分发结果见 BD-01～05；各 H 项的未变路径复用对应历史证据。实现与功能验收逐项判定，源码冻结后的完整门禁另列，不把旧 CI 或失去句柄的测试当作新门禁通过。

| 需求 | 当前实现 | 功能验收 | 证据与边界 |
| --- | --- | --- | --- |
| H-01 父子 Agent | 已实现 | 通过 | 任务 1 父子树/入口、任务 2 取消与恢复；BD-01 真实父子结果。 |
| H-02 委派与通信 | 已实现 | 通过 | 任务 1/2 消息与重启边界；BD-01 真实委派、等待、定向唤醒、消费和幂等去重。 |
| H-03 可视化 Workflow | 已实现 | 通过 | 任务 3 画布、Definition、校验/差异/运行状态；BD-01 正式 Graph 完成。 |
| H-04 混合编排 | 已实现 | 通过 | 任务 3 人工/工具/脚本/Loop/合并及副作用边界；BD-01 两 Agent 与 Artifact 组合。 |
| H-05 对话建图 | 已实现 | 通过 | 任务 3 对话草稿、修改历史、差异、校验与确认发布；本轮未改该入口，复用原证据。 |
| H-06 触发任务 | 已实现 | 通过 | 任务 3 文件/Git/定时/Hook 的队列、去重、取消与恢复；本轮未改该入口，复用原证据。 |
| H-07 文件/终端 | 已实现 | 通过 | 任务 1 文件正文/Diff/PTY 与真实原生 WebView；本轮已装候选原生壳的启动/复开见 BD-05。 |
| H-08 配置继承 | 已实现 | 通过 | 任务 1 TUI 逐层来源/覆盖/恢复/CAS；BD-02 旧新 Session 冻结、TUI 与真实原生窗口配置回读。 |
| H-09 自动审批 | 已实现 | 通过 | BD-02 正式 Session 自动 Reviewer 的持久 ASK/审计、低风险准许、谨慎人工回退及硬 DENY；任务 1 原生人工审批入口复用。 |
| H-10 显式引用 | 已实现 | 通过 | 任务 1 文件/会话引用、失效与按需正文；本轮 TUI continuity-04 引用绑定正式 Context Revision。 |
| H-11 命令/Skill/插件 | 已实现 | 通过 | 任务 1/4 正式 GUI/TUI 动态命令与权限；BD-03 六 Skill 显式效果，TUI continuity-04 发现六命令并调用 grill-me。 |
| H-12 Goal/Plan/BTW/能力包 | 已实现 | 通过 | 任务 1 Goal/Plan/清单；BD-02 真实 Plan 只读、BTW 显式提升、TUI 与原生窗口回读；BD-03 六包真实产物及 BD-05 跨机安装。文件类需显式安装 `[artifacts]` extra。 |
| H-13 上下文 | 已实现 | 通过 | 任务 1 GUI/TUI 入口、任务 2 `context-real-final` 自动/手动长任务压缩与条件保真；本轮 Memory 前缀/查询更改经定向测试及 BD-04 真实组合复核。 |
| H-14 资源治理 | 已实现 | 通过 | 任务 2 归属、TTL、Pin、保留锁及清理；本轮未改资源裁决，复用原证据。 |
| H-15 扩展类别 | 已实现 | 通过 | 任务 4 Tool/Command/Event/Provider/Runtime/Capability Driver 六类正式消费与安装生命周期；本轮默认 Skill 来源冲突修复并在 BD-03/05 验证。Ubuntu 不启用未验收隔离器的第三方 Tool。 |
| H-16 浏览器/桌面 | 已实现 | 通过 | 任务 4 真实 Chrome、TextEdit、真人接管、撤销与原生 WebView；本轮候选 App 生命周期见 BD-05，未变的动作链复用原证据。 |
| H-17 跨设备 | 已实现 | 通过 | 任务 5 真实 Mac↔Ubuntu 配对、任务、断线恢复和撤销；本轮同 SHA 候选 Core/TUI 在 Ubuntu 24.04 独立安装运行，远程协议/执行入口未变。 |
| H-18 日常 TUI | 已实现 | 通过 | 任务 1 普通会话/子会话/上下文/文件/PTY；BD-02 本轮配置/Goal/Plan 连续读写及 continuity-04 正式模型普通会话、历史、引用与六 Skill 命令。 |

## 任务 6 必需场景与当前结果

| 用例 / 需求 | 输入与操作 | 必须观察到 | 实现 / 验收 |
| --- | --- | --- | --- |
| BD-01 / H-01～06 | 隔离真实工作区、正式模型，父 Agent 自主委派、等待子 Agent 并定向通信；正式 Graph 产出可核对工件 | 父子、消息、Graph Run/Node/Artifact 的持久 ID 与终态一致；不重复执行副作用 | **本轮组合通过**：`/private/tmp/operant-task6-combined-06/result.json` 使用发现的 `gpt-6-luna`，父 Agent 只委派一名子 Agent，父子回答均含 `ORCHID_61/91`；定向私信被子 Agent 消费，同一幂等键重发未新建消息；Graph Run `completed`，两个 Agent 节点输出纠正后的 `ORCHID_72/91`，并有 Artifact ID。H-03～06 的其他独立能力仍按历史单项证据复用。 |
| BD-02 / H-08、H-09、H-12、H-18 | GUI/TUI 查看并修改配置，建立 Goal、Plan、清单；运行新旧 Session；执行 BTW 并选择是否提升；在正式任务中触发 ASK 与自动 Reviewer | 逐字段来源、CAS 和冻结边界可回读；Plan 不自行执行；BTW 不暗改主会话；Reviewer 决策和人工回退留审计，硬 DENY 先行 | **通过**：`/private/tmp/operant-task6-control-real-01/result.json` 回读旧新 Session 配置冻结、真实模型只读 Plan 草稿及 BTW 显式提升；`/private/tmp/operant-task6-approval-real-03/result.json` 用发现的 `gpt-6-luna` 和独立 Reviewer Profile，直接 ASK 评估分别准许低风险读取、拒绝进程执行、谨慎模式回退人工及硬 DENY 不交 Reviewer；正式 `ApplicationService.run_session` 产生持久 ASK、Reviewer 自动拒绝与 `approval.decided` 审计，隔离 Git 暂存区未变化。`/private/tmp/operant-task6-daily-tui-01/result.json` 经生成 Client/隔离 Core 连续写读配置、Goal、Plan；`/private/tmp/operant-task6-daily-tui-continuity-04/result.json` 经同一 TUI Controller/生成 Client/正式 HTTP Core 完成真实普通 Session、历史、六 Skill 命令发现、显式 grill-me 问题及文件引用的 Run/Context Revision 绑定。候选原生窗口的配置/Plan/清单持久、旧 Session 冻结和重开回读见 BD-05。任务 1 的 TUI 子会话、上下文和终端独立证据仍按未变入口复用。 |
| BD-03 / H-12、H-11 | 在可信根发现并安装六项默认 Skill，逐项用正式入口完成受控任务 | 各 Skill 真实产生可核对内容/文件或明确发现结果；检查实际产物内容、格式和来源，记录每项缺失依赖或失败 | **通过**：证据根 `/private/tmp/operant-task6-daily-skills-real-N/result.json` 中，`N=02` 的 DOCX、`N=03` 的 PPTX/PDF/新 Skill/实际目录检索、`N=05` 的两轮 grill-me；正式模型为发现的 `gpt-6-luna`，文件类经人工批准的 `run_command` 及回读。独立重开 DOCX/PPTX/PDF 均读到预期正文；macOS Preview 实际打开 PDF 为一页且文字清晰。修复默认包保留根重定向后定向测试通过，BD-05 同一候选 wheel 在 Mac/Ubuntu 的独立 venv 均发现六包并实际运行 PDF helper。`N=01` 审批超时、`N=02` PPTX 命令失败、`N=04` 冲突历史保留原判；本机 Poppler 缺 Adobe-GB1 字符映射而产生空白渲染图，不能把该图计为 PDF 可视成功。 |
| BD-04 / 既有 Memory、H-01～06、H-13 | 同一真实任务形成并确认记忆，下一次召回，再纠正旧事实并继续完成编排任务 | 新 Session 只用确认后的新版本，旧快照边界清楚；目标/约束在压缩后保留 | **记忆纠正组合通过，压缩证据复用**：`/private/tmp/operant-task6-combined-06/result.json` 的真实父子回答用已确认 v1 `ORCHID_61/91`，纠正后的 Graph 两节点及工件用 v2 `ORCHID_72/91`。隔离库 `memory_ledger_heads` 为 `published_version=2/revision=2/state=published`、两个 proposal 为 accepted；持久的 `b24_context_memory` 中父子 Session 冻结 pack 均选 v1，Graph Run 两个 Agent 的 pack 均选 v2。本轮改了 Provider 记忆证据前缀与持久输入绑定，定向测试验证二者一致；压缩选择算法未变，H-13 长任务自动/手动压缩与条件保真复用任务 2 `context-real-final` 证据。旧轮 `combined-02` 的“未确认”误判保留为修复前失败历史。 |
| BD-05 / 分发 | 声明支持的干净机器安装并启动 Core/桌面入口；旧版隔离库升级，备份后回退，再跨机器安装 | 安装依赖可执行；启动和正式入口可用；升级保留数据；回退路径可验证且失败显式；记录构建摘要、环境、数据前后核对 | **通过**：候选 `/private/tmp/operant-task6-distribution/candidate-bundle-01` 的 Core wheel SHA-256 `ac7e9cc205983820d9e84e4de172499e23735d08ef04e03faad47aeab95da928`；Core/TUI/App 的 SHA 清单与构建输入复核通过，wheel 内六 Skill、helper、`[artifacts]` 依赖和来源保护齐。Mac 新 venv 的 Core/TUI 与 PDF helper 实际可运行；`native-final-result.json` 记录真实 Tauri 窗口 Live、配置/Plan/清单及旧 Session 冻结/复开回读，自启 App/Core 退出后 PID 与端口释放，外部 Core 未被误杀。`v20-to-v23-complete-result.json`、`v22-to-v23-enabled-result.json` 证明旧 Schema 20/22 升级到 23、四张业务表、外部 Artifact、`memory-plugins` registry 保留，旧/新正式 Session、管理和记忆检索在升级/回退后均可读，快照未变。Ubuntu 24.04 第二台主机经 SSH/SCP 安装同 SHA wheel，Python 3.12.3 独立 venv 的 Core/TUI、`/healthz`、六包和 PDF helper 生成/重开通过；见 `ubuntu-remote-result.json`、`ubuntu-identity-helper.json` 及[安装验收说明](../../guide/candidate-installation.md#本轮隔离验收记录2026-10-05)。旧 `native-01` 缺 registry 的 500、早期升级未启用插件的 409 和一次端口占用重试均保留失败历史，不计为通过。 |

## 历史证据复用边界

任务 1 的[工作台验收](../session-workbench/acceptance.md)、任务 2 的[可靠性验收](../beta-reliability/acceptance.md)、任务 3 的[编排验收](../beta-orchestration/acceptance.md)、任务 4 的[扩展验收](../extension-runtime-governance/acceptance.md)与任务 5 的[双机验收](../beta-remote/acceptance.md)分别支持上表「历史验收」。复用前按具体能力核对冻结源码、依赖/锁、构建输入、环境及正式入口，明确本轮 diff 未改变被复用链路。本轮 Memory 证据前缀及查询词采样变更已由定向测试和 `combined-06` 正式模型入口核对；它们未改变任务 2 的压缩选择与预算算法，任务 2 长任务证据仅对该算法和条件保真复用。任务 5 的最终 wheel 与双机结果只证明当时的 H-17；任务 1 的隔离原生 WebView 和任务 4 的本机操作只证明当时的桌面链路。任务 6 新增的安装/升级/备份回退、默认 Skill 实际产物与跨模块组合，不能用旧 CI、Mock、截图或安装计数替代。失败历史和 opt-in 跳过仍保留原判；只有新冻结输入的适用门禁及必要真实验收通过后才更新本轮状态。

## Beta 四项完成标准与交付门禁

本轮声明支持的环境为 macOS arm64 Host/Core/TUI 与真实 Tauri 原生壳，以及 Ubuntu 24.04 的 Core/TUI 和任务 5 已验收的私有跨机路径；不声明 Linux 第三方 Tool 的本地隔离器或 Docker 容器执行已验收。原 H-01～H-18 没有以 Docker 容器本身作为必交付能力；历史 opt-in Docker skip 保持未验收，不借其证明容器能力。

| 完成标准 | 本轮产品与真实验收判定 | 证据与剩余交付门 |
| --- | --- | --- |
| 功能齐全 | 通过 | H-01～H-18 均为已实现/功能验收通过，任务 1～5 对应单项证据和本轮 BD-01～05 覆盖原范围；新增六 Skill、自动 Reviewer、配置/Plan/BTW、分发按实际入口补验。 |
| 流程可用 | 通过 | BD-01～04 的真实父子/私信/记忆纠正/Graph、正式 Session 自动审批、TUI 与原生窗口日常配置/Plan、六 Skill 文件与命令效果均通过；历史异常结果保留原判。 |
| 运行可靠 | 通过 | 任务 2 取消/重启/压缩/资源边界、任务 4 原生操控、任务 5 双机断线/撤销，结合本轮记忆冻结和 Tauri 自启 Core 退出/外部 Core 保留的实际 PID/端口回读；不外推为所有故障零风险。 |
| 可安装使用 | 通过 | BD-05 同 SHA 候选在 Mac 与另一台 Ubuntu 24.04 的独立 venv/正式入口启动、六包与 PDF helper、Mac App 退出复开、v20/v22 升级/备份回退及业务数据保留均通过；未进行签名、公证或用户日常 App 更新。 |

上述是产品范围及必要真实场景的独立验收结论，**不等于本批代码交付门禁已关闭**。本机完整 `pytest` 曾启动但运行句柄丢失且进程已结束，没有退出码，不能记通过；源码冻结后须由同一输入的完整 CI 核对 Python 门禁并处理所有失败/跳过，其他适用 Ruff、mypy、锁、GUI、TUI、Rust、构建及 `git diff --check` 以主线程最终记录为准。当前候选构建清单明确 `dirty=true`、HEAD 仍是开工提交；提交后必须核对清单逐文件摘要与冻结提交。推送、PR、CI 和合并由主线程执行并回读，未在本文件标为完成。
