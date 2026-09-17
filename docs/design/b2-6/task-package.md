---
task_id: B2-6
owner: Codex
status: complete
scope: B2-6 / MP-5 only
base_head: b871b3310c501ee0c08e761520bf824df2427de4
code_head: dce035dfc5c59d82b9de704c48899319b5dc4b85
delivery_head: dce035dfc5c59d82b9de704c48899319b5dc4b85
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
  worktree: /Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-6
  branch: codex/b2-6-experience-sharing
  dirty_ref: null
coordination:
  antigravity_task: 929749fd-fc8a-4aa6-8ec7-4723b9982ca4
  initial_activation: verified_user_prompt_and_antigravity_reply
  delivery_channel: native CUA original task input; existing test application path
acceptance_environment:
  holder: null
  revision: null
  resources: []
  paused_writers: []
  release_condition: null
evidence_index:
  - gates.json
  - live-source-freeze.json
  - skill-live-final-03.json
  - sharing-live-final-03.json
  - writer-live-final-03.json
  - native-live-final.json
  - tui-live-final-02.json
  - schema-determinism.json
  - pytest-final-03.xml
  - pytest-phase45-final.xml
  - review.md
review_ref: review.md
next_action: 已完成本批；停止，不自动进入B2-7
---
# B2-6 / MP-5 任务包

记录身份：Codex；适用对象：本批所有执行者。

仅完成 MP-5.1～5.4、GUI-L4 对应增量、TUI 必要入口及 J3。以 B2-5 已合并提交为基线，主目录旧分支与改动保留。治理根内当前计划为范围依据；本树继承的旧计划不覆盖它。保留 B2-4 Host 性能限制，不重启优化，不进入 B2-7/MP-6。工作分支普通提交/推送沿用授权；未授权合并、部署或真实用户库迁移。

## 文件归属

- Codex root：公共契约、迁移/API/生成 SDK、集成、客户端业务、真实验收、文档。
- Codex skill_design（gpt-5.6-luna/max）：Skill契约、experience_skills模块/schema及定向测试已交付并释放写权；root接管集成。
- Codex sharing_design（gpt-5.6-luna/max）：sharing与移交实现/测试已交付并释放；root持有全部产品源码。
- Codex remote_design（gpt-5.6-luna/max）：Remote实现、Session绑定与上传原子性修复已交付并释放；root持有全部产品源码。
- Antigravity 新任务：COM-20260916-002实名领取，B26Presentation.tsx与b26-experience.css已提交交权；已完成实际挂载自检并交权。
- 独立 Reviewer：指定 gpt-5.6-luna/max，冻结代码后只读审查；工具未暴露 Fast 开关，因此不开启。

## 验收表

| ID | 要求与来源 | 负责人 | 验证与预算 | 状态 |
| --- | --- | --- | --- | --- |
| AC-01 | MP-5.1 procedure→Skill，固定来源/版本/hash/信任，验证发布/停用/回退，不授予工具权限 | Codex | 领域测试、正式 API、真实后续 Session 复用及来源撤销阻断 | 通过（范围见证据与限制） |
| AC-02 | MP-5.2 显式 worktree 身份，Writer 绑定 Run/branch；合并目标 tree 验证后才晋级；最严格可见性 | Codex | 隔离真实 Git workspace，未合并拒绝、合并后验证晋级、跨角色隔离 | 通过（范围见证据与限制） |
| AC-03 | MP-5.3 显式个人/项目共享与撤销、数据集移交、卸载反映实际所有权 | Codex | 授权/撤销下一发送、转移后原插件卸载不删新所有者数据 | 通过（范围见证据与限制） |
| AC-04 | MP-5.4 Remote Control 接 Memory，Target 用途/期限最小包、本地复核上传候选、Relay 无正文 | Codex | 已有加密控制/Target 适配器边界，过期/撤销/跨角色拒绝；生产 connector 覆盖如实报告 | 通过（范围见证据与限制） |
| AC-05 | GUI/TUI 正式入口、精确对象、断线只读、J3 中文联合流程 | Codex / Antigravity | 生成 Client、客户端测试/类型/构建、真实原生壳；GUI 挂载宽窄/长文本/焦点/错误 | 通过（范围见证据与限制） |
| AC-06 | Core/契约改动门禁及指定独立 Reviewer | Codex | 完整 ruff format/check、mypy、pytest、uv lock offline、diff；Schema 确定性；Luna/max 审查闭环 | 通过（含已说明的证据复用） |

## 取舍与交接

复用已安装 Skill、Memory Ledger、项目/Writer、Remote Control/Target 和 Action Gateway，避免第二套权威。子任务只跑定向测试，负责人集成后统一全套门禁；已通过真实证据按输入摘要复用。安全、数据正确性、真实验收不降级；所有未验项保持明确。证据固定源码/构建/库/窗口及模型精确 ID，凭据只在进程内注入。

