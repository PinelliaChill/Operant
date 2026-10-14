# PROJECT_ARCHITECTURE 历史记录

> 归档日期：2026-09-22；记录身份：Agent1；适用对象：所有 Agent。
> 来源：整理前的 `.worktrees/ui-install-main/docs/PROJECT_ARCHITECTURE.md`；以下保留原阶段、作者、日期、验收与限制，只有相对链接按新位置调整。
> 文中的“当前”“未完成”“未合并”均指原记录时点，不作为今天的实现状态。

返回[当前文档](../PROJECT_ARCHITECTURE.md)。

## 2. 当前完成度

### UI 全局视觉基础（UI-REFINE-20260920 第一阶段）

GUI 的主题层统一系统中文字体回退、字号/行高、文字颜色、控件高度与间距 tokens。
全局基础样式、原生 select 外观、通用按钮/弹窗、管理卡片说明及模型表单采用这组规则，
补齐既有 `b2-config-*` 类名的布局与长文本样式。空状态使用可读的辅助色与统一排版。
该变化仅涉及呈现；导航、请求、模型链路、权限与服务端状态语义不变。
范围与验证见 [第一阶段任务包](../design/ui-refine-1/task-package.md)，不代表后续页面重排或整轮 UI 交付完成。

### UI 关键页面与集中验收（UI-REFINE-20260920 第二、三阶段）

Live 对话页保留常驻项目选择，把 Thread、Session 和角色选择收纳到可展开的运行设置；
未绑定 Session 时自动展开设置。对话记录与输入成为主区域，系统事件及运行详情/文件按需展开，
审批、取消、错误、恢复和未知写结果仍由原有 Core 状态守卫控制。空态按连接、深链接和项目状态
给出下一步，失败状态不会创建会话或回退演示数据。侧栏为导航增加中文标签，减少健康连接的重复提示。

模型页分别呈现 ModelProfile、可编辑 RolePreset 与只读 AgentInstance，细节折叠但不移除；
表单采用带标签的字段组，保留 Discovery、精确模型 ID、凭据引用及 ToolPolicy 语义。
管理页标题随分区变化，技能目录和安装项采用紧凑列表及清晰的项目作用域；安装、启停、卸载确认
继续使用正式命令。没有改动后端、协议、依赖、Rust 产品源码或真实用户库。

GUI 测试、构建、独立 Tauri Debug 壳、隔离 Core 的真实模型发送、技能启停和断线恢复证据见
[第二、三阶段任务包](../design/ui-refine-2-3/task-package.md)与[验收记录](../design/ui-refine-2-3/acceptance.md)。
这是工作分支中的 UI 交付，未合并主线、替换安装应用或正式发布。

### B2-7 / MP-6 综合验收与候选交付（候选范围验收完成）

本批从已合并的 B2-6 `5b7833a` 开始；范围、冻结版本与逐项结果见
[B2-7 任务包](../design/b2-7/task-package.md)。联合故障、隔离迁移、真实三组对照、形成后带条件复用、
原生新库/升级库及最终治理回执、TUI 与安装包验证已完成；独立签审状态以任务包为准。
这是范围明确的候选交付，不是正式发布。B2-4 Host 性能限制继续保留。

`ApplicationService` 的旧 Memory 写入口统一返回 `schema_upgrade_required`；旧查询通过
`MemoryManager` 的授权与正式发布头。未接入 Manager 的显式历史版本读取仅保留只读兼容，
不再提供旧关键词自动激活或旧自动写入。`SequentialCodingWorkflow` 删除最近条目自动拼接、
任务结束自动候选写回及验证命令关键词晋级；正式 Session 使用同一插件召回链。
显式请求旧候选写开关会失败并提示升级到 Proposal/CAS。

兼容查询、单条读取和历史列表统一逐版本检查项目、dataset、当前发布/撤销、binding/permission epoch、
角色、Agent、条件、敏感级别和来源依赖。Agent 限制只能使用 Core 当前有效 Session lease 解析出的
实际 Agent；普通历史查询不能自报身份绕过。无 Manager 的旧历史读取也逐版本授权，不能用当前版本
的可见性放行更早的私有内容。

B2-5/B2-6 命令保留自己的 Action Gateway 与持久命令日志，避免被通用 M0 回复条目上限
截断精确对象 ID；首次及重放回执统一经过类型校验、敏感值脱敏和大小上限。未知写结果仍返回
`command_outcome_unknown` / `manual_reconcile`，不自动重放。

Python wheel 与 sdist 现在包含两个内置插件、生成协议 Schema 与摘要；协议协商和插件目录
优先读取包内资源，源码开发保留仓库路径。候选使用独立 wheel 安装环境验证，不能用源码目录
可运行来代替安装包可用。SQLite 仍为 v18，公共协议版本和 Schema 未因资源定位调整而改变。
真实隔离迁移只使用合成旧库副本；原用户库、正式发布、签名、公证、生产远程连接器均不属于本批动作。

### B2-6 / MP-5 经验、共享与远程边界（已完成本批验收）

本批范围与逐项证据见 [B2-6任务包](../design/b2-6/task-package.md)。实现基于已合并的B2-5；
Skill与共享已有真实模型分段，Writer正式Git/运行时链、数据集移交和原生Skill生命周期已验；
固定源码三段真实模型复验、门禁与Luna/max独立签审已完成，J3按本批范围通过。
完整套件首轮1050通过/1旧版本断言失败/1Docker条件skip，唯一测试断言修正后该文件16项通过；
产品源码未变，复用后覆盖1051通过/1条件skip，不冒称一次全绿。GUI121、TUI12及真实原生壳证据见任务包。

`experience_skills.py` 将有来源且经审阅的procedure变成只含资料的Skill Artifact，固定版本、资源hash、
来源与信任状态；验证后以CAS更新发布头，停用及回退继续校验来源和权限。它复用ArtifactStore、
Memory Ledger和原Skill管理投影，不创建第二套文件安装器；旧生命周期命令不能绕过经验Skill的精确版本检查。
技能正文不授予工具权限。`experience_runtime.py` 在正式Session上下文中加入已授权Skill与显式共享内容，
每次发送前检查来源、发布状态、角色、模型及Agent限制。历史快照保留原Agent身份，新Agent重新取快照；
同Session无Agent白名单的历史可在当前授权下复用，撤销后阻断再次发送，不删除既有Context历史。

`sharing.py` 管理显式worktree登记、精确记录授权和撤销、Writer证据及数据集移交。
Writer候选维持RunScope，Core要求成功MergeRun的目标路径对应当前项目的有效工作区登记，
核对Core初始化身份、明确的git结果commit与实际干净目标tree，再生成内容寻址验证Artifact；
拒绝调用方提供的验证refs，晋级时再次检查工件正文、hash和目标身份，才追加项目版本。
该验证只证明合并目标身份与干净树，不宣称业务测试通过，原inferred证据等级保持不变。
已晋级版本的commit/tree条件由正式召回读取已登记workspace的当前clean Git事实验证；
缺少事实、未提交改动或目标tree变化时不匹配，不能靠去掉条件让知识生效。晋级证据或登记撤销也阻断依赖。
数据所有权来自Registry；旧插件卸载不能创建对新所有者数据的清除计划，旧绑定不能继续运行。
个人偏好由显式命令保存到当前principal范围，Query返回完整授权版本；共享固定目标项目、角色/模型、
期限及epoch。正式ModelProfile/Session的跨项目复用与撤销下一发送已有真实调用证据。
数据集移交的真实Manager测试覆盖新所有者继续写入、旧绑定拒绝与旧安装delete不删除已移交数据。

`remote_memory.py` 把用途、期限、目标、记录版本、权限epoch与预算固定到最小包，并复用已注册Target的
lease及正式队列。`remote_query.py` 在原配对设备/RemoteSession签名加密请求之上返回短期加密投影，
Relay只处理opaque密文；Host接收、队列接收与Target完成分别记录。当前已验本地InMemory connector与
正式controller适配路径，不代表生产HTTPS connector已验。上传只能进入本地候选审阅，不能直接发布知识。

`api_b2_6.py` 提供独立协议协商、项目投影、事件与命令；写操作经Action Gateway、精确对象参数和持久
幂等journal，未知结果不能自动重放。Schema/两种SDK由`generate_b2_6.py`生成。SQLite v18追加本批元数据、
命令/事件和Run输入引用表，既有v1～v17校验保持冻结，非空本批表拒绝降级清除。
GUI的经验与授权面板接在正式管理页，Textual TUI通过Alt+4进入；断线只读、人工刷新核对，源码投影不是验收结论。

### B2-5 / MP-4 整理与治理（已合并验收）

本批唯一范围和证据入口为 [B2-5任务包](../design/b2-5/task-package.md)。该批已完成完整门禁、真实模型、原生治理和指定Luna/max独立审查，并合并为本树基线；未部署或迁移真实用户库。

SQLite v17 在冻结的 v1～v16 上追加治理来源、关系/依赖、候选期限/复核、维护作业与水位、命令和事件表；唯一正式发布头和不可变版本仍在 Memory Ledger。迁移拒绝篡改旧校验值，非空治理证据禁止降级清表。数据集删除在 Host 清理屏障后清理 dataset-owned 新表，原始 Item 与已发送 Context 按既有历史保留规则处理。

`memory_plugins/governance.py` 提供项目 canonical Item 历史搜索与按需展开，固定 Cursor 分页，区分当时记录和当前约定；不读取或复制私人 Mailbox。治理来源仍校验当前项目、版本摘要、可用性及权限；原始来源被不同记忆摘要引用时递归去重，不增加独立证据数。冲突/替代关系只记录精确版本，Proposal 未确认不移动发布头。有效时间与复核期限在服务端检查，来源撤销传播到派生依赖；正式 Runtime、Host 授权和兼容查询均阻断失效来源。已有 Run 的知识截止点仍可引用历史版本，但当前撤销与时效优先；已发送内容保留，污染历史的后续发送失败关闭。

B2-5 批量审阅携带精确 proposal_id、proposal_revision、proposed_version（含摘要）和 base_head_revision，先核对整批再同事务接受/拒绝、写审阅审计，不能部分确认。接受前重新检查复核期限与来源；未解决conflicts_with拒绝发布，supersedes接受时原子退役目标head，目标已变化则整批回滚。过期或head已变化的精确候选仍可被用户拒绝。B2-3 老确认入口拒绝带治理元数据的候选，要求使用新契约。人工修正同样先形成候选；后台语义提取固定为 inferred，不可自我确认。

`memory_plugins/maintenance.py` 通过已发布的有限单节点 Workflow 和原 Scheduler RunRequest/租约调度执行。每个 ModelProfile 的定义锁定其摘要；仅 B2-5 已登记的精确 RunRequest、Workflow版本和完整快照可进入维护执行器，随后仍经过原 Graph Action Gateway。发布定义含明确维护执行标记，已入队但登记缺失时进入manual reconcile，不回退普通Graph；未知模型结果不能因数据库提交幂等而自动重试。真实 GraphRun/NodeAttempt 保存运行与终态；不存在以 Relay/本地任务ID冒充 GraphRun 的路径。源码/插件包、绑定与权限 epoch、Host配置、模型、来源 Cursor 与预算在作业创建时固定，Host RunLease 跨越模型调用，在提交前重新验证；候选版本/Proposal、来源依赖及处理水位同事务提交。

维护模型通过无工具正式 Session 调用，维护自身 Thread 从后续提取来源中排除。有限来源批次只推进到实际处理截止点，无新来源不调用模型或产生例行候选通知。前台已有 Session 租约时后台让步，维护调用全局并发为一，输入预算在模型调用前检查；Token按实际usage单独计量，未知美元成本保留未知。失败不改写原始任务结果，未完成提交不推进水位；失败与取消使用持久 Scheduler/Graph 状态及原 DLQ 显式重试，关闭/重开不能使旧租约恢复有效。默认 Host 后台开关关闭，B2-5治理入口显式设置；旧插件配置中的维护关闭也同步到Host开关。

Additive `b2-5.v1` 由 `api_b2_5.py` / `contracts/b2_5.py` 生成 OpenAPI、digest、Python/TypeScript Client。Query包括当前知识/候选、历史/详情、增量治理事件和实际Context的当前来源/时效影响；副作用命令经 Action Gateway 与持久幂等journal。事件只包含对象引用与动作，重复命令不重复通知，未知写结果不能自动重放。完成回执与事件同事务；业务提交后崩溃、回执未完成时，持久journal及Projection显式保留待核对command id，启动恢复一次性发出outcome_unknown事件，不推测成功、不重放业务。GUI通过生成 Client 接入候选/冲突收件箱、精确批量确认、历史、来源/有效时间、后台开关/作业/Token/DLQ；断线保留只读投影，状态和确认范围以 Core 为准。上下文检查器保留原始实际发送内容，另显示当前失效或冲突提示。

真实 `gpt-5.6-luna` 验收已证明后台候选、人工确认、正式 Session 召回、无新来源零调用和撤销下一发送阻断；当前证据 `live-07.json` 固定全部src摘要，原始失败与脚本误判仍保留。原生治理、实际上下文及精确替代批量差异走查见 `native-acceptance.json`、`native-review-acceptance.json`；完整门禁1017通过、1 Docker条件跳过，见`gates-final.json`。初审3项P1/4项P2已修复，原Luna/max差异复审确认闭环且无新增P1/P2，见`review-final.md`；后续改动按输入摘要核对证据适用范围。

### B2-4 / MP-3 本地交付（含明确性能限制）

唯一状态与验收入口为 [B2-4任务包](../design/b2-4/task-package.md)。B2-3的已完成事实保留；下述是当前源码，不能替代最终J2、性能门及独立审查。

SQLite v16 在原v15之上增加发布事件历史、trigram FTS索引、Run Manifest和Context Memory使用附表，保留v1—15的冻结校验值。旧库导入时只将当前head作为迁移截止点可知事实，不推断更早发布历史。Memory Ledger每次发布/停用由同事务trigger保存序号；Run按截止点选版本，但当前停用、删除、撤销与权限仍阻断。

`memory_plugins/retrieval.py`将任务拆成有限中文短词、标识符和普通词组；`MemoryManager.search`批量合并FTS/短词候选后按scope、role、agent、sensitivity、条件、来源和历史发布状态复核。候选去重后再排序并限制同来源占比。`recall.py`通过实际PluginHost调用安装的引擎，自动召回和显式引用合成同一Memory Pack；Session、Coding Workflow及本批Graph Agent经同一正式Session入口调用。Manifest事务合并各Session所用版本并保留较新的显式刷新，每次发送重新核对该Session已有历史权限；外部新增知识不进入既有Run；安全回合结束后可显式刷新或移出后续请求。撤销污染无法可靠剥离时拒绝继续旧上下文，需干净Baseline；不改写已经发送的历史。

Context Composer将普通记忆作为明确不可信证据加入实际Provider消息，记录包版本/来源/条件/原因；Memory使用附表与ContextRevision在同一事务保存并核对正文一致性，冻结旧Context协议保持原结构。新的`b2-4.v1`检查器Query返回最近50个typed扩展记录；共享Graph清单的控制在Graph终态执行，运行中拒绝刷新。总预算包括技能、历史、工具Schema和包装/输出预留；`token_counting.py`仅在模型ID精确匹配且有完整Provider wrapper时报告精确，否则明确保守UTF8估算，模型切换重新计数。

`api_b2_4.py`补充实际模板/Team/Role目录、从角色创建版本化编排及自动Roster绑定，新增命令走Action Gateway与持久幂等journal。GUI使用生成B24Client，协作页提供模板/成员选择、编辑发布运行、定向/群聊、任务/工件板与Agent信息；会话检查器显示实际Context/Memory Pack并控制后续选择。`BoundedGraphExecutor`首期驱动明确标记的Agent节点图，绑定真实Session/Thread/Agent与NodeAttempt，支持独立节点并行及依赖输入；其他节点在此驱动入口明确拒绝，不宣称通用Graph全节点执行。运行合计预算和节点预算分别约束，Team终态写入服务端投影。无记忆插件时普通任务可运行，声明强依赖的Graph启动拒绝。

Graph 重试保留原 Session/Thread，分配新的 Agent 并收窄剩余预算；旧 Roster 留作历史，新 Roster 与任务板受让人在同一事务更新。每次 Provider 请求重新读取该成员可见的消息、任务与工件，折叠工具结果按 tool_call_id 匹配；已终止成员不能接收新消息。工件发布的非空 recipient_ids 对应 recipients 可见性，空列表对应 Team 可见性。

