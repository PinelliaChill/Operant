# 开箱即用与界面整理验收

> 2026-10-08；记录身份：Codex 主线程；适用对象：本轮实现与验收 Agent。
> 分支 `codex/onboarding-ux`；基线 `939e3cc`；完整范围见[批准方案](plan.md)。实施中，整轮未完成。

| 需求 | 实现状态 | 验收状态 | 场景与结果 |
| --- | --- | --- | --- |
| UX-01 会话标题 | 已实现 | 通过 | S2 自动标题、手动优先、CAS、旧历史、数据库重开；原生标题、侧栏及改名后重开通过。子对话及分页外标题由正式查询测试覆盖 |
| UX-02 默认角色 | 已实现 | 通过 | S1 原生干净库连接后初始化五个角色，通用助手直接接收任务；已有配置保留由回归测试覆盖 |
| UX-03 统一新建对话 | 已实现 | 通过 | 两个入口共用初始化命令；S2 原生双击只新增一个 Thread/Session，没有新增 Agent 或发送草稿 |
| UX-04 自动初始化运行 | 已实现 | 通过 | S1 原生自动准备运行并完成文件任务；S2 丢创建响应后保留原请求，退出重开再只读核对，只新增一个 Thread/Session，没有自动运行或发送草稿 |
| UX-05 简化模型连接 | 已实现 | 通过 | S1 用户在独立原生窗口保存密钥，发现并选择模型后返回原草稿，直接发送任务；无需角色、系统提示词、环境变量名或绑定运行 |
| UX-06 高级参数与默认值 | 已实现 | 通过 | S1/S6 温度不发送、未知上下文不指定；原生表单默认收起高级参数，服务商切换通过 |
| UX-07 ChatGPT/Gemini OAuth | 部分 | 受阻 | ChatGPT 原生登录页与取消通过；用户批准授权后，官方组织策略拒绝，未返回 code/token。调用/续期/撤销未验；Google 应用客户端与用户项目待提供 |
| UX-08 自动技能目录 | 已实现 | 通过 | S4 默认来源、缺目录、坏项隔离通过；最新隔离 Core 重启后仍扫描已登记的个人工作区，发现标准格式验收技能，21项候选；用户来源根目录变化会停用并要求重新添加 |
| UX-09 标准技能格式 | 已实现 | 通过 | S4 普通 metadata、多行 description、资源、损坏 YAML、大小和解析安全边界通过 |
| UX-10 技能链接与去重 | 已实现 | 通过 | S4 登记根内顶层链接、真实路径去重、目录外拒绝与添加来源通过；资源内链接仍按安装完整性边界拒绝 |
| UX-11 内置技能就绪 | 已实现 | 通过 | S4 两个工作区六技能进入新快照；正式 gpt-6-luna documents 命令经一次审批产出 Word，正文回读通过 |
| UX-12 空会话状态 | 已实现 | 通过 | S2 未绑定时不查询技能命令；原生新对话显示输入入口，没有技能读取错误 |
| UX-13 本机能力使用流程 | 已实现 | 通过 | S5 第六轮网页观察、导航、填写、点击、结果读取通过；第五轮应用读取、输入、按钮点击通过。运行取消、人工接管、新租约恢复后重新读取及最终撤销通过；临时应用已退出并移回验收目录。早期失败保留，未重放旧动作 |
| UX-14 唯一设置与导航 | 已实现 | 通过 | S6 四入口、六设置与六个旧别名已核对；项目文件/知识与配置分离，原生页内安装引导和跳到主内容保留路由、恢复焦点；105项文件目录分页通过 |
| UX-15 共用选择组件 | 已实现 | 通过 | S6 原生深浅主题、1280/700/390 宽度；模型/项目/模板选择的搜索、方向键、Enter、Escape、焦点和弹层位置通过 |
| UX-16 基础团队模板 | 已实现 | 通过 | S5 正式三成员完成；旧详情和回复缓存问题已修。最新原生点击回复自动刷新到16对话，显示审查确认的正确标记和 APPROVED；原生恢复/取消按钮均禁用，运行图默认折叠 |
| UX-17 精简文案 | 已实现 | 通过 | 日常四页面、记忆、技能、审计和能力入口已回读；系统编号、参数和手动控制默认收起，普通错误中文化。最后知识文案“保存和查找项目知识”“已保存与待确认”在最新构建回读通过 |
| UX-18 演示开关归位 | 已实现 | 通过 | S6 默认实时连接，原生日常侧栏没有演示开关；开发设置保留演示入口 |

## 场景与环境

- S1：干净库启动、连接、默认工作区与角色、首条消息及重启。
- S2：创建/初始化幂等、自动名称、改名、切换及断线/未知结果。
- S3：ChatGPT/Gemini 登录、取消、目录、流式工具调用、续期与撤销。
- S4：技能目录、YAML、链接、重名、坏文件、来源变化、安装权限及真实产物。
- S5：原生本机能力启用、目标选择、实际操作、接管/取消；基础团队任务。
- S6：导航、旧链接、选择器、键盘、主题、宽窄屏和文案。