## 历史检查点（以下记录当时状态；最终结论见末节）

### 集成检查点（Codex，2026-09-16）

基线正式gpt-5.6-luna/只读工具及原生壳Query已验，均不替代J3。B26正式API步骤候选/人工审阅/技能发布链、Session确定性Provider撤销下一发送与GUI/TUI定向检查已有通过证据；当前仍在集成，不提前认定全套门禁通过。SQLite v18开发DDL新增后仅对全新合成库重建，v1～v17冻结校验未改；不迁移真实用户库。Schema开发变更前创建的隔离默认库保留原样，测试使用每次新建的import库，避免把开发checksum变化当成可迁移生产升级。

Skill统一显示在原有管理Projection，经验版本和来源由B26元数据管理，正文存既有ArtifactStore；旧B23生命周期命令不能绕过精确版本/来源复核。Remote包经已注册Target的lease和能力检查进入原RemoteExecution队列，队列接收不冒称Target完成。原RemoteControl只有result_ref，故增量补加密Query正文是本批必要闭环，不用不可解引用的字符串冒充可读Query。

Reviewer原任务曾因workspace credits中断；按User继续指令优先恢复原Reviewer，无审查结论时维持未验。子Agent恢复沿用原任务与改动，未替换模型或放宽真实验收要求。

### 真实验收分段与复审（Codex，2026-09-16）

Skill分段固定全src文件hash、全新合成库/绝对workspace和精确发现模型 `gpt-5.6-luna`。`skill-live-02.json`记录正式 `ApplicationService.run_session` 读取合成文件形成经验，经B25审阅和B26 API生成/验证/发布v1、v2，停用v2并回退v1；后续无工具Session实际Context及模型回答确认使用回退Skill；撤销原canonical来源后下一发送被阻断且不新增Context。首轮`skill-live-01.json`仅脚本内部字段名误用于公共契约导致422，已保留，不记为产品通过。此分段不覆盖尚未完成的共享/Writer/原生壳联合验收。

独立Luna/max增量审查见`review.md`，尚未关闭全部问题。root已限制项目投影dataset范围、对历史Skill核对原Agent记录并重新授权当前Agent；同Session的无Agent限制历史允许正常后续任务，每次创建新快照。定向Session测试确认未撤销followup成功、撤销后阻断。Writer晋级条件在正式召回补充已注册workspace的实际clean Git HEAD/tree事实；无法证明的条件继续拒绝，不通过移除条件让已发布知识可用。Antigravity实际挂载视觉续工已送达原实名任务，预览Core/GUI为独立合成副本，仅视觉文件拥有写权。

2026-09-17 Codex续记：Antigravity已完成实际挂载自检并交权，五张截图在本目录；root复核后修正撤销徽章色优先级、中文状态和modal焦点/Esc，并经CUA实际Live操作核对。经验草稿停用/回退前置禁用，个人偏好入口复用Core返回的完整版本。Remote13项在实际v18校验、新库、无checksum hook条件下通过；不是生产HTTPS connector验收。真实Manager安装/关闭/移交/切新owner/旧安装delete保留/新owner继续写入的集成测试已通过。旧retained清理两项回归已修复，B2-3 management 16项及移交测试通过。

Tauri在本树独立`.operant/native-target`构建成功，未覆盖已安装应用；最终GUI增量后还需重建和原生操作验收。TUI实际已安装Python Client连接本机Core的Alt+4、协议协商、精确版本准备和Esc返回见`tui-live-04.json`；为headless Textual+真实HTTP，未提交写命令。首轮失败因editable安装仅映射src、SDK仍是force-include旧副本，已用`uv sync --project clients/tui --frozen --offline --reinstall-package operant-agent`刷新本地包；后续SDK再生成后应再次刷新，不能用PYTHONPATH掩盖包装问题。旧失败文件保留。

流程取舍：子轨先做领域直调而较晚核对正式入口造成返工，现把Manager/API→运行时可达性作为每条轨的先行检查。Writer目标验证不能接受任意字符串或调用方Artifact正文自报通过，正在改为Core实际核对clean Git合并目标并生成对应工件；只宣称合并结构/版本核对，不据此把推断知识提升为业务测试通过。

2026-09-17 Codex最终验收段：Writer的任意verification refs漏洞已修复，Core自行生成并复核目标验证工件，保留inferred；正式Git合并/召回/dirty及撤销阻断测试通过。原生Tauri隔离构建以CUA完成验证、发布v1、重复草稿去重、停用及回退，Core发布头1→2→3，见`native-live-final.json`。TUI12测试与刷新安装包后的实际HTTP精确命令准备通过，见`tui-live-final-02.json`；未提交TUI写命令。首次final读取因沙箱拒绝回环网络失败，放行后通过，不是产品协议错误。Schema连续两次生成未变，ruff/mypy/lock/diff通过；全pytest和最终源码真实模型复验正在进行。