协作目录在服务端按显式workspace过滤Graph运行，每页最多100条摘要，稳定Cursor携带前页排序值以继续读取；没有workspace时仅返回既有定义目录，不返回全局运行。GUI可加载更早运行并去重，正式B2-4运行不依赖legacy关联。摘要提供状态与Team关联，不返回运行输入/输出。生成契约digest为619168f06db1d9df2ba24403a3c29d662a6daa467a4a8d0d065693d04ade1933  operant-b2-4.openapi.json；目录分页已通过定向API/GUI检查，原生已通过第二页找回真实Graph/Team；Team仅通过显式按钮准备，已绑定或已结束的Graph禁用准备，Enter不重复提交。工作区过滤是发现范围，不额外宣称多租户授权隔离。

B2-4 当前补验：固定源码下正式gpt-5.6-luna/low在同Session验证显式记忆进入请求、移出/刷新后新Pack生效，撤销已使用记忆后在Provider前PermissionError停止，保留历史而不重放污染上下文。默认memory_plugin_mode=True且零安装时普通Session成功，Graph强依赖memory-standard明确拒绝。证据见本批j2-current/nextsend-real.json；此证据不替代Host性能或最终独立审查。

B2-4 命令使用自己的持久幂等 journal，避开通用投影条目数裁切；当前结果以 b24-public-result.v1 包络保存已脱敏的类型化投影，旧 journal 在重放时脱敏。空 Idempotency-Key 拒绝，重放返回同一公开结果及重放响应头。

标准记忆插件的可信进程内召回使用Python值传递，省去JSON值往返；请求和结果仍dump后按完整Schema重验，model_copy/model_construct产生的非法嵌套字段不会直接接受，配置、取消与正式Host检查保持执行。

Host继续检查当前租约、epoch、认证、包身份与来源授权；包digest缓存按完整目录inventory及device/inode/size/mtime/ctime/mode/nlink变化失效，正文变化重新流式hash。认证状态每次复核，校验过的不可变metadata按包digest复用，不扩大权限。保留目录逐项身份核验，未采用跳过检查的性能捷径。

本批已有 gpt-5.6-luna 正式 Session/Graph 单、双 Agent 记忆与只读工具调用的历史验收记录。原临时实施树和环境缺失后，源码已恢复到持久隔离工作树；恢复清单中的 172 个 Core/SDK/工件增量文件及 GUI 主入口产物与原版本哈希一致。历史数据和截图尚须按证据索引核对，不把恢复过程当作新的真实模型验收。当前恢复树已通过完整基础门禁969项、1项Docker条件跳过，GUI110项/typecheck/build及SDK确定生成；证据见本批gates/current-04-results.json和j2-current/evidence-index.json。当前固定环境J2单/双Agent、原生GUI及下一发送补证已经完成；gpt-5.6-luna/max独立Reviewer已关闭原目录P1/P2，当前产品增量无新增P1/P2，见review-current-closure.md。

性能历史报告保留集召回率0.9167高于旧基线0.75、禁用样例零泄漏，但Host性能门尚未满足；工件可见性增量的独立审查已关闭，见本批 review-artifact-closure.md。性能脚本将timing、allocation、observation放入独立库/Registry/Host；正式时延/CPU不启用额外计时或RPC编码统计，分配轮只增加tracemalloc，观测轮单独报告重建字节与调用计数。配置/POLICY、逻辑请求、每个结果和所有重复轮次的forbidden命中互校，未改冻结阈值，也不扣除观测耗时。该重构经独立审查和18项回归验证，见[观测分离验证](../design/b2-4/gates/observation-split-results.json)；旧失败报告仍保留。当前产品性能门仍未通过，C完整扫描仅为诊断原型，尚未接入产品。2026-09-15 User指示停止微小性能优化，本批按[性能限制决定](../design/b2-4/performance-scope-decision.md)收尾；功能/J2/独立审查完成，原Host性能比例未达仍如实保留，详见[交接](../design/b2-4/handoff.md)。当前没有推送、合并、部署或迁移用户库。


### B2-3 / MP-2 记忆与管理集成（2026-09-13，已完成本批验收）

范围与门禁见 [任务包](../design/b2-3/task-package.md)。以下描述当前源码；真实 gpt-oss-20b 正式任务/read_file/Context 与原生历史已通过（见本批 model-context-acceptance.json）。J1 生命周期/删除、基础管理、宽窄与错误重连已完成；冻结代码4678b40的完整门禁及指定Luna/max独立复核通过，见本批handoff.md与review-closure.md。

`memory_plugins/manager.py` 通过正式 PluginHost 安装目录中的两个独立包。`memory-standard` 使用来源提取与 Host 搜索，`memory-notebook` 使用键值笔记；认证进程内支持独立私有索引，隔离模式与索引重建通过Host受控读取当前发布版本后执行键名精确过滤；配置分别来自包内 Schema。两者共享 MP-0 Host DTO 与包内标准库 SDK，支持认证进程内与未认证隔离 stdio。Core 负责来源授权和唯一发布 head，插件不能自行发布、越过 scope 或把候选当成正式召回。

SQLite v15 增加 dataset-owned `memory_ledger_*` 与 `b23_*`，不改 v1—v14 migration checksum。Ledger 保存不可变版本、Proposal、CAS head、完整请求幂等摘要和来源。显式用户保存记录为 `user_asserted`；修改提议等待确认，旧版本保留。来源保存为当前项目的 canonical Item；旧数据显式迁入时标记 `legacy_unverified`，Core 映射 scope，旧 payload 不得覆盖。

HTTP/CLI 正式启动启用插件记忆模式：旧记忆写入口明确要求升级，旧读入口惰性初始化 Manager 并只代理已启用项目的已发布记录。旧 Workflow 的自动记忆注入与候选生成关闭；自动召回、压缩和索引调度优化属于 MP-3，尚未实现。直接构造 Python Service 的旧兼容模式仅保留历史调用与测试，不代表生产默认策略。

全局关闭先禁止新访问，再收集 Host 停止回执；未收束 Run 或插件返回 blocked/restart_required，不能报告全部完成。项目关闭、切换、归档与卸载同样经过停止屏障。重新开启只恢复显式操作能力，不补扫历史。keep 卸载保留 dataset，允许独立导出、同插件重新安装接回或显式删除。delete 必须先持久化清理计划，经 Host 资源/活动 Run 屏障后，Core 才清除该 dataset 的专属版本、提议、来源副本和结果缓存；保留 dataset tombstone。普通 Ledger delete 不提供物理清理权限，未知/共享/受保护资源保持阻断。历史 Item、Context、Skill、Artifact 和外部导出副本不随 dataset 删除，也不宣称磁盘安全擦除。

`api_b2_3.py` 提供协议协商、管理 Query 与 typed Command。每个命令经过 Action Gateway；自己的幂等 journal 保留完整业务结果，删除数据后旧数据结果明确不可再取。响应保留类型结构并脱敏，超预算显式失败。`generate_b2_3.py` 从同一 Pydantic/FastAPI 源生成 `b2-3.v1` OpenAPI/digest 与 Python/TypeScript Client；CLI `operant memory manage` 使用生成 Client。

GUI 的项目、知识、插件、记忆设置、Skill、保留与审计页消费同一管理投影。项目注册绑定绝对 Workspace，编辑、归档或解除关联不删除源码。设置按作用域与字段保留值/来源实际变化的独立时间，旧记录无法追溯时显示 unknown；Role 使用其不可变版本的创建时间，变更只影响新 Session 快照。解除关联只清除所选记忆插件，不改变归档状态；归档由独立命令负责。Skill 经发现、显式安装、项目启用、停用和卸载，安装副本校验 digest；运行时以单独 guidance 加入正式 Context，不扩大 Tool Policy。Artifact 页调用既有 Pin/归档/宽限期/Trash/恢复与审计，并呈现正式审计扫描数和发现详情，保留受保护引用屏障，不提供清空全部或物理 purge。

### B2-2 / MP-1 与基础任务接入（2026-09-12，已完成本批验收）

本批从 `8851a23` 独立实施，验收范围和当前证据见 [任务包](../design/b2-2/task-package.md)。
`src/operant/plugins/` 提供显式安装与 Registry、受控认证记录、配置绑定、Run fencing、资源登记及
keep/delete 清理续做。Registry 使用单写文件锁和原子 JSON journal；重启使旧活动 lease 失效，不重放
RPC或自动重装插件。此版本不改 SQLite v14，不实现 MP-2 记忆引擎、数据迁移或新召回。
Run释放按lease身份和fencing清除活动标记，终态释放幂等；迟到旧lease不会解除新Run的停止/卸载屏障。

插件共用 `contracts/b2_1.py` 的 typed Host API。认证进程内方式加载已核对包的 `create_plugin()`；
它是认证信任边界，不是沙箱。未认证 stdio 使用 macOS sandbox-exec、独立目录及隔离Python参数
`-I -S`，启动前真实检查受控 Home 文件、目录外写入与回环网络拒绝；沙箱不可用则拒绝。
受管私有索引的读写删与资源登记逐级持有目录句柄并拒绝符号链接；写入前校验普通文件及单链接，读取按响应预算限量，防止路径校验后中间目录替换与无界读取。首版依赖限定为标准库/包内代码，不安装环境任意依赖，不宣称跨平台沙箱或线上CA。
包、依赖/权限文件指纹、认证撤销与epoch在调用/提交时复核；资源预算、取消、迟到拒绝和未知清理
状态不因cleanup Hook缺失而绕过。stdio 复用进程按并发准入、RPC超时、采样RSS/CPU执行预算；空闲复用不视为请求超时；超限停止独立进程组并标记失败。采样允许短暂超调，认证进程内插件仍是合作式资源边界。当前issuer、scope、存储lease与全局启用状态在Host回调入口复核。`create_app(plugin_host=...)` 是显式受信任启动注入面，由Core负责
shutdown关闭；普通启动默认无Host/无引擎，插件HTTP管理与默认记忆绑定仍属B2-3。
Host回调要求Core提供来源/记忆引用授权器，按真实记录核对dataset、scope、revision/digest和可用性；缺少授权器时拒绝。
Host另行强制来源与当前scope/permission epoch一致、记忆引用属于当前dataset，模型Profile仅来自绑定的提取/重排配置。
异步回调返回后重查lease及来源授权并验证结果关联。MP-1不隐式授予跨scope来源；尚未接入生产记忆检索/模型回调。
直接engine入参及返回值也经过同一Core授权边界：来源、记忆引用、Head与Proposal逐项核对，缺少对应授权器拒绝；结果须关联request/event及watermark。
Manifest能力限制嵌套Host API：recall才可search，extract/recall/maintain可请求授权来源。模型Profile还绑定当前engine操作：extract仅可用提取配置，recall仅可用重排配置；同时声明两能力也不能跨阶段使用Profile。仅索引通知不授予外部读/搜索/模型访问。
停止、卸载和Host关闭先阻止新运行，再对在途调用及关闭/清理钩子作有界等待。未收束的可信代码保留task/engine/资源并返回restart_required，禁止同Host重新启用；同步关闭/清理钩子在工作线程执行，不能物理强杀线程。清理使用目录句柄递归删除，不跟随symlink；路径异常形成可续做blocked条目。
同一安装的stop/uninstall/resume_cleanup使用生命周期锁，Host关闭自身也互斥；关闭期间排队的新请求返回明确restart_required回执。绑定配置在入口生效：maintenance_enabled关闭则拒绝maintain，recall及嵌套search不得超过recall_token_budget；默认零预算允许零分配请求，实际召回内容编排/后台调度仍属后续阶段。

`api_b2.py` 将已提交 Session/WorkflowRun 投影为保留来源身份、动作与明确Workspace关联的任务。
精确任务Query不受列表分页影响，跨来源同ID必须消歧。历史来自canonical Item、AgentInstance与
不可变Session snapshot；已绑定当前Session的Thread在正式运行时保存用户/模型消息、工具及生命周期事实，Runtime Event与对应Item同事务提交。每轮固定此前Item cursor，避免当前轮历史重复进入上下文；普通引用Thread保持只读，旧事件不批量迁移。未绑定Thread不生成消息，Task不伪装成TeamTask。
Agent创建失败时保留无Agent的`session.run_failed`事实并在绑定Thread写入系统Item；Task按该失败与后续新Agent的时间顺序显示失败或新轮状态，不伪造Agent行。
Additive `b2.v1` 从FastAPI/Pydantic经 `sdk/protocol/generate_b2.py` 生成Schema/digest及Python/TS Client，
补模型/角色配置、已登记Workspace首个Thread创建、任务/历史与取消；旧五协议和MP-0契约不变。安装环境可用绝对路径
`OPERANT_B2_SCHEMA_DIGEST_PATH` 提供digest，不用常量伪造协商。

GUI通过生成Client接入模型/角色配置、分页AgentInstance及真实状态、Task/Run详情、分页历史和取消；Core Projection与epoch清理
防止旧请求覆盖新选择。取消终态从Session Task读取，Thread生命周期不冒充运行状态。AgentInstance/Task投影优先读取已提交的Agent终态事件，避免流关闭中断cleanup后显示过时running行；cleanup状态更新放在不可跳过的finally内。历史Query逐字段脱敏保留类型结构；只读历史错误不创建命令结果未知状态。模型ID来自Discovery，secret_ref只存引用名；新Role默认无工具权限，编辑
既有Role不改变tool_policy；Role新版本不回写已创建Session快照。取消accepted是请求接收，不能
标为执行完成。Workflow详情链接既有Graph监控/安全恢复，仍保留未知写入人工核对边界。
会话取消按钮读取B2 Task的服务端动作权限，Agent启动/审批/终态后刷新；已结束或无活动lease时不因Thread仍active而启用。
Antigravity的任务行样式来自 `a0a4a5d`，Codex按原生窄屏结果补断点修复；B2配置表单使用Modal显式portal，默认其他调用不变，Live对话历史按正常文档流避免窄屏重叠。HTTP开发WebView使用同源Vite代理，正式Tauri协议及
`tauri.localhost`保持固定本机Core；`OPERANT_CORE_URL`仅控制开发代理目标。

本批真实桌面、完整门禁和指定Reviewer已通过，代码518bb3f；准确覆盖与未覆盖项见本批交接和验收记录。
B2-3/MP-2、项目CRUD、后续Graph/Team交互及发布签名不在本次授权范围。

### B2-1 / MP-0 增量（2026-09-09）

基于 main `ecb0043` 新增离线契约 `src/operant/contracts/b2_1.py`：Project/Workspace、Task 来源、
Agent 配置与实例、插件数据集/资源/认证、Memory Head/Proposal/CAS、Manifest/Context 使用和 Host RPC。
`sdk/protocol/generate_b2_1.py` 从同一 Pydantic 源生成两份独立版本的 JSON Schema/digest 与
Python/TypeScript Client 接口声明；**未注册运行 API、未实现 PluginHost、未执行用户库迁移**。
当前数据库仍为 SQLite v14；实际 Beta 协商字符串为 `beta.v1`，现有五个协议保持不变。

源码核对、迁移映射、合成 fixture、旧直接策略评测与限制见 [B2-1 契约边界](../design/b2-1/contract-boundaries.md)
和 [源码基线](../design/b2-1/source-baseline.md)。新契约 workspace 引用只返回 hash；旧 ProjectProjection
仍可能返回绝对 workspace_ref，不以新契约声明反推旧实现已完成路径隐藏。
本批完成状态及验证结果见 [交接](../design/b2-1/handoff.md)，不得将契约准备等同于 MP-1 或产品验收。
GUI-L0 集成 Antigravity `ce2ab66` 的嵌套路由、旧深链和 Demo Hook 隔离，模式切换清空演示选择。
旧 HttpClient 的合成 Thread/消息/Context/审批/Graph/Workflow/Remote 与伪造 SSE Cursor 路径显式失败；
已有协商生成 Client 继续承载受支持 Live 功能。真实 Tauri debug WebView 已验证本批隔离、别名、嵌套深链与刷新，见 [原生证据](../design/b2-1/tauri-native-evidence.md)。
Core 预检失败明确显示；本批没有成功模型链路或打包发布产物验收。



### 已实现

- Pydantic 领域模型；
- Model Profile 创建、查询、更新、停用和健康检查；
- Role Preset 创建、复制、停用和版本记录；
- Main、Planner、Explorer、Coder、Reviewer 默认角色；
- 不可变 Role Snapshot；
- 会话级模型、effort 和 budget 覆盖；
- Session、Agent 和 Event 的 SQLite 持久化；
- 正式 Thread、Turn、Item Canonical History：父子关系、Workspace 绑定、终态/归档、稳定 Cursor 与
  Thread 内 position；Turn/Item 只追加且不可原地改写或删除；