正式模型先经 CLI 发现，使用精确 `gpt-6-luna` 和正式 ModelProfile/Session/Graph 入口。
测试数据、凭据、Core 和桌面候选均隔离；SQLite v24及新增v25迁移只用于隔离测试库，未改真实用户库。
候选是独立调试 App，连接隔离 Core 和固定前端构建；尚非自包含发行包，未公证。
自动测试不替代真实模型、OAuth 或系统操作。外部条件缺失仍保留在原需求中，不能算通过。

## 实际流程与证据

证据在实施树 `.operant/onboarding-ux/evidence/`。凭据仅存独立测试目录的 0600 `.env`，
不进入 SQLite、日志、证据或 Git。

| 场景 | 输入、操作和实际结果 | 证据文件 |
| --- | --- | --- |
| S1 | 干净库正式 API 连接、发现、选模型、初始化；Session 读取 welcome.txt 并流式回复 OPERANT_ONBOARDING_READ_OK，agent.completed 与 tool.completed | first-use-real.json、first-message-events.json |
| S1 | 独立原生 Verification 干净库：用户保存连接，发现39个模型、选择 gpt-6-luna，自动准备五角色及个人工作区；原草稿返回后读取 welcome.txt，工具和 Agent 完成，界面回复 OPERANT_NATIVE_SETUP_OK。并发标题读取曾留下误报，修复后的候选重开不再显示 | native-verify-connection.json、native-verify-setup.json、native-verify-first-use.json |
| S2 | 首条消息形成自动名称，改为“开箱即用验收”并重开；原生双击新建仍保留未发送草稿，只新增一个 Thread/Session，改名为“草稿保留验收” | native-new-dialogue-counts.json；持久/CAS/幂等测试 |
| S2 | Verification 独立代理在服务端提交后丢弃响应。原生禁止再次创建、保留当时草稿；退出重开仍有核对入口，按原请求只读查到并打开同一对话。创建 POST 仅1次，Thread/Session 各增加1，新 Session 没有事件或 Agent。草稿仅驻内存，退出后未保留；没有自动发送 | verify-fault-inputs.json、verify-creation-dropped-response.json、verify-creation-request-count.json、native-verify-creation-recovery.json |
| S4 | 新对话显式调用 documents；正式 Gateway 命令经一次审批生成 report.docx（36,739 bytes），Word XML 标题正文正确 | documents-real.json、documents-events-v3.json |
| S5 | 原生基础模板输入任务，规划、执行、审查均 succeeded；首轮文件结果正确，补验轮经正式 Graph 深链看到 completed 和三个节点 | team-real.json、team-nodes.json、team-frozen-attempt.json、team-frozen-real.json |
| S5 | Verification 只授权自有临时网页，原生安装、启用浏览器并绑定新对话成功；真实 gpt-6-luna 首次 ext_browser_observe 因 Phase45 审批未接到 Session 而失败，未创建远端 Job。修复加入持久 Session 审批、精确内层动作核对和原键；当前自动检查不能替代修复后原生复验 | native-verify-browser-attempt1.json、verify-browser-control.json、bridge-final-pytest.log/status.json |
| S5 | 第二次原生运行等待审批60秒后流断开并取消；新增 SSE 注释心跳。第三次在正式对话等待100.51秒后仍可批准，网页观察完成；下一次打开网页已批准但运行失败，没有重放旧动作。心跳和读取通过不代表整项本机操作通过 | native-verify-browser-attempt2.json、native-keepalive-wait.json、native-verify-browser-attempt3.json |
| S5 | 第四轮在密文修复候选重新安装浏览器后，观察成功；导航在有效期内获批并创建Job，10秒后进入manual_reconcile_required/remote.outcome_unknown。临时网页仍HTTP200；填写/点击未执行。原生控制会话配置visible=True；普通Chrome选择读到的是另一用户窗口，不能用来验收，尚未定位目标窗口。未重放、未虚构人工已执行/未执行核对；此前应用控制会话已通过原生Gateway关闭并撤销控制权限 | native-verify-browser-attempt4.json |
| S5 | 用户确认并提供独立验收窗口截图：URL为127.0.0.1:18780，输入为空、Waiting for the test marker仍在，证明导航已执行。原生入口记录applied并经Gateway批准后，待核对列表为0；原Job仍manual_reconcile_required且只有1个，没有重放。源码发现根URL无末尾斜杠与Chrome自动补斜杠的严格比较导致回执超时，修复及正常链路仍待验收 | native-verify-browser-attempt4.json、user-browser-navigation-confirmed.png |
| S5 | 第五轮在根URL修复候选运行：观察与导航Job正常succeeded；填写批准后Phase1EError，没有填写Job。保存的初始观察尚未过期，但导航返回的新posthash未持久化；历史填写参数已脱敏，不能断言当时使用了哪个hash。旧动作不重放，关闭本轮控制会话后正式客户端回读closed、活跃0、待核对0。新增v25来源表和完整调用修复仍待新原生验证 | native-verify-browser-attempt5.json、native-browser-attempt5-control-closed.json |
| S5 | v25候选第六轮通过正式原生对话和 gpt-6-luna 完成 observe→navigate→fill→click→observe，五个Job均succeeded且各有来源。输入的精确标记由DOM表单摘要核对；随后观察确认结果从Waiting变为脱敏的输入回显。没有通过CUA代填或点击网页，待核对0 | native-verify-browser-attempt6.json |
| S5 | 同一浏览器对话发起新任务，在等待读取审批时原生点击取消；持久事件为agent.cancelled，旧审批禁用，没有新增Job，非300秒超时 | native-verify-browser-run-cancel.json |
| S5 | 用户明确授权自有临时 Cocoa 应用；正式应用列表搜索、方向键选择、焦点恢复、安装/启用及卡片创建新对话成功。两个真实 Job 返回成功，随后核对观察正文却为 Window/WindowSharingSessionButton/空字段，与原生测试窗口不符；不计为读取正确窗口。待点击审批时达到300秒任务时限，之后点击取消未形成取消结果，不计通过；标记仍为空 | computer-fixture-registration.json、native-verify-computer-attempt1.json |
| S5 | 第二轮应用观察正确返回 Operant Control Fixture、Apply marker、Verification marker；填写获批后以 Phase1EError 失败，没有输入 Job。时间线排除观察过期：批准早于到期16秒；内层请求已批准但未消费，继续调用重新生成密文导致动作哈希变化。保留原运行，不重放输入，修复后须新任务验收 | native-verify-computer-attempt2.json |
| S5 | 密文修复候选重新安装应用驱动后，新任务的观察 Job 返回 computer.target_rejected，Agent以 CapabilityOperationError 结束，未进入填写。原生测试窗口仍存在，未见新权限提示；目前错误码合并了多个原因，具体原因尚未证实，正补稳定原因码。不能计为输入修复的真实通过 | native-verify-computer-attempt3.json |
| S5 | 第四轮仍返回computer.target_rejected，未进入填写。源码定位到单独激活后读取当前前台进程的竞态；历史Job未保存前台身份，不能断言当时误读了哪个应用。修为按明确Bundle ID定位，并在读取窗口前后核对授权与目标；不扩大应用或剪贴板权限 | native-verify-computer-attempt4.json、computer-frontmost-fixed-20261008-status.json |
| S5 | 修复候选第五轮真实读取、输入、重新观察、按钮点击共四Job成功；独立CUA仅核对结果，输入框与结果均为OPERANT_COMPUTER_NATIVE_OK。首次点击在分发前观察过期，无Job；模型重新观察并申请新点击审批后完成，旧动作没有重放 | native-verify-computer-attempt5.json |
| S5 | 最新应用候选原生接管后进入human_operating且读取禁用；批准恢复后产生新租约/栅栏、清除旧观察。同一冻结Session经真实模型重新读取，正确返回窗口标题和按钮，未重放原输入或点击 | native-verify-control-resume-final.json |
| S5 | 正式已安装SDK经精确Gateway审批关闭控制会话，并停用、卸载两项临时驱动；五次审批后活跃控制0、安装驱动0、待核对0。临时应用已退出，登记副本可恢复地移回验收目录，原用户环境未改 | fixture-access-revoked-final.json、fixture-application-retired.json |
| S5/S6 | 原生控制面板接管获批后显示“人工操作中”、禁止观察；恢复获批后回到 active，须重新观察。最新失败对话新建出无消息的干净对话，没有旧错误或自动发送。插件安装参数、详情、空数据和手动操控台默认折叠；旧远程链接自动打开唯一高级页的远程区。旧流程编号完整保留；测试库不存在该历史运行，裸英文错误已修但待新构建挂载复验 | native-verify-ui-final-increment.json、native-verify-computer-attempt2.json |
| S4 | 最新安装 wheel 的 Core 重启，八个默认来源恢复，缺失工作区目录正常停用；标准 metadata/多行说明的 onboarding-restart-check 被发现，21项候选，未自动安装外部验收技能 | verify-session-keepalive-startup.json、skill-verify-audit.json |
| S6 | 原生模型表单、六设置、五角色、深浅主题和三种宽度；Select 弹层在窗口内、键盘选择与关闭后焦点恢复 | 原生界面回读；GUI 选择器及路由测试 |
| S6 | 原生记忆设置默认只显示全局/项目开关；展开高级区后可进入唯一高级设置。项目内容页不再展示常驻审计说明 | native-verify-ui-copy.json |
| S6 | 根对话链接发送成功，精确回复ROOT_CHAT_ROUTE_OK，没有误报切换；项目文件welcome.txt正文45B正确，深浅主题、1280/700/390宽度与选择器Escape焦点恢复通过。首轮CUA输入丢字如实保存，第二轮核对准确文本后发送 | native-project-files-root-route-final.json |
| S6 | 页内安装应用/网站按钮不改变设置路由，Tab到对应目标；跳到主内容不改路由。105项合成目录首批100项，继续加载后可打开第105项正文；没有把第一页当完整目录 | native-final-navigation-files.json、project-pagination-fixture.json |
| S6 | 最新普通能力页手动控制默认关闭；任务三标签、定时任务中文触发和高级折叠、协作只选模板输入任务、记忆常用开关、审计说明删除和知识配置分离通过 | native-daily-copy-delivery-candidate.json |
| S6 | 最新知识文案为“保存和查找项目知识”“已保存与待确认”；空状态提供添加入口，高级整理默认关闭，无写操作 | native-knowledge-copy-final.json |
| 构建 | 冻结 wheel 的 278 项源/SDK/协议/技能文件逐项一致，隔离安装后导入路径与健康接口正确 | wheel-frozen-readback.json、frozen-runtime-import.json、frozen-runtime-health.json |