固定源码提交 `bb41725d20aaee6acdef48b933c04717305978ff` 的三段真实验收均通过：`skill-live-final.json`、`sharing-live-final.json`、`writer-live-02.json`。`live-source-freeze.json`逐文件核对src hash一致；真实模型精确ID均为发现的`gpt-5.6-luna`。Writer首轮只因发现命令提前导入旧开发库而失败，尚未调用模型；改为先配置新隔离库后通过。全套pytest运行期间src/SDK/客户端/测试未再修改。

完整门禁首轮进程随工具轮次终止，系统进程核对已无pytest存活；部分日志保留`pytest-interrupted.log`。观察到的3项失败均为旧测试版本常量，分别改为未来19、当前18；仅测试提交`e707922`，产品源码与真实证据不变。全套以持久进程重跑，输出`pytest-final-02.xml`，不把中断或定向结果冒称完整通过。

独立冻结审查新增目标绑定/opaque结果与published重复验证问题，Codex接受并修复：目标须解析到当前Project登记的Core workspace；仅可核对的git结果与实际HEAD匹配才允许验证；published的重复验证只读幂等。此修复影响Writer验收，旧正常路径证据保留，但须在新源码重跑；完整门禁主动停止，待定向回归和静态复核关闭后重新冻结。

## 最终验收证据（Codex，2026-09-17）

- AC-01：`skill-live-final-03.json`，发现精确`gpt-5.6-luna`，正式ModelProfile/Session形成经验，步骤审阅，v1/v2验证发布，停用及回退，后续模型实际Context复用，canonical来源撤销后下一发送阻断。
- AC-02：`writer-live-final-03.json`，真实Git隔离Writer/合并目标、Core验证工件、正式项目晋级/后续Session复用、dirty tree及证据撤销阻断；4项Writer回归覆盖不同目标、opaque结果、重复verify无副作用。Git身份验证不把inferred升级为业务tested。
- AC-03：`sharing-live-final-03.json`，显式个人范围、未授权角色排除、授权跨项目真实模型复用及撤销下一发送阻断；Manager移交集成测试证明新所有者继续写、旧绑定拒绝、旧安装卸载不删新所有者数据。
- AC-04：Remote 13项测试使用正式Core注册/租约/队列及加密控制Query/Command，覆盖角色/Session绑定、epoch/过期/撤销、上传候选原子提交及Relay无明文。按MP-5.4明确只验本地InMemory connector与协议/适配器，不宣称生产HTTPS远程闭环。
- AC-05：Antigravity实名交权及实际挂载宽/窄/长文本/焦点/只读证据见COM-20260916-002与截图；root修复颜色/焦点并实际核对。`native-live-final.json`固定Tauri二进制/GUI hash，真实壳验证、发布、停用、回退与Core回执；`tui-live-final-02.json`为实际HTTP+headless Textual精确命令准备，未提交写命令。GUI121、TUI12与GUI类型/构建通过。
- AC-06：`gates.json`区分完整套件与测试断言修正重验。完整结果1050通过/1旧断言失败/1Docker条件skip；唯一改动是并发迁移测试把[17]*8改为[18]*8，该文件16项复跑通过。按测试身份合并后1051项通过/1条件skip，绝非一次全绿运行。核对全套运行时的所有src/tests hash，只有这一处测试文件改变；产品源码未变，按当前证据复用规则不再重复13分钟全套。原失败XML与前两次中断/主动停止记录保留。ruff378、mypy134、lock offline、diff均通过，Schema两次生成无差异。最终Luna/max独立签审已通过，无保留P1/P2，见`review.md`。

三段真实模型证据的全src hash均对应当前产品提交，见`live-source-freeze.json`。客户端代码与原生构建hash未变化；Writer私有实现保护不影响原生Skill流程，复用其真实壳证据。保留B2-4 Host性能限制；不进入B2-7，不迁移真实用户库、不部署、不合并。使用说明见`usage.md`。

结案：Luna/max独立Reviewer复核当前代码/测试提交与真实证据，原Writer验证及目标身份缺口全部关闭，J3按本批范围通过。最终Core在同一合成库重启，原生刷新确认关闭屏障，显式启用后来源恢复有效且Skill发布头仍为3。代码/测试交付头为`dce035dfc5c59d82b9de704c48899319b5dc4b85`；随后只封存文档与证据。普通工作分支推送后停止，不合并、不部署、不迁移真实用户库。