- User Message、Agent Message、Tool Call、Tool Result Ref、Artifact Ref、Approval Link、Steering 和
  System Event 八类类型化 Item；
- 内容寻址 Artifact Store：SHA-256、media type、size、sensitivity、source refs、retention policy ref，
  原子写入、并发去重、读取校验和路径/软链接边界；公开领域对象与 API 不暴露 storage key 或本地路径；
- Artifact 内容通过短时、对象级、操作级 capability 执行完整性校验后的脱敏文本读取、原文下载和
  绑定 Workspace 目录身份的显式导出；HTTP 不签发 capability，物理修改还要求独立 trusted bootstrap；
- 对象级 Artifact Retention Policy、Pin、归档、宽限期、计划删除、可恢复 Trash、恢复和显式物理删除；
  Canonical History、Approval/Audit/Memory、可恢复执行与未知副作用证据作为保守删除阻断项；
- Artifact Store 与 SQLite 引用的零写审计，识别孤儿、缺失、损坏、非安全对象和已删除内容残留；
  修复只接受重新核验后仍成立的精确 finding，并继续使用 M0 Receipt/Action Hash/人工核对边界；
- Provider `CacheObservation` 只追加记录 hit/miss/unknown、显式 Token、请求/前缀 hash 和失效原因，
  不复制 Provider Cache；缺失 usage 保持 unknown，不按 0 处理；
- 每次模型请求前生成不可变 `ContextRevision`，保存实际发送的安全 Message/Tool 快照、版本化
  `PromptLayout`、有序 `PromptBlock`、类型化 Reference Binding、动态 Context Watermark 与来源证据；
- 追加式 Compaction 与可恢复 Tool Result Stub：压缩记录只覆盖同 Agent 已提交的 ContextRevision
  Cursor，或同 Thread 中按真实 `items.sequence` 精确列出的 Canonical Item；不删除或改写 Canonical
  History；大 Tool Result 先进入内容寻址 Artifact，再向模型提供安全 Stub；
- Phase 1B 最小类型化引用：Thread、同 Thread Item、普通 Artifact 和当前 Session/Workspace 可读的
  active Memory；引用在 Provider 调用前完成作用域、敏感级别、完整性与 redaction 校验；
- 冻结版本 `phase1d.v1` 的 Slash Command Registry，把 `/init`、`/review`、`/清空上下文`、
  `/压缩上下文` 及英文别名解析为类型化 Command；Registry 只描述路由，不授予 Workspace、工具或
  审批能力；
- `/init` 只校验并登记一个绝对、存在且可读的 Workspace 到当前 Core SQLite，不创建第二套数据库、
  不复制 Secret/Role/Model；公开 API 只返回身份 hash 和可读写事实，不返回真实本地路径；
- 追加式 Context Baseline：清空只把后续 Context 的起点推进到当前 `items.sequence`，压缩先保存精确
  `THREAD_ITEMS` Compaction 再推进基线；两者都不删除、不更新或伪造 Canonical Thread/Turn/Item；
- `/review` 创建严格只读 Reviewer Session，仅开放 `read_file/search_files/git_diff`，复用 Session
  预算、租约、取消和 Action Gateway，并把结果保存为不可变 sensitive Review Artifact；
- BTW Sidecar 使用启动时已提交 Item Cursor 的冻结视图、独立 Agent/ContextRevision 和空 ToolPolicy，
  不取得主 Session 执行租约、不写主 Session Event/Thread；只有显式 promote 才原子追加一个 Steering
  Turn/Item，重复或并发 promote 返回同一结果；
- Phase 1E 冻结 `phase1e.v1` 单一 OpenAPI Schema，离线生成 TypeScript/Python 类型和 Client；正式 live
  面只保留 8 个 operation，统一版本协商、Schema digest、Receipt、幂等键、类型化错误、`int64`
  Cursor、SSE 增量解析和同 scope 回放，重复生成可得到完全相同的产物；
- `GET /v1/protocol` 严格协商版本和生成物 digest；`GET /v1/projects` 将已登记 Workspace 聚合成只读
  Project Projection，`GET /v1/workspaces/{workspace_id}/files` 使用逐组件 no-follow 校验返回有界、安全的
  目录 metadata，不返回正文、绝对路径、inode 或敏感文件；
- `POST /v1/sessions` 可选接收 `thread_id`，提供时在一个 `BEGIN IMMEDIATE` 事务内创建 Session 并写入
  唯一 `thread_legacy_refs(session)`；不存在、非 active 或已绑定的 Thread 会安全失败且不留下孤儿
  Session，未提供该字段的旧 CLI/API 行为继续兼容；
- React GUI 已建立明确分离的 Mock/live 模式；Phase 1E 表面只调用冻结的生成 Client，Graph/Team 表面
  只调用 additive Phase 23 生成 Client，均经同源 `/v1` 连接 Core。请求或重连失败会显式显示，
  不静默回退或混入 Mock 数据；
- Phase 2 新增 Graph IR 与 Compiler：严格区分 Draft Definition、Published Revision、Graph Run、
  NodeRun 和 NodeAttempt；校验端口、边、条件、Loop 上限、并行/递归/子 Agent 上限、写入幂等等级和
  插件/Scheduler 排除项，发布后的版本不可变；
- Graph Runtime 持久化运行、节点、尝试和只追加事件；以已提交输出做 fixed-point 依赖推进，明确
  区分未选 Condition 分支、ALL/ANY Join、timeout/Loop limit 路由，并在开始 Attempt 时强制
  `max_parallel_nodes`。节点进入 Human Input/Approval 等边界时会聚合检查其他 READY/RUNNING/
  RETRY_WAIT 节点，不会把可运行的并行兄弟一起冻结；只有没有其他活跃节点时 Run 才进入等待。
  Entry、实际下游输入和成功输出都会在任何状态写入前校验 required port、声明类型和有限 JSON；
  缺失的可选源输出会禁用对应边，不以 `None` 冒充值。Condition DSL 只按 Token 转换布尔字面量，
  不改写字符串内容；Loop 成功输出与时间、Token、费用、子 Agent、递归遥测均 fail-closed 校验。
  Graph Timer 节点仍保留为 IR 枚举但 Compiler 拒绝；Phase 5A 已实现与 Graph 节点分离的
  Cron/一次性 Timer Scheduler，它只调度已发布的 Graph Workflow，不是通用节点执行器；
- Graph 恢复只从已提交事实重算派生 READY/SKIPPED 状态：Attempt 已提交成功但尚未推进下游的崩溃
  窗口可恢复；幂等未知结果沿用首次逻辑动作键安全重试，未知非幂等写节点进入
  `manual_reconcile_required`，API 不提供强制重放开关。取消、失败或预算超限会在同一收口中终结其他
  未完成节点；迟到的边界输入不能复活终态 Run，同时保留未知非幂等副作用的人工核对状态；
- 既有 `SequentialCodingWorkflow` 已迁移为 Graph Runtime 的兼容协调入口。每个 legacy WorkflowRun
  绑定一个 GraphWorkflowRun，Planner、Explorer、Coder、Reviewer、Main 和有限返工都形成 NodeRun/
  Attempt；Attempt 只绑定实际持久化的 AgentInstance ID，不用 RolePreset ID 冒充。Coder 在调用现有
  Application Service 前记录副作用开始，仍由 Action Gateway 执行工具；
- Phase 3 新增本地 Team Definition/Run、Roster、单条 canonical Message 与逐接收人 Mailbox Delivery。
  消息正文按显式接收人投影为不可信上下文；消息或 `ApprovalRequested` 通知不能改写 Graph 状态或
  代替 Approval 决定；
- Team Run、初始非空 Roster、Team Event、Graph 绑定回填与 Graph Event 在同一事务创建，并以
  `BEGIN IMMEDIATE` + `team_run_id IS NULL` CAS 保证单 Core 下一个 Graph 只绑定一个 Team；冲突、
  并发竞争或后续写入失败会整体回滚。该入口受 `max_active_agents` 约束。消息时间线要求明确
  viewer，在 SQLite 分页前过滤：定向消息仅发送者与接收者可见，owner/audit 行不进入普通 UI；
  Task/Artifact Board 使用 revision 和幂等键更新，Mailbox Ack 持久保存首次事实，同 key 重试返回
  完全相同的 Ack；
- 新增 `phase23.v1` additive OpenAPI Schema 和确定性 TypeScript/Python Client，共 25 个 Graph/Team
  operation；两个生成器共同维护兼容的 Python 包入口，Phase 1E 的 Schema、digest、专属 TS/Python
  Client 文件和 8-operation 行为保持逐字冻结。Graph/Team Event 固定公开 `event_id`、
  `schema_version=phase23.v1`、资源内 `run_sequence`、Cursor 和 scope；Graph 先创建，Team 再通过唯一
  原子入口绑定，`StartGraphRunRequest` 不暴露无法成立的反向 Team 输入；
- React GUI live 模式已接通 Graph 定义/启动、运行节点与 SSE 监控、legacy Coding Workflow Run 映射，
  以及本地 Team/Roster/消息/Mailbox Ack/Task/Artifact Board；Core Projection 与 Cursor 仍是权威，
  错误或断线不会回退 Mock。切换 Graph Run、Team Run 或 viewer 时先立即清空旧 scope 投影，再以查询
  epoch 丢弃迟到 resolve/reject，避免旧运行状态或定向消息短暂泄漏到新 scope；SSE 正常 EOF 后先做
  权威 Query 校正，只有已持久终态回到 idle，非终态 EOF 明确显示断线并保留 Cursor 重连语义；
- React GUI 通过生成的 `Phase45Client` 与同源 `/v1` 接通 Policy dry-run/解释、每个 Action Hash 最多
  100 条的安全审计事实投影、受信根 Skill 候选发现与列表，以及 MCP Server 配置/启停/删除、工具快照、
  工具调用和 durable receipt 查询；stdio 表单只能选择 Core 返回的 root ref，并校验相对 cwd、JSON argv
  与 digest-pinned Docker image。Skill 始终标为未信任候选，MCP 副作用继续由服务端 Action Gateway
  裁决，客户端不直接执行命令、不解析 Secret、不把 ASK/DENY 当作允许。Phase 45 ASK 显示绑定事实，
  允许后复用原稳定 Idempotency-Key 重试，拒绝后不执行；`outcome_unknown` 只允许人工核对。Live 加载、
  空、错误和断线状态不会回退 Demo，危险生命周期操作要求明确确认；
- Phase 4 安全控制面把 principal、tool/operation、规范化 target/arguments、capability、Secret Ref、
  sandbox/network profile、幂等等级、Policy Version 和幂等键绑定为稳定 Action Hash；
  分层 Policy 按 `DENY > ASK > ALLOW` 合成，System hard DENY 不可覆盖，无规则命中默认 DENY。
  `ApprovalReviewerAdapter` 只能把可评审 ASK 收口为 ALLOW/DENY，超时、异常或非法输出一律 DENY，
  不能覆盖 hard DENY；
- Capability Lease 绑定精确 Action Hash、principal、capability、target、Policy Version、TTL 和使用次数，
  消费用 SQLite CAS 防止过期、撤销、越目标或重复使用；Secret Broker 仅在已 ALLOW 的精确 Action
  执行时把环境变量名解析成短时 Secret Lease，真实值不持久化，输出再次脱敏；
- Skill Discovery 只扫描 Core 启动时配置的有界绝对受信根；从重新核对身份的 root FD 开始逐层
  no-follow 打开候选、resource 与嵌套目录，拒绝软链接、路径逃逸、父目录/文件替换竞态、非普通文件
  以及非法或过大 frontmatter/body/resource；仅存候选快照与 hash，默认
  `untrusted_candidate`，不自动信任、安装或执行脚本；
- MCP 支持无 Shell 的 Docker stdio JSON-RPC 与明确标注为 legacy 的 SSE+POST transport；stdio 只接受
  Core 配置的 workspace root ref 与 digest-pinned 本地镜像，使用过滤后只读快照、`--network none`、
  无环境 Secret、固定 CPU/内存/PID/capability 限制和有界快照，不回退 Host。legacy SSE 的 endpoint/
  bearer 只按 SecretBroker 60 秒 Lease 解析，Lease 到期会关闭连接并清空材料。工具必须存在于快照，
  参数先验证，再经 Action Gateway 与一次 Capability Lease 才调用；默认 stdio 的无网络只读能力可
  ALLOW，legacy SSE 的网络与 Secret 使用为 ASK；持久 Approval 可由 User 或受控 Reviewer 决定且只能
  消费一次，DENY/hard DENY 不可覆盖；
- MCP 工具调用在发出前持久保留精确配置/工具 Schema/参数绑定的 action receipt，并原子进入 `sent`；
  完成结果脱敏后持久化并可幂等回放，发送后丢失结果则进入 `outcome_unknown`，禁止自动重放。Server
  start 另用 token/fencing/TTL CAS，避免并发启动两个进程；
- Phase 5A 实现版本化 Cron/一次性 Timer、IANA 时区与 DST gap/fold 处理、`skip`/`fire_once`/
  有界 `catch_up` misfire、持久幂等 RunRequest Queue、手工触发、取消与 DLQ 显式 replay。
  Scheduler Leader 只负责具象化 due occurrence，Runtime Writer 只负责 dispatch；两者与 Job Lease 均使用
  owner/token/单调 fencing/TTL，过期执行者不能续租或提交。有界指数退避达上限后进 DLQ；
  非幂等 dispatch 在副作用已开始后丢失 lease/结果时进 `manual_reconcile_required`，不自动重放。
  调度 dispatch 经 Policy/Capability/Audit 后使用持久 `scheduler_graph_dispatches` 绑定唯一 Graph Run；
- React GUI live 已接通 Schedule 列表/创建/修改/暂停/恢复/取消、手工触发、Queue、DLQ 和显式
  replay，并保持加载/空/错误、重试幂等键与高风险操作确认；
- Phase 5B Remote Control 已实现单 Host、默认关闭、短时一次性配对、Ed25519 签名、X25519 会话密钥、
  ChaCha20-Poly1305 加密、独立设备 Scope/撤销、RemoteSession/Cursor、Command 幂等与 Host Ack；配对
  Challenge 持久绑定本机预授 Scope，设备请求只能取其子集；远程动作还必须匹配 Host 注册的
  `(tool, operation) → capabilities` 精确映射，不能靠设备自报 Capability 提权。本地
  Policy/Capability/Audit 是最终裁决，确定未配置 executor 的动作明确拒绝；Command 执行使用可续租
  owner lease，只有租约过期的 `received`/`accepted` 才会保守收口，后者进入 `outcome_unknown`；人工
  核对还要再次通过 Action Gateway，并以 CAS 落到已知终态，旧 owner 不能覆盖，不自动重放；
- 自托管 Relay MVP 只保存有 120 秒上限和大小上限的 opaque 加密 Envelope，采用独立 Bearer 运维鉴权；
  Host Connector 主动拉取、解密、重验设备签名并提交本地 Core，Relay delivery/ack 不冒充 Host Ack。
  Beta/RC 新增 TLS-only WSS 直连 Gateway：精确 Origin、Bearer、子协议、frame/pending 上限、
  Host/Device/Session 绑定与持久连接租约均失败关闭；Relay Ack 仍不冒充 Host Ack。此能力未经过公网
  部署验收，不等于允许把 `/web` 或普通 `/v1/*` 暴露到公网；
- Remote Execution Target 已与 Remote Control 分域：Target 注册、Identity/Heartbeat、Capability
  Manifest、Workspace Lease、fencing、Job/Result 幂等、取消、Artifact checksum，以及 Browser/Computer
  observe-before-act 的 target/precondition/postcondition 证据均由本地 Controller 持久裁决。Target
  凭据只保存环境变量引用，Lease token 只在首次响应返回且 SQLite 仅保存 SHA-256；非幂等运行中断线
  或租约失效进入 `manual_reconcile_required`，不自动重放；
- Beta/RC 的 Host/Target 生产 Connector 只接受 HTTPS，绑定签名、recipient、route、TTL、nonce、
  fencing 与调用方绝对 deadline；响应和流式 frame 均有界。发送后无法确认结果的非幂等操作进入人工
  核对，不能自动重放；