中间构建证据对应契约 `234d2316…`。最新 Core wheel 已重建，278 项逐文件一致；
摘要 `839d53cfd8ca190d36226176d31500fa2ec42d78812bc1324d1554fba60d8e4c`，见 wheel-final-readback.json。
隔离安装后的实际导入、健康与新协议通过，见 final-runtime-import.json、final-runtime-health.json；
原生二进制已加入受限登录命令，前端最终补验仍在完成。提交 `848ecf3` 和对应运行候选的
`onboarding.v1` 有 21 个操作，摘要
`aac48a78b39f04a1a3d84741bb18a226c558f0026c06332ca03c572188f50ecf`。
当前源码新增对话专用控制开启与原请求查询，共 23 个操作，摘要
`aae5c5d98b0fda75c68fae94307b81db81df8e8a486a8eece40c0cbe60a5ccdc`。
独立 Verification 已安装 `468c9db` 的新 wheel，281项（含许可）逐文件一致，摘要
`a601fd7256aa077651f8ac0b0a6a735b636a440b935ac4fe83571e4d992cddc0`；
Core 18778、前端3028、verify-state测试库与原候选分开，实际导入、健康及23操作协议通过。
见 local-control-wheel-readback.json、verify-runtime-health.json、native-verify-build.json。
当前修复候选 wheel 的282项源码、SDK、Schema、内置资源和许可逐项一致，摘要
`d46519db5c79d2be4ad5f20540350cebeff11af0d83e3c04153ce7fdb0e9b27e`，见 approval-bridge-wheel-readback.json。
使用新的 runtime-approval-bridge 与原隔离测试库，替换前仅备份测试数据库，未复制凭据文件。
前端 gui-approval-bridge-dist 独立构建并挂载3028，包体检查通过；原用户 Core/窗口不重启。
随后心跳与技能源边界候选安装到新的 runtime-session-keepalive；282项输入逐文件一致，wheel摘要
`4712890e4dd06f3aab6db940bdd5eee0fef4cbccbe4cb8cfbee78373bd92df3a`，见 session-keepalive-wheel-readback.json。
上述中间前端为 gui-ux17-copy-final-dist；更换前仅备份隔离数据库，不复制凭据。
此前已运行候选为 runtime-native-ux-final，wheel摘要
`d740b8bb76b4f2e9efbadafb9607d21bb2bb20e466d40db2621ea3404e54f797`；282项安装输入一致，
固定前端为 gui-native-ux-final-dist。见 native-ux-final-wheel-readback.json、native-ux-final-runtime-import.json、
native-ux-final-startup.json。此候选含窗口选择和明确过期处理，尚不含随后发现的输入密文续行修复。
随后密文续行候选已安装并实际运行，runtime-native-input-final，wheel摘要
`51886b26625e2e0adc1195c8bfc72d971e67db1f0611d379baaee1dcca6936af`；282项安装输入一致，
前端 gui-native-input-final-dist 已挂载，190项测试、类型、构建及包体预算通过。新原生表单回读确认
中文触发名称、执行规则与版本默认折叠；旧流程保留编号，错误已中文化。见 native-input-final-wheel-readback.json、
native-input-final-runtime-import.json、native-input-final-startup.json、gui-native-input-final-report.json。
随后实际运行的候选为 runtime-capability-outcome-final，wheel摘要
`e6bcc40866d12fc717537592f70a764489e73af9b96408c43f02dd7444099bfb`，282项逐文件一致；
GUI gui-capability-outcome-dist 已挂载，191项、类型、构建和预算通过。该候选包含明确应用观察原因、
未知结果终态与根URL回执修复，不包含随后新增的v25后置观察来源修复。
见 capability-outcome-final-wheel-readback.json、capability-outcome-final-startup.json、
gui-capability-outcome-report.json。
原用户候选 Core 18768 未重启。一次 GUI 构建错误写入3018的静态输出，随后按848ecf3源码恢复，60项文件与恢复构建一致；没有刷新原窗口、改变数据库或复制凭据。见 original-gui-output-restored.json。后续 Verification 构建使用独立输出目录；独立代理18779只用于故障验收。

最新 Core 为 runtime-computer-bound-observation，wheel摘要
`a7fa123cded876ed9b84a61b5d5e571200aa14718ab1a168f154c2835243ed60`；283项源、SDK、Schema、
资源与许可逐项一致。隔离库已升至v25，外键检查为0、待核对0；原Core18768和用户库未动。
见 computer-bound-observation-wheel-readback.json、computer-bound-observation-runtime-import.json、
computer-bound-observation-startup.json。最终GUI为gui-route-file-final-dist，275项输入、61项输出已冻结；
仅替换3028预览，Core保持运行。其后的最终异常分支补修候选为runtime-postcondition-final，wheel摘要
`3e24bcde915bdddd57373b686748bd969501fdea1f821f05dd025f2940fdca1e`，283项逐文件一致，
源及安装输入仅controller异常分类分支变化。GUI最终为gui-delivery-copy-final-dist，276项输入与61项输出冻结。
最终候选与测试库完整性见postcondition-final-wheel-readback.json、postcondition-final-runtime-import.json、
delivery-candidate-startup.json及verify-delivery-candidate-processes.json；不触碰原Core或凭据文件。

## 检查记录

检查输出保存在 `.operant/onboarding-ux/checks/`。范围和版本变化分别记录，不相加为整轮结论。