- Phase 6 Graph Compiler 允许带显式 `WriterNodePolicy` 的多个写节点，并强制独立 isolation ref、互斥
  ownership 和覆盖全部 Writer 的 Merge Node。运行层保存独立 Writer Workspace、token-hash + fencing
  Lease、Patch/Commit Artifact、冲突与 Merge Run；可信 Git adapter 在管理员映射的绝对 worktree 中
  重新核对 commit/patch SHA-256、base ancestry、changed paths 和 ownership；patch 只应用同一次验证
  得到的不可变内存快照，同一 target 由 SQLite `RUNNING` 唯一约束、可续租 owner lease 和 Git worktree
  文件锁串行，再经 Action Gateway 的 `workspace.write + git.commit` 审批执行 merge。Git adapter 在
  私有 checkout 先算出预期 tree，目标 index 必须逐字匹配，并用固定 tree 的 `commit-tree` + old-HEAD
  `update-ref` CAS 提交；检测到外部干扰会保留现场并转 `outcome_unknown`，不会 reset/clean 外部数据；
- additive `phase56.v1` 从同一 OpenAPI Schema 确定生成 TypeScript/Python Client，并逐字冻结 Phase
  1E/23/45 生成物。GUI live 已用该 Client 查询 Remote Host/Device、Remote Target/Job 和多 Writer
  投影，可从本机显式启用 Host、创建一次性票据和撤销设备；错误或断线保留最近投影并明确显示，
  不回退 Demo；
- additive `operant-beta.v1` 继续从单一 Schema 确定生成 TypeScript/Python Client，覆盖 WSS 连接投影
  和 Container Writer 生命周期；旧 Phase 1E/23/45/56 Schema 与生成物保持冻结。PWA、Textual TUI
  与 Tauri 薄壳都只通过生成 Client/正式 HTTP 边界访问 Core；TUI 的 Cursor tracker 按资源单调去重，
  SSE EOF 后先 Query 校正；Tauri 只负责本地 Core 生命周期与 WebView，不执行 Agent 动作；
- OAuth 2.0 Authorization Code + PKCE 支持精确 issuer/audience/subject/redirect、state/nonce、JWKS
  有界读取、回调一次性消费、登录限流、Secure/HttpOnly/SameSite Cookie 与可选撤销。Access/refresh
  Token 只保存在 Core 进程内存，退出、过期或重启即失效，不写 SQLite 或本地 token 文件；
- `operant serve` 拒绝公网、通配和含糊 hostname；私网监听必须同时启用 TLS 与 OAuth，WSS Gateway
  必须启用 TLS。运行时显式依赖 WebSocket transport；`--desktop` 只允许 loopback，并只为固定 Tauri
  Origin 和生成 Client 所需请求头开启最小 CORS；
- WorkflowRun、WorkflowRunEvent、任务状态和阶段检查点的 SQLite 持久化；
- v1—v15 单事务 SQLite Migration、逐版本冻结 manifest/checksum、完整 schema integrity 自检、
  真实 Week 1/完整 Week 1—4 数据库识别升级、精确 preview 收编和受限回滚；
- OpenAI-compatible `/v1/models` 查询和 origin 自动补全；
- 流式 `chat/completions` 与 Tool Call 分片拼接；
- Agent Loop 和 Tool Result 回写；
- 测试失败结构化反馈、有限自我修复和重复失败停止；
- workspace 文件工具、命令工具和 Git diff；
- Docker 快照 Runner、CPU/内存/PID 限制、无网络命令执行和进程清理；
- Role Tool Policy 双层校验；
- 持久化 Action Gateway：副作用 Tool Call 的规范化 Action Hash、Receipt、结果重放和未知结果保护；
- 高风险命令的持久化审批请求、单次决定、过期、审计，以及同进程暂停后继续执行；
- Token、精确费用、时间和 Tool Call 硬预算；依赖的 usage 或定价缺失时保持 unknown 并安全停止；
- 总超时和全 Workflow 协作式取消；取消会持久传播到并行 Explorer 和其他活跃 Agent；
- SQLite Session run lease：同一 Session 跨服务进程只允许一个 Agent run，持久 Pending Approval 也会
  阻止重启后误开新 run；租约以 token、generation 和 owner 防止旧执行者 ABA 释放或续期；
- 完整的 Typer CLI；
- Model、Role、Session、审批和 Workflow 的 FastAPI/SSE；REST Command 提供持久化
  `Idempotency-Key` Receipt、Action Hash、结果重放和统一错误信封；
- Planner → 只读 Explorer → Coder → Reviewer 编排，以及由明确 verdict 驱动的有限返工；
- 最多 4 个只读 Explorer 的有限并行、自定义角色替换和并行槽位权限校验；
- 每个子任务的结构化成功/失败结果，以及必需角色失败时的安全停止；
- 可替换的只读 Main Role 最终汇总，并接收全部结构化子任务结果；
- 中断任务的阶段边界恢复；Coder 写结果不明时转为人工核对，拒绝自动重放；
- Session 与 Workflow 级 Trace 摘要、Token/耗时/错误统计和脱敏 JSONL 导出；
- Working、Episodic、Project 三类 Memory、版本/来源/作用域和 SQLite FTS5；
- 任务开始前读取已确认的项目知识，任务结束后生成项目结构/编码约定、验证命令与情节候选知识；
- Memory 候选确认、保守激活和停用；
- 任务、Trace、恢复、取消与 Memory 的 CLI/API；
- 无前端框架、无 CDN 的本地 Web 工作台，支持注册表、选角、SSE、审批和任务回放；
- Provider usage 解析、模型/工具/Agent 耗时和脱敏的 Provider 异常事件；
- EvaluationSuite、EvaluationCase、EvaluationVariant、EvaluationRun、EvaluationResult 及其不可变声明/
  实际快照、验证结果、指标、聚合与失败分类领域模型；
- 顺序执行 Case × Variant × repetition 的 Evaluation Runner v1，支持单 Session/完整 Workflow、
  模型/Prompt/effort/Memory 对照，以及 Exp 19—24 的 Suite 表达；
- 每个 Evaluation Result 的隔离 artifact workspace、变更路径、外部安全验证、Trace 证据和五类根因分析；
- Session、Workflow、Evaluation 事件的 SQLite Cursor、开区间 Query 和已提交 SSE 回放；
- Evaluation Suite/Run/Result/Event 的 SQLite 持久化、CLI 和 FastAPI/SSE；
- `.env` 安全解析，不执行 shell 内容；
- Kimi K2.6、Gemini 3.7 Flash 的六角色真实多模型端到端验收；
- `SECURITY.md` 安全边界；
- 自动化测试、Ruff、mypy 和 GitHub Actions CI。

### 尚未完成

- 模型流中任意字节位置的恢复，以及结果未知 Coder 写操作的无人值守恢复；
- 审批 Future 的跨进程恢复；
- 使用真实 Provider 完成 Exp 19—24、形成统计性实验结论和用户学习验收；
- 自动模型价格发现、显著性分析，以及中断 Evaluation Run 的逐 Result 自动续跑；
- 允许公网暴露的接入网关、多租户身份系统与通用 CSRF 方案；当前 OAuth 只服务单用户私网部署，
  本地 loopback 仍可无 OAuth 运行；
- Graph Proposal/智能创建、Graph IR 中的 Timer 节点执行与任意第三方节点执行器；Phase 5A
  Scheduler 只能触发已发布 Graph Workflow，不会把任意 Graph 节点变成后台作业；
- 完整的安装向导、自动更新、代码签名/公证与移动端推送；当前 React PWA、Textual TUI 和 Tauri
  薄壳已统一使用生成 Client，但 Tauri 候选包仍要求本机已安装可执行的 `operant` Core；
- 复杂 `@` 引用、Provider Cache 的执行/复制，以及面向非可信 HTTP 客户端的 Artifact capability 签发；
- 多 Host 自动发现/通知、允许公网部署的 Gateway、真实浏览器/桌面驱动矩阵与高压容量验收；当前交付
  单 Host TLS/WSS 直连、HTTPS Host/Target Connector、Container Writer 生命周期和可信 Git worktree
  merge adapter，不建设 SaaS、多用户、分布式 Core 或高可用。



## 16. 测试与质量检查

当前测试覆盖：

- Role 修改产生新版本；
- 进程重启后旧 Snapshot 不变；
- 不支持的 effort 被拒绝；
- 凭据不能进入 Model Profile URL；
- Role 复制和停用；
- Model Profile 更新、停用和会话级模型覆盖；
- 默认角色幂等初始化；
- Tool Result 写回下一轮模型上下文；
- 失败测试的结构化反馈、最小 Patch 修复和再次验证；
- 连续相同测试失败的 `agent.no_progress` 停止；
- 高风险命令审批、批准后继续执行；
- Shell 解释器审批、命令输出截断和 Host 命令超时；
- 总超时持久化和运行中取消；
- workspace 路径逃逸被拒绝；
- Docker 命令的无网络/资源限制参数和过滤快照；
- Docker 真实集成测试在 Docker 与测试镜像均可用时执行，否则明确跳过；
- 只读角色不能执行写工具；
- Coder 可以修改真实文件；
- OpenAI-compatible 模型发现；
- origin Base URL 自动补全 `/v1`；
- SSE Tool Call 分片拼接；
- `.env` 不执行 shell 且不覆盖进程环境；
- Main/Planner/Explorer/Coder/Reviewer 独立 Session 的确定性工作流；
- `role_main` 接收全部结构化结果并在独立 Session 中生成最终汇总；
- 两个只读 Explorer 的真实异步并发、结构化结果交接和自定义 Role ID；
- Explorer 超时结构化为状态、完成步骤和失败原因，且不会让后续只读结果丢失；
- Main/Planner/Explorer/Reviewer 槽位拒绝可写角色，API 在建立 SSE 前返回配置错误；
- `max_rework_rounds=0` 时仍报告缺失的 Reviewer verdict；
- Workflow 状态、角色选择、阶段和事件写入 SQLite，进程重启时运行中任务转为中断；
- 恢复任务复用已提交的 Planner/Explorer/Coder 检查点，不重复执行已确定的写阶段；
- Coder 结果未知时拒绝默认重放，并进入 `manual_reconcile_required`；
- Working/Episodic/Project Memory 的版本、FTS5 检索、作用域判权、确认和停用；
- 任务只召回精确 workspace 的 active Project Memory，并保守保存验证命令与候选总结；
- 成功 Explorer 的项目结构/编码约定摘要只保存为 candidate Project Memory，不会自动注入后续任务；
- Provider usage 解析、模型/工具耗时、脱敏 Session/Workflow JSONL Trace 和异常清洗；
- Workflow/Memory/Trace 的 CLI 与 API 回归；
- 无 CDN Web 工作台的静态资源、任务查询、恢复、取消和安全 DOM 约束；
- FastAPI 注册表、默认角色、会话覆盖和 Snapshot 回归；
- Evaluation 领域边界、Prompt/Role/Memory/环境/Workflow 快照契约和未知指标传播；
- Suite/Case/Variant/Run/Result SQLite 事务、唯一 Pending→单一终态、取消/流关闭和重启中断；
- artifact 隔离复制、越界软链接排除、外部验证超时/进程组终止和变更路径检查；
- Session/Workflow Variant、Memory 开关与禁用回写、实际角色漂移、指标/费用聚合和五类 Trace RCA；
- Evaluation CLI/API/SSE、错误清洗和本地 artifact 路径脱敏。
- v1/v2/v3/v4 Migration 的旧库保留数据升级、原子失败、并发初始化、校验和/版本损坏拒绝，以及只有
  显式 isolated 且对应租约/M0 表为空时才允许的受限 rollback；逐版本冻结 manifest/checksum、完整受管对象/DDL/列/
  PK/UNIQUE/CHECK/FK/index/trigger/FTS integrity/AUTOINCREMENT/`foreign_key_check` 漂移拒绝，以及
  真实 Week 1/完整 Week 1—4/精确 preview 的兼容升级；
- REST `Idempotency-Key` 自动生成、相同 Action Hash 结果重放、不同 Hash 冲突、Handler/首帧前流失败
  后人工核对、同 key 不重复调用 Handler、首次/replay 同体脱敏，以及 trailing-slash 307 不占 Receipt；
- SSE Command 只在首帧资源与持久 Cursor 可验证时 accepted、完整首帧 256,000 bytes 上限、
  FAILED/manual-reconcile 分流，以及同 key 重试返回 202 JSON typed Receipt 和 `replay_url`；
- Tool Action Hash 的规范化与精确副作用绑定、重复 Tool Call 只执行一次、进程重启未知结果保护；
- Approval 初始 pending、Receipt 上下文/状态精确绑定、持久正向 Decision 执行校验、决定/过期/CAS/
  审计/重启查询，以及决定落库早于 Future 创建的竞态恢复；
- Session single-flight、API 建立 SSE 前 JSON 409、Service 层兜底、Pending Approval 重启阻断，
  跨进程并发 acquire 只有一个胜者、lease 过期回收、stale token/generation/owner fencing、取消后
  Action Gateway 禁止新副作用，以及不同 Session 并发；
- Workflow coordinator guard、活跃 guard/child lease 的多实例初始化保护、guard 过期崩溃恢复、
  长 Provider 等待时 guard 丢失收束、并行 Explorer 全取消、重复取消和取消后不启动 Coder；
- 累计 completion Token、精确定价费用、整次运行时间和 Tool Call 硬预算，usage/定价 unknown 时
  副作用前 fail closed，以及 Provider 剩余额度、Trace/Evaluation unknown 传播；
- Session、Workflow、Evaluation 的真实 SQLite Cursor、开区间 Query、SSE `id` 一致性和
  `Last-Event-ID` 已提交事件回放；
- v1/v2/v3/v4 → v5 升级、重复初始化、逐步空表回滚、失败原子性、历史数据保留和 schema drift；
- 父子 Thread、显式 legacy mapping、并发 Turn/Item position、八类 Item、终态追加栅栏、不可变 trigger、
  Thread/Turn/Item/Artifact Cursor 分页和 Thread Item SSE `Last-Event-ID` 回放；
- Artifact 原子发布、跨实例/并发去重、同 hash metadata 冲突、完整性损坏、大小上限、临时文件清理，
  以及路径穿越、逐组件软链接、大小写别名、非普通文件和 inode 替换拒绝；
- Thread/Artifact 修改 Command 的 M0 Receipt 重放、Action Hash 冲突、UTF-8 字节限额、metadata-only
  响应和本地路径/正文不泄露；
- v1/v2/v3/v4/v5/v6 → v7 保留数据升级、相同 legacy Policy 分组、重复/并发初始化、失败事务完整回滚、
  非空受限 downgrade、冻结 manifest/checksum 与 schema drift 拒绝；
- Artifact capability 的敏感级别、对象/动作/到期/导出目录 inode 绑定，脱敏文本读取、原文下载、
  不覆盖导出和 M0 幂等重放；Retention Pin/归档/宽限期/计划删除/Trash/恢复、CAS 并发冲突、保护引用；
- 物理删除默认关闭、短时 trusted 授权、unlink 后数据库失败进入人工核对、原 Command/Action Hash +
  缺失 Blob 的显式收口，不自动重放未知删除；
- Artifact 审计严格零写、孤儿/缺失/损坏/非安全/删除残留分类、path-free finding、过期 finding 拒绝和
  单对象显式修复；CacheObservation hit/miss/unknown、缺失 usage 保持 null、脱敏、只追加 Cursor 分页；
- v1/v2/v3/v4/v5 → v6 升级、重复/并发初始化、事务失败原子回滚、空表受限 downgrade、append-only
  trigger、完整 PromptLayout/必需 Block 回放、全部类型化来源的实体/scope/version/hash 防伪、Memory
  Block/Binding/snapshot/ref 集合一致性、同 Agent request ordinal 并发幂等和 Cursor namespace；
- 每轮 Provider 输入与持久 ContextRevision 精确一致、Snapshot context window 冻结、unknown 容量/
  输出预留不按 0、父 Thread 不继承正文、Thread/Item/Artifact/Memory 引用判权和敏感数据 redaction；
- 动态 Watermark、首次 Emergency 不伪造 Compaction、追加式摘要不改 Canonical Item、首轮大 Thread
  的精确 `THREAD_ITEMS` coverage/hash/顺序/range/digest 与确定性并发复用、大 Tool Result Artifact Stub 可恢复、跨
  Agent/Session 去重、敏感 hash 降级拒绝和 Artifact 写失败不留部分 Revision；
- Agent 创建前 Factory 失败写 `session.run_failed(agent_id=null)`、释放 lease 且可重新运行；Agent 已
  创建后的 Composer 初始化失败写真实 `agent.failed`；
- 既有 Session run body/SSE 事件顺序兼容，Context Revision Query 只返回 hash/计数/Watermark/来源，
  不返回 prompt、工具参数、Artifact 正文或本地路径；
- 统一错误信封、校验输入与未知异常不泄密，以及首次非流 Command、REST 4xx/5xx、命令输出、
  Receipt、事件、模型 Tool Result 共用 bounded redaction；短 Bearer、任意/不完整 PEM 私钥块和
  大小写敏感文件名回归。
- Slash Registry 版本/别名/未知命令、`/init` 绝对路径/权限/幂等和公开路径脱敏；Context clear/compact
  不改 Canonical History、精确基线链、下一次真实 ContextRevision 才消费基线，以及跨 Agent
  `THREAD_ITEMS`/同 Agent ContextRevision Compaction provenance 分界；
- v1—v7 → v8 保留数据升级、重复初始化、三种精确 preview 收编、事务失败、并发基线、非空回滚拒绝
  与 v8→v7→v8 两条 trigger 的逐字形状恢复；
- `/review` 严格只读角色拒绝、预算/Session 运行复用、sensitive Artifact、M0 SSE Receipt 与 Audit
  `Last-Event-ID` 回放、启动前确定性 4xx 失败 Receipt 与同 key 不重执行；BTW 无主 Session
  lease/Event/Thread 写入、空工具、Provider 越权 Tool Call 拒绝、unknown usage/price、超时/取消/
  重启收口、敏感输出清洗和显式提升只追加一个 Steering。
- Phase 1E Schema digest、8-operation 形状、生成物确定性、Python 3.10/TypeScript 类型、标准/自定义
  XOR、无损 Cursor、分块 UTF-8/CRLF/畸形/line-frame-data 超限 SSE、错误信封和幂等键复用；
- Protocol/Project/Thread/Workspace File 后端投影、确定性分页、敏感路径、路径穿越、逐组件软链接、
  inode/目录替换、大小写别名、超限与类型化非重试错误；
- Session 与 active Thread 的事务绑定、已绑定/非 active/不存在冲突、失败回滚、并发单胜者、旧无
  `thread_id` 兼容和 M0 replay；
- 实际绑定 `127.0.0.1` 的 Uvicorn + 生成 Python Client 闭环覆盖全部 8 个 operation：确定性 Provider
  产生真实 Session/Run/SSE、批准/拒绝、Action 恰好一次、Receipt replay、SSE `id`/JSON Cursor/SQLite
  Cursor 三方一致、`Last-Event-ID` 只回放已提交事件且不新建 Command/Agent/Action，以及统一错误信封；
- React GUI live adapter/state 覆盖显式 Mock 分离、同源连接、持久 Thread→Session 恢复、Cursor scope、
  SSE 重连/去重、Approval、manual reconcile、有限事件窗口、无静默回退和移动端演示状态；真实浏览器
  另核对宽/窄屏、暗色、键盘首焦点、缩放、实际 Core 投影、审批动作和断线状态。
- Graph Compiler 覆盖重复/缺失节点与端口、类型和条件、非法 cycle/loop back、递归/子 Agent上限、
  非幂等重试、插件与 Timer/Scheduler 拒绝；Graph Runtime 覆盖 fixed-point Condition/Fan-out/ALL/ANY
  Join、并行上限、有限 Loop/limit handler、SKIP、Human Input/Approval 分流、cancel/interruption、
  required/type/有限 JSON 端口、实际聚合输入、严格 Loop 遥测、字符串布尔字面量、等待边界与并行兄弟
  继续执行、终态/预算失败拒绝迟到边界输入、成功提交后崩溃恢复、稳定幂等键和未知非幂等副作用人工核对；
- Coding Workflow bridge 覆盖 legacy Run 一一映射、Planner/Explorer/Coder/Reviewer/Main Attempt、
  Explorer 超时跳过、Coder committed/unknown、有限返工多 Attempt、恢复来源与流关闭收束；
- v8→v9 保留数据升级、重复初始化、事务失败、冻结 manifest/checksum、空 v9 受限回滚和非空拒绝；
- Team 覆盖 Definition、Graph CAS + Run + 非空 Roster + 双侧 Event 的单事务绑定、并发单胜者与故障
  注入回滚、active-agent 上限、canonical Message + 原子多接收人 Delivery、viewer 级时间线隔离、
  SQLite 过滤后分页、不可信上下文投影、大消息 Artifact 边界、幂等 Ack 首次事实返回、Cursor/scope
  冲突，以及 Task/Artifact Board revision 和幂等更新；
- `phase23.v1` 覆盖 25-operation 形状、digest、双 Client 确定性生成、Python 3.10/TypeScript 类型、
  Receipt/幂等、Graph/Team Event identity/version/sequence 与 SSE Cursor，并拒绝 Graph 创建时的反向
  Team 输入；同时逐字核对 `phase1e.v1` Schema/digest/专属 Client 文件未变，并验证两个生成器按任意
  顺序重建同一个兼容 Python 包入口；
- React GUI 覆盖 Graph 定义/启动、Node/Attempt 运行监控、legacy Coding Workflow Run 映射、Team/Roster/
  Message/Mailbox Ack/Task/Artifact Board、scope 切换即时清空与迟到响应隔离，以及窄屏、横屏、暗色、
  键盘焦点、断线和 reduced motion。
- Phase 4 覆盖 Action 规范化/稳定 hash、Policy 层级与 hard DENY、ASK-only Reviewer 失败关闭、
  Capability Lease 绑定/过期/撤销/用量 CAS、Secret Ref 延迟解析与输出脱敏、DENY no-progress
  和只追加 Security Audit；
- Skill Discovery 覆盖受信根、根/候选/资源软链接、越界、非普通文件、读取竞态、严格
  frontmatter 和文件/数量/层级上限；MCP 覆盖 stdio 与 legacy SSE 生命周期、端点/redirect/
  frame/Schema/JSON/快照上限、Docker 无网络/只读/digest pin、工具快照、精确配置 Action Hash、
  Gateway/Lease、ASK 的 User/Reviewer 一次性审批、并发 start fence、durable receipt replay/unknown 和
  Secret Lease 到期清理/结果脱敏审计；
- Phase 5A 覆盖 Cron/IANA/DST gap/fold、单次 Timer、三种 misfire、物化去重、手工触发、
  Leader/Writer/Job Lease fencing、取消竞态、有界 retry/backoff/DLQ/replay、非幂等结果未知人工核对、
  Policy/Capability/Audit 栅栏、Scheduler→Graph 持久幂等绑定、Core 接管与 lifespan 停机释放；
- v1—v11 保留数据升级到 v12、v10/v11/v12 manifest/checksum 冻结、并发初始化、中途失败原子回滚和
  只允许空数据 isolated downgrade；`phase45.v1` 覆盖 32-operation 形状、digest、TS/Python 确定生成和
  与冻结 Phase 1E/23 生成物兼容；
- React GUI Phase 45 live 覆盖 Policy/有界 Audit、Skill 候选、MCP root-ref 配置/启停/删除/工具快照、
  ASK allow/deny 后同 key 重试、tool call、receipt 与 unknown 人工核对，以及 Schedule/Queue/DLQ/replay
  的加载、空、错误、断线、幂等重试和确认边界。
- Phase 5B 覆盖本机预授 Scope 的一次性配对/撤销、签名和密文篡改、Capability 伪报拒绝、过期、
  Host/session 绑定、Command replay/Host Ack/ASK、owner lease/CAS 崩溃收口与 Gateway 人工核对、key store 并发写、Relay
  TTL/大小/opaque delivery、Target identity/lease fencing、并发
  上限、Job/Result 幂等、Artifact checksum、Browser/Computer observation target + pre/postcondition、
  payload 上限、取消、断线 unknown 和重启 reconcile；
- Phase 6 覆盖 Compiler 的 Writer/Merge/ownership 约束、Lease token hash/fencing/expiry、Artifact base/
  ownership/test evidence、稳定冲突检测、review/rollback/terminal CAS、管理员路径映射、Git commit
  ancestry/SHA/changed paths、patch 不可变快照、target owner lease/独占 RUNNING/文件锁、预期 tree 与
  old-HEAD CAS、外部干扰保留现场、崩溃后人工 reconcile，以及 finalize 前 Action Gateway ASK；
- `phase56.v1` 覆盖 operation 形状、digest 和双 Client 确定生成，并确认 Phase 1E/23/45 Schema、digest
  与专属生成文件保持冻结；GUI adapter/test 覆盖生成 Client、live 无 Mock fallback 和 Remote/Writer
  投影解析。

本地验证命令：

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv lock --check --offline
git diff --check
npm run test --prefix clients/gui
npm run typecheck --prefix clients/gui
npm run build --prefix clients/gui
```

2026-09-05 的 Beta/RC 最终门禁为 709 项 pytest 通过、1 个条件性 Docker 测试跳过、1 个既有
Starlette 警告，GUI 79 项、TUI 9 项与 Tauri Rust 1 项测试通过；Ruff format/check、mypy、
`uv lock --check --offline`、`git diff --check`、GUI typecheck/build 均通过。Vite 路由与依赖分块后最大
chunk 为 287.96 kB，低于 500 kB 候选预算。条件性测试仍不能单独算作运行验收，但同日另以实际
Docker 29.7.2 和本地已有的 digest-pinned `python:3.13-slim` 镜像完成 Container Writer
create/start/inspect/stop/remove/absent 生命周期；容器按宿主 `501:20` 运行，保留 `network=none`、
只读 rootfs、`cap-drop=ALL`、`no-new-privileges`、CPU/内存/PID 上限与唯一可写 workspace mount。
真实验收发现 Docker 29 的 not-found stderr 使用小写文本，修正为大小写无关识别并加入原样回归。

同日使用仅限 loopback 的临时自签名证书、实际 TLS Core 和 `websockets` Client 完成 WSS hello、
ping、cursor sync 及持久化关闭投影。另以两个真实本机 HTTPS Server 完成 Target execute/cancel 与
Ed25519 响应验签，以及加密 Relay Envelope、签名 Host Command、Action Gateway Receipt 和
“Host completed Receipt 后才 Relay Ack”的边界。OAuth 使用独立本机 HTTPS IdP 验证完整重定向、
PKCE S256、RS256 ID Token/JWKS、Secure+HttpOnly Cookie、受保护 API、logout 双 Token revocation
和退出后重新 401；Token 只在进程内存出现，SQLite 未落盘。该 OAuth 证据不等于外部 IdP 联调。

生产 PWA build 在实际 Chromium 中注册有效 Service Worker/manifest，live 模式连接真实 Core；
1440×900 与 390×844 均完成视觉/可访问树核对，390px 无水平溢出。真实 Textual TUI 完成协议与 Schema
digest 协商；真实 macOS Tauri `.app`/WebView 以 `tauri://localhost#/chat` 冷启动并连接 loopback Core，
Core/协议/Projection Query 均正常。该候选仍未签名、不生成 DMG，也不代表公网、浏览器矩阵、代码签名、
公证或自动更新验收。

正式 `operant model discover` 发现并选用精确模型 ID `gpt-5.6-luna`，通过正式 ModelProfile 与只读
Session 精确返回 `BETA_RC_REAL_ACCEPTANCE_20260905_OK`；用量为 543 input + 15 output tokens，请求耗时
4.143 秒，0 个 Tool Call，隔离 workspace 无写入，SQLite 只保存 `secret_ref=OPERANT_API_KEY` 且未出现
API Key。Python wheel、sdist 与 TUI wheel 另在全新 Python 环境完成安装和 CLI/生成 Beta Client import
smoke；SPDX SBOM 与 SHA256 manifest 从冻结 lock 生成并复核。该模型 smoke 与 Container 生命周期是
两份独立证据，不等于真实模型控制桌面、真实模型加 Docker Coder 或公网链路验收。

首次 GitHub Python 3.10 门禁进一步发现仓库发布检查直接导入仅在 Python 3.11+ 内置的 `tomllib`；
`release_checks.py` 现于 3.10 使用显式锁定的 `tomli` 回退，Python 3.10.14 隔离环境中的 4 项发布证据/
篡改拒绝测试通过。该修复不把 `tomli` 加入产品运行时，只在 `dev` extra 且 Python 3.10 时安装。

2026-09-04 的 Phase 5B/6 验收使用隔离 v13 SQLite、实际 localhost Uvicorn 和生成 Python
`Phase56Client`，完整跑通 Host 显式启用、一次性配对、设备侧 X25519 会话密钥推导、签名加密
Remote Command、Host Ack/Cursor、Relay publish/pull/ack、独立 Remote Target 注册/心跳/Workspace Lease，
以及 Browser/Computer observe-before-act Job/Result；全部受控动作都留下 Action Gateway hash，协议
digest 为 `538eb163b88e0bfbb42f99c84d31314b01965b23e95b662604b97963c7de6513`。实际链路发现并修复
`/v1/remote-targets/jobs` 被动态 target 路由抢先匹配的问题，并加入 200 响应回归；全量生成顺序还发现
旧协议生成器会覆盖 Python 包入口，现由公共生成器统一保留 additive Phase 56 export 并加入确定性回归。

同一真实 Core 的 GUI live 验收在 1440×900 和 375×812 下显示 Host、已配对设备、Remote Target 与
Browser/Computer Job 投影；键盘首焦点为 skip link，随后可到 Refresh 和 Create Pairing Ticket，实测
标题与按钮对比度分别为 17.49、16.74 和 7.29。Core 断开后写动作禁用、保留最后投影并显示
“不会回退演示数据”的显式错误；以同一 SQLite 重启 Core 后投影恢复。该过程还发现并修复 live
Settings 未暴露 Remote 页面的问题，保留旧 `tab=remote` 跳转兼容，不新增视觉方案。

独立 `sol-medium` Reviewer 聚焦 Scope/Capability 提权、密文与 replay、owner lease/CAS、断线恢复、
unknown 人工核对、Git patch TOCTOU、同 target 并发与外部干扰，返工后给出 `VERDICT: PASS`，未留下
P0/P1/P2。正式 `operant model discover` 发现并选用精确模型 ID `gpt-5.6-luna`，通过本分支正式
ModelProfile 与只读 Session 返回 `PHASE56_REAL_MODEL_OK`；用量 574 tokens、模型请求耗时 7.889 秒、
0 个 Tool Call，隔离 workspace 无写入，SQLite 仅保存 `secret_ref=OPERANT_API_KEY`。该 smoke 证明
正式 Provider/ModelProfile/Session 链路仍可用，不等于真实模型控制浏览器/桌面、生产 Relay/Target
connector、Container Writer 生命周期、多进程高压或真实模型 + 多 Writer 的同一次端到端验收。

2026-09-04 的 Phase 4/5A 验收使用隔离 v12 SQLite、实际 localhost Uvicorn、生成 TypeScript/Python
Client 和真实浏览器；最终返工后完整 pytest 为 618 通过、1 个条件性 Docker 测试跳过、1 个既有 Starlette 警告，
GUI 为 73 项测试通过。Ruff format/check、mypy、`uv lock --check --offline`、GUI typecheck/build、
Phase 45 生成器双次复现、Phase 1E/23 冻结文件核对和 `git diff --check` 均通过；Phase 45 digest 为
`94a3b48ba9482184712c587937a9606373637663dd650d92fd252766e6e25af3`。真实浏览器验证 Policy/Audit、
Skill 受控扫描、MCP root-ref 表单与非法 cwd 拒绝、Schedule/Queue/DLQ Projection、375px 无横向溢出、
键盘焦点，以及 Core 断线后保留只读 Projection 但禁用配置、审批、调用和 Receipt，且不回退 Mock。
另以本地已有 `python:3.13-slim` 显式设置 `OPERANT_DOCKER_TEST_IMAGE`，真实 Docker Runner 集成测试
6 项通过；同一 digest-pinned 本地镜像上的受控 MCP stdio responder 还验证 JSON-RPC 初始化/列表/调用、
过滤快照可见、workspace 写入被拒绝和网络被拒绝。这仍不等于真实第三方 MCP 专用镜像兼容性验收。
Vite 约 903.58 kB 主 chunk 警告保留为性能债务。

独立 `sol-medium` 安全 Reviewer 先后发现并验证同步 Reviewer 超时绕过、MCP Schema 静默忽略、MCP
start lease 卡死、stdio/Skill 父目录替换竞态与目录枚举预算过晚；返工加入对应回归后，最终复测确认
上述攻击均 fail-closed、FD 无泄漏、扫描工作量受独立 entry cap 约束，并给出 `VERDICT: APPROVE`，
未留下 P0/P1/P2。

同日按 D-034 在已配置 `.env` 的原项目根目录先执行正式 `operant model discover`，再使用发现结果中的
精确模型 ID `gpt-5.6-luna`，通过本分支正式 ModelProfile 和只读 Session 入口完成受控真实调用；安全
返工后的最终重跑返回 `PHASE45_FINAL_REAL_MODEL_OK`，用量 549 tokens、模型请求耗时 12.873 秒，
0 个 Tool Call，隔离
workspace 无写入。SQLite 回读确认 Profile 只保存 `secret_ref=OPERANT_API_KEY`，Session 快照仅允许
`read_file`、`search_files` 与 `git_diff`；凭据未复制到隔离 worktree、日志或文档。该 smoke 证明本次
生成 Client/GUI 所依赖的正式 Provider/ModelProfile/Session 链路仍可用，不等于真实第三方 MCP Server、
真实模型工具写入、真实模型 + Docker Coder 或 GUI 外部模型端到端验收。