- v25迁移：三项通过，覆盖v24历史保留、旧观察无来源、空库回退、缺表拒绝、三外键及fencing/digest约束；旧1–24摘要未变。首轮测试夹具遗漏既有目标/动作外键失败，已补齐正式Repository父记录后通过；生产外键未放宽。格式、ruff和两源文件mypy通过。日志 observation-v25-migration-final-pytest.log、observation-v25-migration-mypy.log。
- v25来源与完整SDK闭环：**76通过、1条件跳过**；强化ASK前租约断言后6项定向复验通过。成功Job、后置观察及来源原子保存；接管、范围变化、未知结果与无来源旧观察不可继续写。新观察哈希绑定新Job，未延长旧有效期。见 post-observation-v25-final-status.json。
- 明确Bundle ID的macOS读取：**22项通过**，脚本仅编译核对；真实输入、点击和恢复读取另见S5第五轮。见 computer-frontmost-fixed-20261008-status.json。
- 增量冻结检查：格式593文件、ruff、mypy204源文件通过；协议与首次配置6项通过，公开Schema和旧迁移摘要不变。GUI最终**191项**、类型、独立构建及包体预算通过，日志退出码与输入/输出摘要见gui-route-file-final-report.json。最终提交的完整门禁和CodeQL仍待运行。
- 后置条件不匹配的本机非幂等动作：72项、Operator6项通过；可能已执行时为manual_reconcile_required，无新观察/来源，阻断继续派发。普通remote/幂等动作保留FAILED。异常与权限边界按确定性测试验收，不声明该异常分支已做原生故障注入。见postcondition-unknown-status.json。
- 项目文件分页与范围：适配器9项通过，GUI全量193项、类型、构建及包体预算通过，276项输入与61项输出冻结；断线卸载旧正文预览。见gui-delivery-copy-final-report.json和原生105项分页场景。
- 完整 pytest v4：**1380 passed、12 skipped**，退出 0，28 分 46 秒；见 pytest-final-v4.log 与同名 status.json。
  运行开始后新增了单个标题查询、按请求标识核对和模型显示名称，完整结果不作为这些后续改动的通过证明。
- 后续会话、SDK、模型连接和持久检查：**29 项通过**。最后的新旧协议确定性、模型错误/目录及会话查询汇总 **94 项通过**，见 contract-final.log/status.json。
- Python 格式检查 588 文件、ruff、mypy 203 源文件及 uv 锁检查通过；最后发现的长行和导入排序已修，复查通过。
- 本机能力增量：后端 **38 通过、1 条件跳过**，见 local-control-backend-pytest.log、local-control-backend-status.json；
  新客户端与协议确定性 **11 项通过**，见 local-control-sdk.log。GUI **178/178**、类型、独立目录
  构建和包体检查通过，见 gui-local-control-report.json。源码检查见 local-control-static.status.json。
  这些自动检查不作为原生或真实模型调用通过。
- 标题读取与错误提示增量：GUI **181/181**、类型、独立构建、包体与 diff 通过，见
  gui-copy-final-report.json、gui-copy-recovery-report.json。并发旧读取不再误报失败；只读标题警告
  与创建/改名错误分开，标题成功不会清除未知写核对提示。模型连接复用503等未知写判断。
- GUI 最终冻结轮 **169 项**、类型与构建通过；回复刷新、未缓存深链和不存在目录提示已补修，真实团队回复跳转通过。执行者记录见 gui-final-report.json。
- TUI 34 项通过；Rust 3 项通过，覆盖凭据引用、本机 Core 地址和官方 OAuth 地址/绑定回调白名单。
- 提交 `848ecf3` 的 CI 37603131043（Python 3.10/3.12、GUI、TUI）与 CodeQL 37603130641 全部通过。
  两版 Python 均为 1378 passed、16 skipped，GUI 169 项通过。
  此后 UX-13 增加的对话专用控制接口和绑定不在该提交中，需单独冻结验证。
- 提交 `468c9db`：CI 37618788630 的 Python3.10 **1394通过、16跳过**，GUI178通过；
  CodeQL37618784598全部通过。Python3.12在97%触及45分钟作业时限而取消，不能算通过，
  原因见 ci-468c9db-summary.json。下一版将作业时限调整为60分钟、输出10条慢用例，完整测试集不变。
- 提交 `b1a3c8b`：CI 37636666859 双版本及 GUI 全通过；Python3.10/3.12 各 **1394通过、16跳过**，GUI **181项**，TUI **34项**。60分钟时限没有删减测试。CodeQL 分析作业成功，但汇总检查报两项高等级问题：凭据摘要及来源路径；不能算 CodeQL 通过，修复后的扫描仍待运行。
- 当前审批桥定向检查 **47通过、1条件跳过**；模型凭据和完整 Core 中间件 **22项通过**；协议、持久、会话和首次配置 **13项通过**。GUI **182项**、类型和构建通过；Memory 常用设置与高级配置分离，已批准的“继续原请求”直接创建并进入对话。后端、GUI 当前改动仍需冻结候选和新 CI。
- 提交 `6bf0553`：CI 37718265823 全部作业通过；Python3.10/3.12 各 **1417通过、16跳过、1警告**，GUI **182项**、TUI **34项**。CodeQL 分析作业成功，但汇总检查报三项高等级问题：旧摘要兼容核对、对应旧格式测试夹具、来源路径；尚未完成审查处理，不能算 CodeQL 通过。见 ci-6bf0553-snapshot.json、ci-6bf0553-all.log。
- 后续 GUI **187项**、类型与独立构建通过，仅保留当时工具输出。最新冻结 GUI **190项**、类型、独立构建及每资产500KiB包体预算通过，273项输入和60项输出摘要保存于 gui-native-ux-final-report.json；包含错误来源隔离、终态审批禁用和旧路由。首次检查189通过/1失败及类型错误日志保留，修复后重跑通过。随后旧流程错误文案的补修需新构建与回读。
- SSE 心跳定向检查 **23项通过**，包含正式 Session 路由、SDK 流读取、等待审批、取消与租约回收。12秒注释心跳不是持久 Event，不改变 Cursor；第三次原生运行等待100秒后成功继续。见 session-sse-heartbeat-status.json、native-keepalive-wait.json。
- 技能边界 **24项通过**，覆盖旧目录别名/符号链接收据读回、禁止重复登记/扫描、真实目标变化、未知结果及批准前不读取目录；保存后的用户根或祖先被替换为其他链接目标时停用，显式重新登记才能接受新目标。见 skill-boundary-tests.log、skill-verify-audit.json。
- macOS标准窗口选择 **12项通过、1条件跳过**；五段脚本编译检查通过，未执行脚本作为旁路验收。真实模型第二轮观察正确窗口及控件，输入仍失败；见 computer-ax-selection-status.json、native-verify-computer-attempt2.json。
- 密文精确续行 **48项通过**；正式Phase56Client经完整Core/Session/Phase45中间件，网页填写和应用输入批准后各创建一个Job，DENY、接管后范围变化及观察过期均无Job。只在本次工具调用内复用随机密文，finally清空；不持久化、不更改有效期、不重放旧动作。目标执行在此测试中为合成结果，原生第三轮观察失败，实际输入仍未通过。见 sealed-ask-continuation-status.json、sealed-ask-continuation-diagnosis.json。
- 窗口读取原因与未知结果分类初次 **63项通过、1条件跳过**，后续汇总 **66项通过、1条件跳过**；明确FAILED的窗口读取结束为tool.failed并结清收据；结果未知或等待超时保留专用失败类型及原收据，不转普通工具失败。首次历史断言因回放增加规范turn=0失败，修正测试后通过；原生尚待新候选复验。前端映射未知结果为人工核对、不可重试，**191项**、类型、独立构建及包体预算通过，e6候选已挂载。根URL回执另有10项通过，第五轮正常导航的结果见上表；后续v25及日常文案候选尚待新挂载。见 computer-browser-receipt-final-20261008-status.json、gui-capability-outcome-report.json。
- 本机应用扫描负例覆盖顶层/Contents/Info.plist 链接、超限文件和重复应用；模型私有状态只允许不透明签名/加密引用。