2026-09-03 的 Phase 2/3 验收使用隔离 v9 SQLite、确定性 Provider、实际 localhost Uvicorn、生成
TypeScript/Python Client 和真实浏览器；完整 pytest 为 503 通过、1 个条件性 Docker 测试跳过、1 个既有
Starlette 警告，GUI 为 44 项测试通过。Ruff format/check、mypy、`uv lock --check --offline`、GUI
typecheck/build、两套生成器双次复现和 `git diff --check` 均通过。真实浏览器验证 Graph Definition/
Run/SSE、Team 定向 Mailbox 与 Ack、Run/viewer scope 切换即时清空、宽屏与窄屏、深色、高对比键盘
首焦点、reduced motion 以及 Core 断线显式失败且不回退 Mock。该证据不包含外部真实模型或 Docker E2E；
条件性 Docker skip 不视为容器验收，Vite 814.79 kB 主 chunk 提示保留为性能债务。

同日按正式门禁先在项目根目录执行 `uv run operant model discover`，再使用发现结果中的精确模型 ID
`gpt-5.6-luna`，通过正式 ModelProfile 完成一次只读 Session 调用和一次 Coding Workflow 调用。Session
返回 `REAL_MODEL_OK`，用量为 537 tokens；Workflow 的 Planner、Explorer、Coder、Reviewer、Main 共
完成 5 次模型调用，Reviewer 给出 `APPROVED`，总用量为 4,772 tokens，模型请求耗时合计 33.732 秒，
没有 Tool Call 或 workspace 写入。该 Workflow 的持久 Graph 投影为 `completed`：10 个 NodeRun、7 个
成功 Attempt、27 个连续 `phase23.v1` Graph Event 均可从隔离 SQLite 重新读取。运行时凭据仅注入目标
进程，没有复制到隔离 worktree、运行日志、文档或 SQLite。该受控 smoke 证明当前 Provider、Session、
Coding Workflow 与 Graph bridge 的真实模型链路可用；它不等于真实模型执行工具/写入、真实模型加 Docker
Coder、GUI 外部模型端到端或 Exp 19—24 验收，Team 本身也没有独立模型调用入口。

2026-09-02 的 Phase 1E 验收使用隔离 SQLite/Workspace、确定性 Provider、实际 localhost Uvicorn、
生成 Python Client 和真实浏览器；完整 pytest 为 402 通过、1 个条件性 Docker 测试跳过、1 个既有
Starlette 警告，GUI 为 29 项测试通过，独立 TypeScript SDK parser 为 2 项通过；Ruff format/check、
mypy、`uv lock --check`、GUI typecheck/build、生成物双次复现和 `git diff --check` 均通过。真实浏览器中
Approval 后 Action 恰好执行一次，SSE 终态
释放输入区；停止 Core 会显式断线且不回退 Mock，同库重启后自动恢复服务端 Session。该证据证明真实
本地 Core HTTP/SSE/Approval 闭环，但不是外部真实模型或 Docker E2E；条件性 Docker skip 仍不视为
容器验收，Vite 的大 chunk 提示保留为已知性能债务。

2026-09-01 的 Phase 1D 后端底座使用确定性 Provider、隔离 v1—v8 SQLite 和临时 Workspace/Artifact
Store：完整 pytest 为 367 通过、1 个条件性 Docker 测试跳过，并保留 1 个上游 Starlette TestClient
弃用警告；Ruff format/check、mypy、`uv lock --check` 和 `git diff --check` 通过。未设置真实 Provider，
未把 Docker skip 视为容器验收，也未实现 Skill、MCP、Scheduler 或客户端接入。

2026-08-31 的 Phase 1C 后端底座只使用隔离数据库与临时 Artifact Store，覆盖 v1—v6 升级、能力票据、
目录身份替换、Retention CAS/宽限期、物理删除中断、人工核对、孤儿/缺失/损坏/非安全审计、显式修复
和 Cache usage unknown：完整 pytest 为 336 通过、1 个条件性 Docker 测试跳过，并保留 1 个上游
Starlette TestClient 弃用警告；Ruff format/check、mypy、`uv lock --check` 和 `git diff --check` 通过。
未对真实 `.operant/` 执行清理，未设置真实 Provider，也未做高并发吞吐基准；Docker skip 不视为容器验收。

2026-08-30 的 Phase 1B 后端底座使用隔离 v1/v2/v3/v4/v5 数据库、并发请求、伪造关联、敏感内容和
Artifact 故障探针：完整 pytest 为 328 通过、1 个条件性 Docker 测试跳过，并保留 1 个上游 Starlette
TestClient 弃用警告；Ruff format/check、mypy、`uv lock --check` 和 `git diff --check` 通过。未设置
真实 Provider，也未做高并发吞吐基准；Docker skip 不视为容器验收。

2026-08-28 的 Phase 1A 后端底座使用隔离 v1/v2/v3/v4/preview 数据库、并发进程/线程、受控文件系统
竞态和临时 `OPERANT_DB_PATH`：完整 pytest 为 237 通过、1 个条件性 Docker 测试跳过，并保留 1 个
上游 Starlette TestClient 弃用警告；Ruff format/check、mypy、`uv lock --check` 和
`git diff --check` 通过。独立 terra-max Reviewer 复验 chunked body 有界读取、Canonical 引用防伪、
root 替换、preview 原子拒绝、并发 migration/position/blob 去重与旧 API 兼容，最终 P0/P1/P2 均为 0。
未设置真实 Provider，也未做吞吐基准；Docker skip 不视为容器验收。

2026-08-28 的 Phase 0 可靠性收尾使用确定性 Provider、隔离 fixture 和临时 `OPERANT_DB_PATH`：
完整 pytest 为 194 通过、1 个条件性 Docker 测试跳过，并保留 1 个上游 Starlette TestClient 弃用
警告；Ruff format/check、mypy 和 `git diff --check` 通过。未设置真实 Provider，也不把 Docker skip
视为容器验收。2026-08-27 的 M0 后端协议基线最终门禁使用确定性 Provider、隔离 fixture、临时
`OPERANT_DB_PATH` 和 FastAPI TestClient：完整 pytest 为 161 通过、1 个条件性 Docker 测试跳过，
并保留 1 个上游 Starlette TestClient 弃用警告；Ruff
format/check、mypy 和 `git diff --check` 通过。未设置真实 Provider，也不把 Docker skip 视为容器
验收。2026-08-25 的第四周工程收尾门禁为 92 通过、1 跳过。2026-08-22 的门禁曾在
显式设置 `OPERANT_DOCKER_TEST_IMAGE=python:3.13-slim` 后执行
Docker Runner 真实集成路径：pytest 为 57 通过；Ruff format/check、mypy、JavaScript 语法和
`git diff --check` 均通过。仅保留 1 个来自上游 Starlette TestClient 的弃用警告。

开发阶段按需使用 `codebase-memory-mcp` 维护代码知识图谱。当前本机版本为官方
`v0.9.1-rc.1`；Operant full 索引已成功生成 516 个节点和 2597 条边，`search_graph`、
`trace_path` 与 `get_architecture` 均已验证。完整操作说明存放在本机按需加载的
`codebase-memory-mcp` skill 中，不再注入用户级全局 Prompt。

## 17. 真实模型验收场景

仓库中的 `examples/buggy_calculator/` 是可重复验收 fixture。原始版本故意保留两个问题：

- 除法使用整除，丢失小数；
- 除数为零时没有转换为明确的业务错误。

2026-07-30 已完成一次真实验收：

1. 将 fixture 复制到临时 Git workspace；
2. 通过 `/v1/models` 确认 `kimi-k2.6`、`glm-5.2` 和
   `gemini-3.1-pro-preview`；
3. Kimi Planner 读取代码并生成计划；
4. GLM Coder 使用 `apply_patch` 修改真实临时文件，并运行
   `python3 -m unittest -v`；
5. Gemini Reviewer 读取 Git diff 与测试，给出通过结论；
6. 模型外再次运行测试，3 项全部通过，`git diff --check` 通过；
7. SQLite 保存三个独立 Session 的 Snapshot 与事件；
8. 另建 Role 和 Session，再把 Role 从版本 1 修改为版本 2 并切换 Model Profile；
9. 重新读取旧 Session，仍得到版本 1、旧 Prompt 和旧 Model Profile，
   `snapshot_unchanged = true`。

2026-08-22 又完成一次第三周六角色真实验收：

1. 先执行 `operant model discover`，确认本次使用的精确模型 ID 仍由上游提供；
2. 将 calculator fixture 复制到新的绝对临时 workspace，并为六个角色建立独立 Session；
3. Kimi K2.6 Planner 生成计划，Kimi K2.6 与 Gemini 3.7 Flash 两个只读 Explorer 并行检查实现和测试；
4. Gemini 3.7 Flash Coder 在该可信隔离副本中通过 Host Runner 修改 `calculator.py`；
5. Gemini 3.7 Flash Reviewer 给出 `VERDICT: APPROVED`，Kimi K2.6 Main 完成只读汇总；
6. 六个 Session、Workflow 阶段和事件均写入独立 SQLite，Trace 汇总得到 2 个模型、零工具失败和
   `APPROVED` verdict；
7. 工作流生成两条 candidate Project 结构/约定摘要、一条 active Project 验证知识和一条
   candidate Episodic 总结，并分别记录来源 Session 与任务；
8. 模型外独立运行 `python3 -m unittest -v`，3 项全部通过；`git diff --check` 通过，diff 只包含
   预期的除零检查和浮点除法修复；
9. 所有非 Coder 角色的 Snapshot 均再次核对为只读。此次 Host Runner 仅用于可信临时 fixture，
   不能外推为不可信 workspace 的宿主机安全验收。

完成最终代码前的真实验收还暴露并修正了两个问题：一次模型修复留下文件末尾多余空行，被独立
`git diff --check` 正确拒绝；验收脚本的默认数据库曾复用系统临时目录父路径，现已改为每次独立的
artifact 根目录。最终成功运行对应修正后的代码，并在 Coder 收到一次结构化测试失败反馈后完成修复。
更早还有两次独立尝试分别在 Planner 或 Coder 模型调用阶段遇到上游 Provider 瞬时错误；这些失败均
未计入成功证据。失败任务被持久化为失败状态，Provider 原始响应未写入审计事件。



## 18. 已知技术债务

1. SQLite 使用同步 API，运行规模扩大后需要评估异步边界；
2. Coding Workflow 已迁移到 Graph Runtime，但兼容协调入口仍按固定角色顺序调用真实 Agent。Phase 5A
   Scheduler 只能触发已发布 Graph Workflow，尚无 Graph Timer 节点或任意节点执行器；恢复只发生在已持久化边界，不支持从任意模型流位置
   继续。Coder 写入结果未知时必须人工核对，不能无人值守恢复；
3. Approval Request、Decision 和 Audit 已持久化，但待审批工具调用的 `Future` 与任意模型流位置仍只
   存在于进程内，不能跨进程恢复；重启后的决定不等于原 Agent 自动继续；
4. Memory 已有版本、来源、作用域、FTS5 和保守激活，但还没有自动冲突合并、质量评测、容量淘汰
   或跨项目知识共享；
5. 已有 v1—v14 原子 Migration、旧库识别升级和 Session/Workflow/Scheduler/Remote/Writer lease；v9
   Graph lease 仍只是单协调者预留，v11 Scheduler Writer 与 v13 多 Writer 解决的分别是调度派发和隔离
   工作区写入，不是分布式 Core 或高可用协调。downgrade 只用于显式 isolated 且新增表全空的数据库；
   没有通用生产 downgrade，REST Command 也没有跨常驻 Core 进程的 owner/liveness lease；
6. Session/Workflow/Evaluation/Graph/Team 已有 Cursor 和已提交事件回放，GUI 会按同 scope Cursor
   回放并查询投影校正，但仍不支持任意模型流位置续传；SSE 断线不保证后台继续。Phase 1E 冻结协议没有
   Session 列表/详情 Query，因此刷新后 GUI 只能从 Thread 的权威 `session_id` 恢复运行入口，并把角色/
   模型详情明确标为未查询，不能伪造本地详情；
7. OAuth PKCE 已为单用户私网部署提供身份保护，但不是多租户身份系统或通用公网 CSRF 方案；loopback
   默认仍可无 OAuth 运行。Phase 5B 配对和 E2E Command 只保护 Remote Control，不能给全部 `/v1/*`
   充当公网认证；Relay/Gateway 还有各自 Bearer/Origin 边界。当前仍不得直接暴露到公网；
8. Evaluation Runner v1 已有可复现 Suite、隔离 artifact、外部验证、指标和五类 Trace RCA，但模型
   价格仍须由配置/Suite 固定提供，尚无自动价格发现、Evaluation Run 级总预算与调度、统计显著性、
   真实模型 Exp 19—24 结果或逐 Result 断点续跑；中断组合会保留为不可重放的 Interrupted Result，
   当前仍需新建 Run 才能重新执行；
9. Docker Runner 已跑通真实隔离集成用例，第三周六角色 Workflow 也已在可信临时 Host fixture 上
    完成真实模型验收；两者仍是不同证据，尚未完成“真实模型 + Docker Coder”的同一次端到端验收，
    也尚未构建专用 Operant 镜像；
10. React PWA、Textual TUI 与 Tauri 薄壳已使用生成 Client；OAuth、WSS Gateway 和 HTTPS Host/Target
    Connector 已实现。Skill 信任/安装、Policy 修改、Graph Proposal/智能创建、Graph Timer/任意节点、
    多 Host 发现/通知、移动推送和完整安装向导仍未实现。Browser/Computer 仍不是内置驱动；Relay 和
    Gateway 不是已审计的公网托管产品；Tauri 候选只生成未签名 macOS `.app`，仍要求本机已有
    `operant` Core，不包含 DMG、签名、公证或自动更新。
11. Artifact 已有对象级 Retention、Pin、宽限期、Trash、只读审计和显式孤儿修复，但
    Session/Workflow/Evaluation 事件、Thread Canonical History、Tool/Command Receipt、Approval Audit、
    Memory 和 Context/Compaction 仍没有清理执行器，会随运行持续增长；Artifact 也没有后台自动清扫，
    真实物理删除必须另行启用并显式授权，不能把当前能力描述为全局容量治理。
12. Thread/Turn/Item 与 Artifact 已建立持久底座，Context Composer 可显式绑定新 Thread 与最小引用，
    但尚未把既有 Session/Workflow 自动投影为 Canonical History；旧数据仍只支持显式 legacy mapping，
    不能伪称已转换。复杂 `@` 解析、跨项目 sensitivity 授权策略和面向非可信 HTTP 客户端的
    capability 签发仍未实现。当前只记录 Provider CacheObservation，不执行、复制或裁决 Provider Cache。
13. ContextRevision 为了审计和精确解释当前保存 bounded-redacted Provider 输入，Compaction 与 Tool
    Result Artifact 也会持续增长；Artifact 之外还没有对象级 retention 执行，也没有语义摘要质量评测
    或吞吐基准。Composer
    与 SQLite/Artifact Store 使用同步本地 I/O，超大引用和高并发规模需要后续性能评估。
14. Slash Registry 当前冻结为 `phase1d.v1`，只覆盖四个正式 Context/Review/Workspace 命令；Phase 45
    Skill/MCP/Scheduler 使用独立 REST 协议，尚未注册为动态 Slash Command。MCP stdio 已限定为 Docker
    无网络只读快照，但尚未完成真实第三方 Server 与专用镜像的长时验收；legacy SSE 是兼容输运，尚未
    实现 MCP Streamable HTTP。Phase 45 Approval 可跨请求/重启保存决定，但不会恢复任意模型流位置。
    BTW Sidecar 的主动取消通知仍是本进程协作式信号；跨进程重启只会安全标为
    `process_interrupted`，不会从任意模型流位置恢复或自动继续。
15. GUI 已按路由 lazy-load，并把 React 与生成 SDK 分块；候选构建最大 chunk 约 288 kB，500 kB 预算
    会在构建时失败。仍缺真实大 Graph/Team/长事件流基准、浏览器矩阵和签名 Tauri WebView 验收。旧
    demo SDK 类型只服务明确 Mock，后续 live operation 必须先进入单一 Schema 再生成两种 Client。
16. Phase 6 已实现可信 Git worktree 验证/合并和受限 Container Writer 生命周期。Merge 进程若在 Git
    已提交而 SQLite
    尚未写入成功之间崩溃，owner lease 过期后会进入 `outcome_unknown`，保留独立 target worktree 供
    本机 Gateway 人工核对，不自动猜测或重放；同一 target 的并发由 SQLite 持久独占状态与跨进程文件锁
    拒绝；Container 的真实 Docker 生命周期仍依赖部署环境和 digest-pinned image。尚未完成多进程
    高压、超大 patch 性能和进程崩溃故障注入演练。



## 20. 变更记录

### 2026-09-05（Operant 2.0 Beta/RC 产品化收口）

- 从 `main@d06d6fb` 建立隔离集成分支，Codex 统一持有 additive `operant-beta.v1` Schema、SQLite v14
  Migration 与最终集成；三条 coder 实现轨分别交付 Gateway/Connector/Container、PWA/TUI/Tauri、
  OAuth/性能/候选发布检查，并由独立 Reviewer 复用定向证据审查边缘、安全、性能和 Bug；
- 生产入口 `operant serve` 限制显式 loopback/私网地址；私网必须 TLS+OAuth，WSS 必须 TLS，desktop
  CORS 只开放固定 Tauri Origin。OAuth Token 只保存在进程内存，登录、JWKS、token 和 revocation 都
  有界；
- WSS Gateway、HTTPS Host/Target Connector 与 Container Writer 生命周期保持 Host/Device/Session、
  route/recipient/nonce/TTL/fencing、Action Gateway、lease/CAS、未知结果人工核对和 SQLite 权威；
- React PWA 做路由/SDK 分块与 500 kB 预算，TUI 增加 scope Cursor 单调去重及 SSE EOF Query 校正，
  Tauri 只管理 loopback Core 和 WebView；候选构建生成锁文件依赖闭包 SBOM，Python 分发显式携带生成
  Client 和 WSS transport，TUI 候选依赖同版本 Core，未签名产物明确禁止发布；
- 使用本地自签名证书、真实 TLS Core 与真实 WebSocket Client 验证 WSS hello/ping/cursor 和持久化关闭
  投影；真实 Tauri macOS `.app`/WebView 切换 live 后连接 loopback Core，验证固定 Origin 的 CORS 预检、
  版本请求头、协议协商和 Projection Query。该证据不外推为公网部署、签名安装器或浏览器矩阵验收；
- 范围继续排除 SaaS、多用户、分布式 Core、高可用、多 Host 自动发现、移动推送与未验收公网暴露。

### 2026-09-04（Phase 5B Remote + Phase 6 Multi-Writer）

- 从 `origin/main@56d87b8` 建立隔离集成分支；Remote Control、Remote Execution Target 与 Multi-Writer
  三条实现轨并行交付，公共领域 Schema、SQLite v13 Migration、生成 Client 和最终集成由 Codex 统一持有；
- Remote Control 实现单 Host、本机显式开关、短时配对、设备 Scope/撤销、E2E 加密、签名 Command、
  Host Ack/Cursor 与 opaque Relay polling；Remote Execution 独立实现 Target Identity/Manifest、Lease
  fencing、Job/Result/Artifact、Browser/Computer observe-before-act 和 unknown-write 人工核对；
- Graph/SQLite/API 增加隔离 Writer、Lease、Patch/Commit Artifact、冲突与 Merge Node；可信 Git
  adapter 在管理员映射的 worktree 内重验 SHA/base/path ownership，Merge 由 Action Gateway 审批并在
  普通确定失败时回滚；若检测到外部干扰则保留现场并进入 unknown，不执行破坏性回滚；Lease token
  和一次性响应不以明文写入 SQLite/Command Receipt；
- Reviewer 复核后补强 Remote Command/Merge Run 的 owner lease 与 CAS 恢复、Gateway 人工核对、
  Git 私有 expected-tree/固定 tree commit/old-HEAD CAS；外部干扰保留现场并进入 unknown，不破坏性回滚；
- PR 的 Python 3.10 门禁发现调度器等待超时捕获类型只适配 3.11+；改为显式捕获
  `asyncio.TimeoutError`，保持 3.10/3.12/3.13 的 lifespan 停机语义一致；
- 新增 additive `phase56.v1` 确定生成 Client并冻结旧协议；GUI live 接入 Remote 与 Multi-Writer 投影，
  不新增视觉方案、不静默回退 Mock。OAuth、TUI、Tauri、完整 Remote PWA/WSS、多 Host 运维套件、
  生产 Target connector 与 Container Writer 生命周期仍明确排除或后续处理。

### 2026-09-04（Phase 4/5A 独立安全审查收口）

- 新增 SQLite v12 的 MCP durable action receipt、start fencing、stdio sandbox 绑定与 Phase 45 持久
  Approval；completed 可安全回放，sent/unknown 禁止重复副作用，User/Reviewer 决定均绑定精确 Action
  且只消费一次；
- Skill Discovery 改为 root/候选/resource/嵌套目录全链路 FD 锚定，复核 identity/version/path binding；
  缺少 dir-fd、scandir-fd、`O_NOFOLLOW` 或 `O_DIRECTORY` 的平台明确 fail-closed；
- stdio 改为 digest-pinned Docker、过滤只读 workspace 快照、无网络/无环境 Secret、固定资源与快照
  上限，不拉取且不回退 Host；快照复制新增目录 FD 锚定、no-follow 与版本复核，替换竞态 fail-closed；
  legacy SSE 的 endpoint/bearer 改由 SecretBroker 短租约按需解析，到期自动断开并清空；
- MCP 工具 Schema 改为明确支持的递归有界子集，未知断言在快照阶段拒绝；start 请求取消会关闭
  transport 并 fenced 地收口，过期 start lease 可由新 owner 原子接管，旧 owner 不能提交工具快照；
- Scheduler 在持有 Graph Run 到终态期间持续占用 concurrency slot，先做确定性校验再写 pending，使用
  稳定 Graph Run ID 恢复；Cron 搜索改为有界 calendar candidate；TS/Python Client 和 GUI 修改动作复用
  稳定 `Idempotency-Key`；
- additive `phase45.v1` 扩到 32 个 operation，增加 Phase 45 Approval、MCP workspace root ref 与 action
  receipt 查询，仍保持 Phase 1E/23 生成物冻结；范围继续排除 Remote/Relay、OAuth、TUI、Tauri、
  Browser/Computer 与通用多 Writer。

### 2026-09-03（Phase 4 Security + Phase 5A Skill/MCP/Scheduler）

- 新增 Action 规范化、分层 Policy 合成、hard DENY、ASK-only Approval Reviewer、精确 Capability Lease、
  延迟 Secret Ref 解析、DENY no-progress 与只追加安全审计；默认无匹配 DENY，Reviewer 失败关闭；
- 新增受信根内的有界无软链接 Skill Discovery，候选始终是未信任快照；新增无 Shell stdio 与
  legacy SSE MCP transport、有界协议/端点校验、工具 Schema 快照与 Action Gateway 前置栅栏。MCP
  Server 不是安全边界；默认 stdio/network/Secret 均 ASK，当前无 Phase 45 独立审批 continuation；
- 新增版本化 Cron/单次 Timer、IANA/DST、三种 misfire、持久幂等 RunRequest Queue、单 Scheduler
  Leader/单 Runtime Writer、Job Lease fencing、retry/backoff/DLQ/显式 replay、非幂等未知结果人工核对，
  并将 Policy/Capability 后的调度派发幂等绑定到唯一已发布 Graph Run；
- 新增冻结 manifest/checksum 的 SQLite v10/v11，及 additive `phase45.v1` 27-operation Schema、
  TypeScript/Python 确定性 Client；保持 Phase 1E/23 专属协议与生成物冻结。本次不新增 Remote/Relay、
  OAuth、TUI、Tauri、Browser/Computer 工具或通用多 Writer，Graph Timer 节点仍在 Compiler 拒绝范围。

### 2026-09-03（Phase 4/5A GUI 安全、Skill 与 MCP live 接入）

- GUI 复用生成的 `Phase45Client`、同源 Base URL 和显式 Client mode，增加独立 Phase 4/5A 适配与状态层；
- Live 安全设置只提供 Policy dry-run/解释，不伪造可修改策略；Approval 继续按 Session scope 走正式
  Command，ASK 与 DENY 不在客户端放行；安全审计列表只消费 cursor、时间、主体、事件、决定和规则
  ID，不把服务端 detail、原始动作参数或 Secret 放入客户端状态；
- Skill 页只展示 Core 受信根扫描出的未信任候选及安全 metadata；MCP 页接通配置、生命周期和工具
  Projection，启停/删除带确认，参数数组不经 Shell，端点与 Secret 仅接收引用名；
- Scheduler 页接通 Schedule 列表/创建/更新/状态、手工触发、Queue、DLQ 和显式 replay；
  Demo 页面保持原有分支，新增适配/状态/生成 Client 测试并验证 typecheck 与生产 build。

### 2026-09-03（Phase 2 Graph Runtime + Phase 3 本地 Team Runtime）

- 从冻结 `origin/main@4cbf828` 建立隔离分支，主 Agent 独占公共 Schema、SQLite v9 Migration 与最终
  集成；保留 `phase1e.v1` Schema、digest、生成物和 8-operation 行为不变；
- 新增 Graph IR/Compiler、Draft/Published Revision、GraphRun/NodeRun/Attempt、条件、Fan-out/Join、
  有界 Loop、Human Input/Approval/Wait/Subworkflow 边界、预算、取消和持久恢复；依赖从已提交事实做
  fixed-point 推进，强制并行上限，成功 Attempt 后崩溃可恢复，幂等未知结果沿用首次 key；Timer 因
  Scheduler 明确排除而在编译期拒绝，未知非幂等副作用继续进入人工核对；等待边界不会冻结并行兄弟，
  终态和预算失败拒绝迟到边界输入，不会复活 Run；返工补齐端口 required/type/有限 JSON、缺失可选
  输出禁边、实际下游聚合输入、严格 Loop 遥测与成功输出校验，以及只转换 Token 的 Condition 布尔字面量；
- 将现有 Coding Workflow 经 bridge 投影到 Graph Runtime，保留原 CLI/API、Role/Session、Action
  Gateway、Receipt、Approval、Cursor 与恢复契约；Attempt 绑定实际 AgentInstance 而不是 RolePreset，
  Coder 仍独占写入，返工追加 Attempt 而不覆盖历史；
- 新增本地 Team/Run/Roster、canonical Message、逐接收人 Mailbox、首次事实幂等 Ack、Task Board 与
  Artifact Board；Team/非空 Roster/初始 Team Event、Graph CAS 回填与 Graph Event 原子创建，并发只允许
  一个 Team 绑定同一 Graph，错误全回滚；消息按 Roster viewer 在分页前过滤，定向消息只对发送者与
  接收者可见；消息投影不能裁决 Graph/Approval，也不能绕过 Action Gateway；
- 新增冻结 manifest/checksum 的 SQLite v9、`phase23.v1` additive Schema、25-operation TS/Python
  Client 和 React GUI Graph/运行监控/本地 Team live 面；两个生成器重建兼容 Python 包入口，Phase 1E
  专属 Schema/digest/Client 文件仍冻结；Event 增加稳定 identity/version/run sequence，Graph 创建契约
  移除无法成立的反向 Team 输入；GUI 切换 Graph Run、Team Run、viewer 或 workspace 时先清空旧投影，
  丢弃迟到 resolve/reject，并在 SSE EOF 后查询权威终态；Skill/MCP、Scheduler、安全控制面扩建、OAuth、
  Remote、TUI、Tauri 与多 Writer 保持排除；
- 独立 `gpt-5.6-sol` medium Reviewer 连续三轮检查边缘情况、并发、回滚、恢复、性能与协议一致性；前两轮
  的 P1/P2 已全部返工，第三轮复跑最小复现、100 个后端聚焦测试和 44 个 GUI 测试后给出
  `VERDICT: APPROVED`，未发现 P0/P1/P2；
- 先执行 `uv run operant model discover`，再以发现的精确模型 `gpt-5.6-luna` 通过正式 ModelProfile
  完成只读 Session 与五阶段 Coding Workflow 真实调用；Reviewer 为 `APPROVED`，持久 Graph 投影包含
  10 个 NodeRun、7 个成功 Attempt 和 27 个连续 `phase23.v1` Event。该 smoke 不含工具执行、workspace
  写入、Docker Coder、GUI 外部模型端到端或 Exp 19—24；

### 2026-09-02

- 从 `origin/main@27fb387`（PR #9）建立隔离集成与三条独立执行线，清点后只导入 77 个 `clients/`、
  `sdk/` 源码/配置文件，排除 `node_modules`、`dist`、缓存和构建产物；共享目录中的前端草稿未被修改；
- 冻结 `phase1e.v1` 单一 OpenAPI Schema 与 8 个正式 operation，加入确定性离线生成器和
  TypeScript/Python Client；统一严格版本/digest 协商、Receipt/幂等、错误信封、无损 `int64` Cursor、
  有界增量 SSE 解析和同 scope 回放，生成两次保持 clean diff；
- 新增只读 Project/Workspace 聚合和安全目录 metadata 浏览；文件遍历逐组件拒绝软链接、敏感文件、
  越界路径和并发目录替换，不新增 SQLite Migration，也不返回正文或绝对宿主路径；
- `CreateSessionRequest` 增加兼容的可选 `thread_id`，Store 在单一 `BEGIN IMMEDIATE` 事务中创建 Session
  与唯一 legacy ref；React GUI live 以 Thread 投影恢复既有 Session，未查询的角色/模型详情保持明确
  unknown，不让客户端缓存覆盖 Core 绑定事实；
- React GUI 保留明确标识的演示模式，live 路径只消费生成 Client，并通过同源 `/v1` 接入本地 Core；
  打通 Project、Thread、Session、Run SSE、Cursor replay/reconnect、Approval 与错误/人工核对显示，任何
  失败都不静默回退或混合 Mock。Graph、Team、Skill/MCP、Scheduler、OAuth、Remote、TUI 和 Tauri
  仍明确排除；
- 最终复核补齐 SSE 首帧 `id`/JSON/SQLite Cursor 三方一致、Session/Thread 稳定 404/409、Python/TS
  parser line-frame-data 上限，以及 GUI 的 Thread 切换锁、全部已知失败终态、Approval 查询乱序隔离、
  Session Approval 作用域、Modal/Drawer 标题语义、键盘 separator 和 320 px 窄屏；
- 两位独立 `luna-max` Reviewer 在返工后分别复核 GUI 与协议/后端集成，最终 P0/P1/P2 均为 0；
- COM-20260901-001/002 只接受调整后的 Workspace 只读聚合与安全文件 metadata；模板实例、群聊、
  Provider 批量导入、OAuth、Task/知识库、全局审批模式和 Workspace Skill symlink 均延期到对应阶段，
  客户端 demo 不能作为后端契约证据。

### 2026-09-01

- 补齐第三种已知 v8 run-scope preview 的精确收编：只接受旧 PromptBlock provenance guard 与缺失
  Review/Sidecar scope/identity triggers 的完整已知形状，升级后恢复正式 guard、四条 run trigger 和
  冻结 checksum；未知 schema 漂移继续拒绝。
- 修正 Review/BTW 启动前确定性 `stream_error` 的 M0 Receipt：按 not-found、validation、policy 保存
  安全 FAILED 404/400/403 和 `recovery=none`，相同 key 重放持久失败且不再次创建资源或执行 Provider。

### 2026-08-31

- 新增冻结 `phase1d.v1` Slash Command Registry 与四个正式类型化命令；`/init` 只登记当前 Core 的
  Workspace，clear/compact 只推进追加式 Context Baseline，均继续使用 M0 Receipt/Action Hash/Audit；
- 新增严格只读 `/review`，复用既有 Session 预算、审批、取消与恢复边界，并把结果保存为 immutable
  sensitive Artifact；新增 BTW Sidecar 的冻结 Thread 视图、独立 Agent/Revision、空 ToolPolicy、
  资源级 Cursor 回放和显式幂等 Steering 提升；
- 新增冻结 manifest/checksum 的 SQLite v8、六张表和 append-only/scope trigger，支持 v1—v7 保留数据
  升级、三种精确 preview 收编、失败原子回滚与受限空表 downgrade；`THREAD_ITEMS` Compaction 可在
  同 Session/Thread 跨 Agent 消费，但普通 ContextRevision Compaction 继续同 Agent；
- 本阶段未实现 Skill、MCP、Scheduler、Graph、Team、Remote/Relay 或客户端，也未修改目标架构、
  `clients/`、`sdk/`、前端和 UI 设计文件；