## 已发现的问题与处理

- HashRouter页面内的安装/对话入口曾使用裸锚点，误变为未接入路由；修为同页定位与键盘焦点，跳到主内容同时修正。原生安装入口和键盘回读通过。
- 项目文件页曾只取前100项并丢页令牌；现通过正式分页继续读取、校验目录快照并保留顺序/去重；断线清除旧正文，原生第105项文件可达。
- 本机非幂等动作的成功回执若后置条件不匹配，原逻辑误记普通FAILED；现保留人工核对，不发布新观察，不提示模型重试。


- OAuth 轮询曾丢登录链接；改为仅驻内存、取消/过期清除、响应 no-store，关闭含回调 code/state 的原始访问日志。
- 原生链接曾没有打开外部登录页；受限 Tauri 登录命令已在最新候选成功打开 OpenAI 官方 Operant 授权页。用户已批准本次授权，官方组织策略拒绝，未获得 code/token；本机取消入口通过，原 API 连接和对话保留。见 chatgpt-real-auth.json。
- 团队详情曾进入旧 Workflow 页并报 task not found；修为正式 Graph 深链，原生补验通过。最新回复入口虽然 URL 正确，但旧线程投影导致误报找不到对话；手动刷新后看到正确中文标题和回复，前端已补自动刷新，最后原生团队回复跳转通过，见 team-final-real.json。
- 工作区技能来源曾在重启后退回错误目录；从已登记工作区恢复。普通 GET 不再重复扫描。
- 用户登记根或祖先目录被替换后，刷新曾继续扫描新目标；现在与已保存的真实路径比较，变化时停用来源、清除该根缓存候选并提示重新添加。默认共享目录仍支持已允许的链接。
- 项目知识和记忆设置曾有常驻协议说明，审计说明漏删；已删除并折叠高级操作。原生 Memory 常用开关、折叠区和唯一高级入口回读通过，其余页面继续按 S6 核对。
- 默认技能缺失时曾从外部同名候选补装；改为仅接受内置来源，先核对六项完整性，缺失或损坏直接报错，不自动执行外部候选。
- 新模型请求以受保护的随机私钥计算 HMAC 匹配值，数据库保持64字符不透明指纹；私钥首次写入经过 Gateway。旧未版本化收据只用于兼容核对，不重写历史。模型路由使用自身私有收据，避免通用缓存持久化授权 URL 或额外凭据摘要。
- 首次原生创建后，较旧的标题查询被新查询取代，却被当成失败；改为区分成功、被取代与失败。
  只读警告独立保存，不能覆盖未知写提示。丢响应提示曾重复下一步且含双句号，已精简为
  “没有收到创建结果。请刷新并核对原请求，暂时不要重试。”，处理行为不变。
- 本机能力安装/授权后，通用助手仍只有五个文件/命令工具；原接入还要求手动修改角色并从操控台开租约。此项是未完成的产品接线，不能仅记为外部授权受阻；按方案补新对话作用域绑定。
  现已补专用开启/查询、新快照绑定和逐次租约复核；同一个工具对象在接管期间拒绝、恢复后解析
  原会话的新租约，关闭后不能使用另一会话。两个缓存层曾让原键回放旧 active，已让本机持久收据
  单独处理新入口。503 等可能在提交后出现的错误保留原键；已知审批刷新后走正式状态查询。
- 真实浏览器首个观察进一步发现内层审批和通用409缓存冲突；观察也创建持久 Job，因此经 Session 审批。批准后核对工具收据、动作内容、授权范围、当前租约和 Policy，再批准对应的内层请求；拒绝、取消、接管和未知结果不自动重放。修复后的实际操作仍待验收。
- 第二次原生审批等待因流长时间无数据而被取消；增加12秒 SSE 注释心跳并显式清理内部生成器。第三次原生等待100秒后获批并完成观察，打开网页仍失败；保留失败并继续修复，不自动重试结果不明的动作。
- 第三次网页观察完成后，下一步批准晚于60秒观察有效期78秒；时间线与源码映射指向分发前的过期409，旧运行只保存 Phase1EError 类型，没有保留原 HTTP 错误正文。明确过期码、工具失败收尾与中文提示已修，45项相关测试通过，文案定稿后3项复验通过；60秒有效期保留，原生复验待完成。
- 应用第二轮填写在内层批准后重新生成随机密文，引发同键动作哈希冲突。单次工具上下文现复用同一随机密文，并核对原键、目标、租约/栅栏、观察、字段和内容；变化时拒绝。第三轮尚未进入填写，观察失败的具体原因码仍需修复与新任务复验。
- 应用验收 Job 成功不能证明正确窗口：首轮实际误选分享浮层。已统一观察、操作和截图的标准窗口选择，并递归读取控件标签；不增加任意正文或字段值曝光。第二轮真实观察已正确返回测试窗口、按钮和输入框；填写在内层批准后失败，继续请求的随机密文变化正在修复。首轮超时后点击取消不能算取消通过。
- 初始化曾覆盖注入的旧兼容 Provider，导致两个上下文回归失败；现在保留委托。相关 61 项回归通过。
- 旧迁移测试夹具人为删 v23 账本却留下 v24，产生缺口；改为从真实 v22 库迁移。15 项远端回归通过，未改生产迁移账本。

## 保留的失败与未覆盖项

- 文档前两轮因等待审批、超时和额度中断未产出；第三轮成功不删除失败记录。
- 两轮早期完整测试未完成；契约变动中的一轮为 563 通过、11 跳过、1 确定性失败。
  v3 为 1376 通过、12 跳过、3 失败；上述 Provider 和夹具修复后 v4 通过。
- 初次 wheel 在生成输入变化时有四处不一致，弃作候选；重建回读通过的中间 wheel 仍需随最后改动更新。
- 最后协议复查首次漏设隔离数据库，导入触发早期 v24 草稿库校验和不匹配，收集阶段失败；原库和迁移账本未改，重跑使用新库。
- 首次干净库首消息曾只有正式 API 证据；现已补独立原生录入、返回草稿与真实文件任务。
- 原用户候选的丢响应补验因用户登录而暂缓，没有注入故障；随后在独立 Verification 完成，
  不改变原窗口。未知请求跨重开保留，草稿仅在退出前保留；不将草稿跨重开持久化算作已实现。
- ChatGPT 官方错误 `3p_delegated_access_policy_denied`，提示当前组织尚未为应用启用访问。未获得 code/token，已取消本次待登录。真实模型调用、续期、撤销受阻；没有改变组织权限。Gemini 的 Operant 客户端/Cloud 项目仍缺。
  User 确认使用 Business / Enterprise / Edu。当前官方管理入口为 Admin Console → Global settings →
  External access → Operant → Scopes；Identity 与模型调用授权分别检查，需组织 Global admin。
  若应用未列出，先用 Enable apps 添加。[官方管理说明](https://help.openai.com/en/articles/12289294-managing-your-tenant-in-admin-console)。
  当前开源客户端模型调用文档列出符合条件的 Plus / Pro；组织登录成功不能证明订阅调用资格。
  [官方资格说明](https://developers.openai.com/siwc/quickstart)。
- 最新三成员读取任务虽然 Runtime completed，审查回复为 REWORK，指出未证明全部成员读取；不把运行成功当作任务通过。见 native-final-review.json。最后按规划/执行/审查职责复验，真实回复正确且 APPROVED；历史 REWORK 仍保留。
- TextEdit 的具体目标授权未取得，没有授权其访问。本轮仅使用用户明确批准的自有临时 Cocoa 应用，不含剪贴板或其他应用；真实输入、点击、接管恢复、取消与撤销已通过，临时应用已退出并保留可恢复副本。
- 条件跳过不作为能力通过。OAuth外部条件、最终安全修复选择、完整门禁与未覆盖界面补验仍保留在原范围中，整轮尚未完成。

最终候选的CLI模型发现复验被自动批准审查拒绝：当前授权未具体点名凭据接收地址axon.ystone.top。
已请求用户明确批准该目的地，尚未运行此调用，也不换入口绕过；历史真实模型结果仍按原候选保留。

交付边界仍为可审阅代码、隔离桌面候选与真实验收记录；合并、发行、安装版更新和真实用户库迁移另行授权。

### 旧连接收据的安全修复提案（待用户选择，未实施）

CodeQL #46 指向旧无版本连接请求仍计算 SHA256(secret)。这些早期收据只保存请求指纹和结果，
没有完整原始输入；仅凭当前连接配置和secret_ref无法严格区分省略默认值与显式默认值等旧请求。
因此不能既删除旧SHA算法，又凭现有记录保证所有旧键自动成功重放并拒绝不同输入。

推荐将旧无版本请求改为409 manual_reconcile_required，提供按原request_id的本机只读核对：
连接及收据一致时显示已创建，记录缺失或已删除时说明尚不能确认；不验证新提交密钥、不写凭据、
不创建第二个连接、不重新启动OAuth。新请求继续HMAC。现有连接、ModelProfile和执行快照原值保留。
公开旧路径及Schema仍兼容；早期旧键的自动成功重放行为改为人工核对，需用户接受该边界。

另一选择是先制作隔离、可回滚的旧收据迁移：只有可唯一核对的合法原始请求候选才原子CAS为HMAC。
缺凭据、已删除连接和旧OAuth状态不能猜测，仍需人工核对。只处理隔离副本；真实用户库迁移另须授权。
此提案未修改生产处理或任何数据库，方案选择不代表允许合并、发行或迁移真实用户库。