- 新增短时对象/操作/敏感级别 Artifact capability：完整性校验后的脱敏文本读取、固定安全文件名原文
  下载，以及绑定 Workspace root/父目录 inode 链的不覆盖原子导出；HTTP 不签发 capability，不返回
  storage key、本地路径或未授权正文；
- 新增 Artifact Retention Policy、Pin、归档、宽限期、计划删除、可恢复 Trash/恢复、保护引用扫描和
  CAS 状态推进。物理删除默认关闭并要求独立 trusted bootstrap + 短时 capability；unlink 后状态未知
  进入 M0 人工核对，凭原 Command ID/Action Hash 和 Blob 已缺失事实显式收口，不自动重放；
- 新增严格零写 Artifact 审计与显式孤儿修复，识别 orphan/missing/corrupt/unsafe/purged residue，修复前
  在跨进程 mutation lock 内复核精确 finding；自动化验证只使用临时 Store，没有清理真实 `.operant/`；
- 新增只追加 CacheObservation，记录 Provider 明确返回的 hit/miss/unknown、可选 Token、请求/前缀 hash
  与失效原因；缺失 usage 保持 unknown，不保存 prompt、response、原始 cache key 或 Provider Cache；
- 新增冻结 manifest/checksum 的 SQLite v7、保守 legacy Policy/active projection 回填、只追加审计和
  CacheObservation、受限空数据 rollback，并覆盖 v1—v6 升级、事务失败、并发、权限、完整性、
  幂等、未知删除与恢复边界；未实现 Graph/Team/Remote/Relay/GUI/TUI/Tauri、复杂 `@` 或客户端生成。

### 2026-08-30

- 新增 `ContextRevision`、版本化 `PromptLayout`/有序 `PromptBlock`、动态 Context Watermark、追加式
  `Compaction`、Reference Binding 与 Tool Result Stub；每次 Provider 请求前保存与实际安全输入一致的
  不可变证据，Provider 失败仍可查询，Canonical Thread/Turn/Item 从不被压缩记录删除或改写；
- Composer 按冻结 context window、输出预留、工具 schema 与动态安全余量计算 Green/Yellow/Red/
  Emergency/Unknown；缺失容量或预留保持 unknown。大 Tool Result 优先写入内容寻址 Artifact 并提供
  可恢复 Stub，Red/Emergency 再对已有 Revision 追加结构化摘要，首次请求不伪造 Cursor；
- 新增 Thread/Item/Artifact/Memory 四类最小显式引用与 inline/metadata 模式，执行 Workspace、归属、
  sensitivity、Memory scope/version/hash、Artifact 完整性和 redaction 校验；Memory Block、Binding、
  source snapshot 与 ref 集合保持一致，历史冻结证据不受后续 Thread/Memory 状态变化破坏；不继承父
  Thread 全文，不实现复杂 `@`；
- 新增冻结 manifest/checksum 的 SQLite v6、四张只追加表、FK/UNIQUE/CHECK/index/trigger、并发请求
  幂等、完整 Layout/必需 Block 回放、全部类型化 source ref 的实体/scope/version/hash 防伪、
  `THREAD_ITEMS` 确定性 ID 与精确 coverage
  校验、v1—v5 保留数据升级和空表受限 rollback；Session run 仅新增可选 thread/references，
  `model.completed` 仅新增可选 revision ID，并提供不含 Tool Result 摘要正文的 metadata-only Query；
- Phase 1B 核心、迁移、并发、安全和旧契约回归已加入自动测试；未实现 Graph、Team、Remote/Relay、
  GUI/TUI/Tauri、客户端生成、Provider cache 或任意模型流位置恢复，未修改 `clients/`、`sdk/` 和前端文件。

### 2026-08-28

- 建立正式 Thread/Turn/Item Canonical History：父子关系、不可变 Workspace 绑定、终态/归档、八类
  类型化 Item、Thread 内稳定 position 和 SQLite Cursor；Turn/Item 只追加，终态 Thread 禁止继续写入，
  旧 Session/Workflow 只允许核验后的显式 mapping，不自动伪造历史；
- 新增冻结 manifest/checksum 的 SQLite v5，包含 Thread/Turn/Item、Artifact blob/metadata/source refs 与
  legacy mapping 表、完整 FK/UNIQUE/CHECK/index/trigger、自增 Cursor 和受限空表 rollback；支持
  v1/v2/v3/v4 保留数据升级，并精确验证/原子收编未合并的旧 Phase 1A preview；
- 新增内容寻址 Artifact Store：SHA-256 派生路径、同文件系统临时写入与 hardlink 原子发布、fsync、
  并发去重、完整 hash/size 校验、逐组件 no-follow/真实大小写/inode 检查；root/shard/target 并发替换、
  路径穿越、软链接和非普通文件均安全失败，公开对象/API 不返回正文、storage key 或真实本地路径；
- 新增 Thread/Turn/Item/Artifact Application Service 与 REST/只读 SSE；修改请求继续使用 M0
  Idempotency-Key、Action Hash、Receipt 和统一错误，Query 使用开区间 Cursor，Thread Item SSE 支持
  `Last-Event-ID`。Artifact 上传以纯 ASGI 有界读取拒绝缺失/伪小 Content-Length 的超限 stream；
  Tool Result、Artifact source 和 legacy mapping 在 Store/trigger 双层核对，不能伪造 Canonical 关联；
- 新增 Phase 1A migration、并发顺序、append-only、八类 Item、preview、Receipt/SSE、Artifact 去重/
  损坏/上传限额和路径竞态测试；完整门禁为 237 通过、1 个 Docker 条件 skip、1 个上游 warning，
  Ruff format/check、mypy、`uv lock --check` 和 `git diff --check` 全绿。未实现 Phase 1B Context/
  Compaction、Graph、Remote、Relay、GUI/TUI/Tauri 或客户端生成，未修改 `clients/`、`sdk/` 和前端文档；
- 新增冻结 manifest/checksum 的 SQLite v4：`session_run_leases` 以 owner、token、generation、TTL、
  Agent/Workflow 绑定和 cancel bit 提供跨服务进程 Session single-flight；`workflow_execution_leases`
  为协调器起步、阶段间隙和长 Provider 等待提供独立 guard，旧执行者不能续期、释放或创建新 child；
- Agent Loop 强制累计 completion Token、成对冻结价格计算的精确费用和 Tool Call 预算；整次运行时间
  继续由 Service 绝对 deadline 强制。依赖 usage/价格的硬预算遇到 unknown 时产生
  `budget.exhausted`，并在 `tool.started`、Receipt、审批和副作用之前停止；
- Workflow 取消改为在同一事务内持久化 `cancelled` 并 fence 全部活跃 child lease，再唤醒本进程任务；
  并行 Explorer、其他活跃 Agent 和后续阶段均停止。启动和事件推进使用状态 CAS；guard 丢失会取消
  child、条件写入 `interrupted`，不覆盖用户取消，也不自动重放结果未知的 Coder/Tool 写入；
- Model Profile 增加可选成对输入/输出单价，Budget 增加可选 `max_tool_calls`，Model usage 三个计数可
  分别保持 `null`；Trace 与 Evaluation 不将缺失计数改写为 0。REST/SSE 既有成功响应保持兼容，未知
  写入 lease 回收返回人工核对语义；客户端增量契约记录为待 OpenCode 确认的 COM-20260827-007；
- 新增预算、并发 acquire、租约过期/ABA/fencing、双实例初始化、崩溃恢复、重复取消、并行取消、
  guard 丢失、未知写入和恢复边界测试；最终门禁为 194 通过、1 个 Docker 条件 skip、1 个上游 warning，
  Ruff format/check、mypy 和 `git diff --check` 全绿。未实现 Graph、Remote Control、Relay、GUI、TUI、
  Tauri 或任意模型流位置恢复，未修改 `clients/`、`sdk/` 和前端设计文档。

### 2026-08-27

- 建立 checksummed v1/v2/v3 SQLite Migration：逐版本冻结 manifest/checksum，完整核验受管对象、DDL、
  列/约束/索引/trigger/FTS integrity/AUTOINCREMENT/外键数据，识别并保留升级真实 Week 1 与完整
  Week 1—4 数据库，只精确收编已知 preview；全部步骤单事务完成，并限制 v3 rollback 只能用于显式
  isolated 且 M0 表为空的数据库；
- 新增 REST Command Receipt：修改请求持久化 `Idempotency-Key`、规范化 Action Hash、HTTP 结果和
  恢复状态；支持相同命令结果重放、不同命令冲突、执行中重试提示和未知结果人工核对；
- 新增持久化 Tool Action Gateway：`apply_patch`/`run_command` 在副作用前预留 Receipt，重复
  Tool Call 重放安全结果，进程重启后的不确定动作拒绝自动重放；
- 将审批请求、唯一决定、过期和只追加审计落入 SQLite；创建时强制 pending 并事务核对 Receipt 上下文/
  状态，执行时同时要求持久正向 Decision、精确 Agent/Receipt/Tool Call/Action Hash；修复决定落库早于
  Future 创建时的竞态，但进程内 Future 和模型流仍不支持跨进程恢复；
- Session、Workflow、Evaluation 事件统一使用真实 SQLite Cursor，增加开区间 Query、SSE `id` 和
  `Last-Event-ID` 已提交事件回放；SSE Command 只在 256,000 bytes 内的完整首帧能验证已持久资源与
  Cursor 后 accepted，同 key 重试返回 202 JSON Receipt 与 `replay_url`；明确 SSE 断线不保证后台继续；
- 为修改型 REST Command 增加统一安全错误信封与恢复建议；首次非流响应与 replay 使用同一安全
  payload，JSON/文本/空 body、REST 4xx/5xx、Tool/命令输出、Receipt、事件和模型反馈统一 bounded
  redaction；尾斜杠 307 不预留 Receipt；服务器生成 key 仅界定当前响应，客户端跨请求去重仍必须保存
  并复用原 key；
- 扩展短 Bearer 与任意/不完整 PEM 私钥块脱敏，修复 `.ENV`、`.NPMRC`、`ID_RSA`、
  `CREDENTIALS.JSON` 等大小写变体在 Linux 上绕过文件工具/Docker 快照过滤的问题；
- 增加 Session single-flight：API 在 SSE 头前返回同 Session JSON 409，Service 层同样守卫；重启后的
  Pending Approval 在决定或过期前阻止新 run，不同 Session 仍可并发；
- 新增 M0 persistence/API/protocol 自动化测试；未实现 Graph、Remote Control、Relay、GUI/PWA、TUI、
  Tauri 或 Client SDK，也未把现有本地 Web/API 暴露到公网。

### 2026-08-25

- 完成第四周 Evaluation Runner v1：新增 Suite/Case/Variant/Run/Result、声明/实际快照、验证结果、
  指标聚合与五类 Trace 根因分析领域契约；
- 增加逐 Result 隔离 artifact、凭据/缓存/越界软链接排除、白名单外部验证和进程组超时清理；
- 支持固定单 Session 或完整 Workflow，以及模型、Prompt、effort、Memory 对照和 Exp 19—24 Suite；
- 增加五张 SQLite 表、唯一 Pending→单一终态 Store 契约、Run/Result 原子重启中断、CLI 与
  FastAPI/SSE，并对外移除本地 artifact workspace 路径；
- 修复 reviewer 发现的中断审计空窗：组合执行前即保存 Pending，同一 ID 写终态；取消、流关闭或进程
  重启均收口为不伪造实际事实的 Interrupted，聚合保留完整 Suite 分母；
- Eval Workflow 只读取精确源 workspace 的 active Project Memory，禁用候选回写；角色漂移在模型
  调用前停止并保存实际快照；
- 新增领域、持久化、Runner、Workflow、RCA 和 CLI/API 自动化测试；未运行真实模型 Exp 19—24，
  未修改 Web、GUI、Graph、Remote Control 或其他前端/2.0 后续功能。

### 2026-08-25（目标架构与治理）

- 明确当前实现文档、目标 Harness 架构与目标客户端设计的三层权威关系；
- 记录 React GUI、Textual TUI、Tauri 桌面壳、通用 Graph Runtime 和 Team/Mailbox 尚未实现；
- 将目标 Remote Control 与 Remote Execution Target 分离：前者使用手机/浏览器控制本地 Core，后者
  才在授权远程主机执行；目标支持直连和用户自托管 Relay，但当前均未实现；
- 明确 Operant 2.0 保持单用户、单 Core、本地 SQLite 权威，不把 SaaS、多用户协作或分布式 Core
  写入短期范围；
- 移除已完成且与现行目标文档竞争权威的旧 v1 项目计划、工程计划、学习计划和项目总结；
- 本次只同步设计与治理文档，没有改变当前 Runtime、API、SQLite、Web 工作台或安全行为。

### 2026-08-22

- 完成第三周多 Agent、Memory、恢复、可观测性和基础 Web 工作台工程；
- 将固定流程扩展为 Planner → 一个或多个只读 Explorer → Coder → Reviewer；
- CLI/API 默认在 Reviewer 批准后运行可替换的只读 `role_main` 完成最终汇总；
- 支持最多 4 个不同 Explorer Role，并用信号量限制只读并行；任何可写角色都不能进入并行槽位；
- 增加带 Role ID、Session ID、状态、摘要、完成步骤和失败原因的结构化子任务结果；
- Explorer 失败可以交给后续角色处理，Planner、Coder 或 Reviewer 失败则安全停止；
- 将 Workflow 任务、阶段和有序事件持久化到 SQLite；支持安全检查点恢复、取消和 Coder 未知写结果
  的人工核对状态；
- 实现 Working/Episodic/Project Memory、不可变版本、来源和角色/项目作用域、FTS5 检索、候选确认、
  保守自动激活和停用；
- Provider 与 Runtime 增加 usage、模型/工具耗时和清洗后的失败事件，新增 Session/Workflow Trace
  聚合与脱敏 JSONL 导出；
- CLI/API 增加任务查询、事件、Trace、恢复、取消和 Memory 管理入口；
- 新增不依赖 CDN 的本地 Web 工作台，覆盖模型/角色、Session/Workflow、审批、分栏 SSE、任务回放、
  Trace、恢复和取消，并用真实浏览器完成视觉与交互检查；
- 完成 Kimi K2.6 与 Gemini 3.7 Flash 的六角色真实 Workflow 验收，独立 unittest 和 diff 复核通过；
- 新增并发、恢复、Memory、Trace、Web、CLI/API、usage、失败清洗和权限回归测试；工程完成不代表用户
  已完成第三周学习，学习状态仍由源码核对、练习和复述验收决定。

### 2026-08-19

- 完成第二周 Coding Loop、自我纠错与安全执行工程切片；
- 测试失败会产生有限的结构化反馈，并在连续相同失败时通过 `agent.no_progress` 停止；
- 增加 Host / Docker Command Runner，新初始化的默认 Coder 使用 Docker 过滤快照、无网络、资源限制和
  进程/容器清理路径；
- 扩展 Tool Policy 的 Shell 与数据库删除审批，并让输出截断显式可见；
- 修复所有 `max_rework_rounds` 取值下缺失 Reviewer verdict 的事件一致性；
- 新增 `SECURITY.md`，并增加 Docker 边界、超时、结构化修复与无进展的自动化测试；
- 该工程切片完成时本机尚无 Docker CLI，真实容器集成测试按条件跳过；这是当时的环境状态。
- 随后按官方默认方式安装 Docker Desktop 4.87.0，以 `python:3.13-slim` 跑通真实 Docker Runner
  集成用例；完整测试为 32 通过、1 个上游 Starlette 弃用警告。尚未运行含真实模型的完整 Workflow
  端到端验收。

### 2026-07-30

- 建立第一版项目架构与实现说明；
- 记录当前领域模型、调用链、SQLite、工具权限、CLI/API 和 Workflow；
- 将“每次项目更新必须同步检查本文档”设为项目维护规则；
- 将开发图谱工具升级到官方 `v0.9.1-rc.1` 并重新注册；Operant full 索引及符号、调用链、
  架构查询验证通过，移除对应技术债务；
- 移除安装器注入的用户级全局 AGENTS 块，改用按需加载的 `codebase-memory-mcp` skill；
- 完成 Model/Role CRUD、默认角色、会话覆盖、总超时、取消和可恢复审批；
- CLI/API/Workflow 统一通过 Application Service；
- 完成 21 项自动化测试和真实三模型 calculator 端到端验收；
- 证明角色更新后旧 Session 的 Role Snapshot 保持不变；
- 增加安全 `.env` 加载和 origin Base URL 的 `/v1` 自动补全。
