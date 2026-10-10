# 开箱即用与界面整理验收

> 2026-10-10；记录身份：Codex 主线程；适用对象：本轮实现与验收 Agent。
> 分支 `codex/onboarding-ux`；基线 `939e3cc`；完整范围见[批准方案](plan.md)。实施中，整轮未完成。

当前 S3：Gemini 真实账号授权和原生模型发现已通过，发现 21 个模型并选择 `gemini-2.5-flash-lite`。
首条正式任务以 `ProviderError/network_error` 失败，没有模型增量、完成事件或工具调用；旧记录
不足以确定是超时、代理、连接还是解析错误。推断诊断补修和后续真实调用分别验收，不将登录
成功算作模型可用。`047f0e1` 的双 Python 完整门禁与 GUI 已通过；之后的增量另行记录。
ChatGPT 的 Mountain 组织策略尚无变更，不重复登录。CodeQL #45 当前 PR 实例仍开放。
用户已明确本轮只用免费额度，不充值、启用付费结算或自动充值；调用前核对项目的免费层状态。

| 需求 | 实现状态 | 验收状态 | 场景与结果 |
| --- | --- | --- | --- |
| UX-01 会话标题 | 已实现 | 通过 | S2 自动标题、手动优先、CAS、旧历史、数据库重开；原生标题、侧栏及改名后重开通过。子对话及分页外标题由正式查询测试覆盖 |
| UX-02 默认角色 | 已实现 | 通过 | S1 原生干净库连接后初始化五个角色，通用助手直接接收任务；已有配置保留由回归测试覆盖 |
| UX-03 统一新建对话 | 已实现 | 通过 | 两个入口共用初始化命令；S2 原生双击只新增一个 Thread/Session，没有新增 Agent 或发送草稿 |
| UX-04 自动初始化运行 | 已实现 | 通过 | S1 原生自动准备运行并完成文件任务；S2 丢创建响应后保留原请求，退出重开再只读核对，只新增一个 Thread/Session，没有自动运行或发送草稿 |
| UX-05 简化模型连接 | 已实现 | 通过 | S1 用户在独立原生窗口保存密钥，发现并选择模型后返回原草稿，直接发送任务；无需角色、系统提示词、环境变量名或绑定运行 |
| UX-06 高级参数与默认值 | 已实现 | 通过 | S1/S6 温度不发送、未知上下文不指定；原生表单默认收起高级参数，服务商切换通过 |
| UX-07 ChatGPT/Gemini OAuth | 部分 | 受阻 | ChatGPT 个人套餐权限不可用，Mountain 被组织策略拒绝；不重复登录。Gemini 已通过真实授权、原生发现 21 个模型和选择模型，首条任务失败且没有输出或工具调用。流式回复、工具调用、真实续期和撤销仍未通过；安全与诊断回归不能替代真实验收。历史失败保留在 S3 |
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
| S6 | 最新文件页已打开welcome.txt后，对无运行任务的隔离Core做40秒受控断线并自动恢复。连接失败时选择和刷新禁用，正文预览卸载；恢复只重载目录，没有恢复旧正文缓存，再点击文件正确读取。无模型调用、数据库迁移或凭据读取/复制，原用户Core未动 | native-project-file-disconnect-recovery.json、project-file-disconnect-recovery-process.json |
| 构建 | 冻结 wheel 的 278 项源/SDK/协议/技能文件逐项一致，隔离安装后导入路径与健康接口正确 | wheel-frozen-readback.json、frozen-runtime-import.json、frozen-runtime-health.json |

中间构建证据对应契约 `234d2316…`。最新 Core wheel 已重建，278 项逐文件一致；
摘要 `839d53cfd8ca190d36226176d31500fa2ec42d78812bc1324d1554fba60d8e4c`，见 wheel-final-readback.json。
隔离安装后的实际导入、健康与新协议通过，见 final-runtime-import.json、final-runtime-health.json；
原生二进制已加入受限登录命令，前端最终补验仍在完成。提交 `848ecf3` 和对应运行候选的
`onboarding.v1` 有 21 个操作，摘要
`aac48a78b39f04a1a3d84741bb18a226c558f0026c06332ca03c572188f50ecf`。
前一候选新增对话专用控制开启与原请求查询，共 23 个操作，摘要
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
- 增量冻结检查：格式593文件、ruff、mypy204源文件通过；协议与首次配置6项通过，公开Schema和旧迁移摘要不变。GUI最终**191项**、类型、独立构建及包体预算通过，日志退出码与输入/输出摘要见gui-route-file-final-report.json。完整门禁已在冻结6271186的双版本CI通过；CodeQL仍有两项high，未算通过。
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
- 冻结 `6271186`：CI37781913471全部作业通过，Python3.10/3.12各**1496通过、16跳过、1警告**，TUI各**34项**；GUI**193项**、类型和构建通过，格式593、ruff、mypy204与锁检查通过。CodeQL37781908519的分析作业成功，汇总仍有旧凭据摘要和技能目录路径两项high；测试夹具告警在当前PR实例为fixed，未抑制告警，安全扫描仍未通过。所有作业已收齐后再处理修复。见ci-6271186-final-snapshot.json、ci-6271186-final-summary.json、ci-6271186-all.log及codeql-6271186-summary.json。
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
- 新模型请求以受保护的随机私钥计算 HMAC 匹配值，数据库保持64字符不透明指纹；私钥首次写入经过 Gateway。旧未版本化收据现转为只读人工核对，删除了此前的旧摘要匹配，不重写历史。模型路由使用自身私有收据，避免通用缓存持久化授权 URL 或额外凭据摘要。
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
- 条件跳过不作为能力通过。OAuth外部条件及最终安全扫描仍保留在原范围中，整轮尚未完成。

最新隔离环境复核Gemini应用客户端ID/secret均未配置，未打印其值或调用Google。
自有项目启用API、授权页面/测试用户及Desktop app客户端的准备流程已写入桌面说明，
不自动发布Google应用或接受账号条款。见gemini-application-configuration-current.json。

最终候选的CLI模型发现曾因未明确点名凭据接收地址被自动批准审查拒绝；用户随后明确同意
`https://axon.ystone.top` 的隔离发现与合成消息。新候选正式CLI返回39模型，包含精确
`gpt-6-luna`；原生新对话返回`OPERANT_MODEL_RECONCILE_OK`，正式历史回读为completed、无工具调用。
凭据只进入目标进程，不输出值，未改原用户环境。见model-request-candidate-discovery.json与
native-model-request-candidate-reply.json。一次清理审查误认18778为原用户端口；用ps/lsof确认
隔离PID28175监听18778、原用户PID22137监听18768后批准继续，没有换入口绕过。

交付边界仍为可审阅代码、隔离桌面候选与真实验收记录；合并、发行、安装版更新和真实用户库迁移另行授权。

### 旧连接收据的安全修复（用户已同意，已实现）

CodeQL #46 指向旧无版本连接请求仍计算 SHA256(secret)。这些早期收据只保存请求指纹和结果，
没有完整原始输入；仅凭当前连接配置和secret_ref无法严格区分省略默认值与显式默认值等旧请求。
因此不能既删除旧SHA算法，又凭现有记录保证所有旧键自动成功重放并拒绝不同输入。

用户选择将旧无版本敏感请求改为409 manual_reconcile_required，提供按原request_id的本机只读核对：
连接及收据一致时显示已创建，记录缺失或已删除时说明尚不能确认；不验证新提交密钥、不写凭据、
不创建第二个连接、不重新启动OAuth。新请求继续HMAC。现有连接、ModelProfile和执行快照原值保留。
公开旧路径保持兼容，onboarding.v1新增只读查询与DTO，24操作摘要为
`4f1a5ab639869b762c5641cf0739b529dc1d4effe485a3acf594e6a3469c0d32`，由同一Schema生成
TS/Python Client，旧协议摘要未变。早期旧键自动成功重放改为人工核对，已获用户接受。
完整Core错误Envelope保留原请求编号，返回manual_reconcile_required及manual_reconcile恢复指引。

新API创建使用请求锁和受保护凭据文件锁；凭据已写入但DB保存失败时保留孤儿凭据，原键重试
不覆盖它。查询用同一SQLite快照校验DTO、确定性连接ID、原创建指纹与版本；仅证明原创建记录，
不验证新输入或凭据。OAuth查询unavailable不能证明授权未发生，客户端保持未知，不自动重新登录。

定向模型连接25项与协议3项通过，GUI196项、类型、独立构建与包体预算通过。首次协议测试误用了
历史临时导入库，触发v24账本校验失败；改用新的隔离导入库后通过，未改账本或正式迁移。
日志model-request-integrated-pytest.log保留失败，最终结果见model-request-integrated-final-pytest.log。
格式、ruff、mypy204、离线锁及diff通过。冻结`c8c30c5`的完整CI37801782989已全部通过：
Python3.10/3.12各**1499通过、16跳过、1警告**，TUI各**34项**，GUI**196项**、类型/构建/包体通过，
格式593、ruff、mypy204及离线锁通过。4个作业的完整日志与摘要见ci-c8c30c5-final-summary.json、
ci-c8c30c5-final-status.json。父线程watch曾因GitHub API unexpected EOF退出1，之后通过官方作业
终态与完整日志核对success，没有把watch错误当作测试失败，也未重新运行该冻结提交的完整门禁。
CodeQL37801777170分析成功，汇总仍为失败：当前仅#45目录路径high开放，#46旧凭据摘要与#47夹具
实例均fixed，没有抑制或dismiss；见codeql-c8c30c5-summary.json。CI通过不等于安全扫描通过。

原生丢响应场景：保存成功后代理丢弃返回；列表刷新及重开App仍阻断新提交，表单不跨重开保存。
原请求GET确认后恢复，实际创建POST仅1次、连接仅1条。窄屏详情换行已修，1280/700/390及深浅
主题、长编号换行与键盘焦点通过；最终原生核对确认后密钥和名称清空，再次保存只出现填写提示，
没有第二次POST。见native-model-request-recovery.json与native-model-request-final.json。当前Core候选wheel
`d8b26ea9d53ff878ebaf25934fd6164a15211d0feffb5e047df5c10f5165fa81`，283项安装文件与源码一致，
runtime-model-request；GUI为gui-model-request-form-dist，278项输入和61项输出冻结、原生挂载通过，
见gui-model-request-form-report.json。临时库v25沿用，未复制凭据或迁移真实用户库；合成连接已清理，
保留原隔离模型配置。新候选的正式模型发现及原生合成消息通过，不把API通过当作OAuth通过。
最新隔离环境再次只核对应用配置是否存在：Gemini客户端ID/secret仍均缺失，未打印配置值、未调用
Google；见gemini-application-configuration-c8c30c5.json。ChatGPT组织策略拒绝仍未得到条件变更确认。
用户准备状态待答；两家登录、模型发现、流式工具调用、续期与撤销的完整真实验收继续保留为受阻。

缺配置的错误引导补修：已明确失败的模型操作改为显示具体原因与下一步，未知写入仍按原请求
核对。Gemini缺桌面客户端/项目或客户端密钥时，提示高级填写入口；项目或客户端与旧连接不匹配
时提示使用原配置。GUI198项、类型/构建/预算通过；原生用合成项目ID得到具体中文原因、可打开
高级字段，既有API连接及配置保留，没有Google登录/调用或凭据输入，见native-model-setup-guidance.json、
gui-model-setup-guidance-report.json。只有GUI及展示测试改变，Core/SDK/Schema/依赖与已通过c8提交
输入一致，后端完整门禁及真实模型证据可复用；当前前端为gui-model-setup-guidance-dist。

未采用旧收据迁移方案。用户本次选择不代表允许合并、发行或迁移真实用户库。

### 技能目录路径告警复核

CodeQL #45 的路径数据流真实存在：自选绝对目录须在本机请求与Gateway授权后解析，不能称
“已经净化”。只读复核未找到逃出已批准真实根的具体漏洞；根或祖先链接漂移会停用来源，
扫描另经读权限授权、真实路径身份核对和根内链接限制。保留任意用户目录，不加入伪净化或抑制。
loopback、Host、Origin校验不认证同机进程身份；若不可信本机进程或其他系统用户在威胁范围内，
仍需调用方认证。该信任前提尚未完成正式安全处理，告警保持开放，不能算CodeQL通过。

2026-10-09 再次回读：#45 在 `adac563` 的 PR 实例仍为 open/high，未修复或关闭。
独立源码复核确认，现有 Operant 登录依赖预配置 HTTPS OIDC 和 Secure Cookie，直接打开该
中间件会挡住 HTTP 回环地址上的模型 OAuth 回调。桌面尚无原生私有请求通道，独立 PWA/TUI
也没有首次配对流程；给来源路由增加一个 Header 并不能独自解决首次连接。
后续认证须同时覆盖原生启动和其他客户端配对，并保留由 state/PKCE 校验的模型回调。
这些改动尚未实现或验收；目录来源的功能验收不代表调用方身份认证通过。
认证也不能作为任意目录路径的净化证明，告警须经实际边界测试与安全审阅处理。
见 skill-source-caller-auth-review.json、codeql-alert45-adac563-current.json。

### Google 测试应用最终提交准备（2026-10-09）

浏览器连接恢复后，旧标签已关闭。打开已获许可的同一项目，关闭免费试用提示，进入正常
Google Auth Platform 向导；本次没有要求填写付款信息。应用名称为 Operant，支持和联系
邮箱均采用已获许可的默认账号，受众为 External/Testing，仅允许明确添加的测试用户。
前三步已完成，最后一步的用户数据政策复选框未勾选，“创建”未点击，桌面客户端未创建，
没有取得凭据或调用 Gemini。页面已保留，并请求当次政策接受及创建测试应用的确认。
见 gemini-branding-final-prepared.json；先前付款页和浏览器工具受阻记录不删除。

### Gemini 输出隐私与续期权限补修（2026-10-10）

复核发现原解析会把 `thought:true` 的文本当作普通回答发布，且流末尾空文本签名被跳过。
现严格校验思维标记，过滤思维正文；普通空文本签名按零长度段恢复。无法安全原样恢复的
带签名思维 Part 和混入工具/代码的思维 Part 明确失败，不存正文或伪造上下文。
不完整流、取消、单项及聚合超限均在保存签名前阻断。
依据：[Part 定义](https://ai.google.dev/api/generate-content#Part)、
[签名与空文本规则](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)。

Gemini 续期收到显式缩权时，原实现仍使用新凭据。现与初次授权共用必要 scope 校验，保留旧
凭据和账号绑定，标记 `needs_auth/permission_denied`；未提供 scope 的合法续期保持兼容。
独立 Review 另复现思维工具 Part 被静默丢弃、needs_auth 被未过期旧 Token 清回 connected；
两项已修，直接调用和路由均保留重新授权/撤销未确认状态，正常续期仍可用。

第一次补修 64 项通过的记录保留；Review 修复后模型连接 **80 项通过**，格式/ruff、两源码
mypy 及 diff 检查通过。独立原反例复跑已拒绝，并补验 **11 项通过**（与 80 项有重叠，不累加）。
见 gemini-thought-refresh-status.json、gemini-thought-refresh-review-status.json、对应日志与
gemini-thought-refresh-independent-review.json。完整门禁和新原生候选尚待完成，不复用旧 Core
门禁来证明这次后端改动，也不将模拟结果算真实 Gemini 通过。公开 Schema、SDK 和迁移未改。

用户随后当次允许接受政策并创建测试应用，Google 页面显示配置及桌面客户端创建成功。
下载事件观察超时后，本机核对确认只下载了一个新配置，没有再次点击下载。配置按原子写入
隔离 0600 `.env`，原 JSON 同存该文件作为可恢复归档，核对后移除下载目录明文副本。
既有模型凭据保持；没有付款操作、公开发布、用户 Token 或模型调用。
见 gemini-desktop-configuration-stored.json、gemini-test-application-created.json。

### 免费验收与推断诊断续行（2026-10-10）

`047f0e1` 的 CI 38017158163 四作业已成功：Python 3.10/3.12 各 **1554通过、16跳过、1警告**，
TUI 各 **34项**；GUI **201项**、类型、构建通过，格式593、ruff、mypy204和离线锁通过。
原始日志与终态见 ci-047f0e1-final-summary.json、ci-047f0e1-final-status.json；旧 in_progress
快照保留。CodeQL #45 的该提交 PR 实例仍 open/high，不能把 CI 成功算作安全扫描通过。

随后真实 Gemini 授权成功，原生发现21个模型并选择 `gemini-2.5-flash-lite`，默认角色、
温度和上下文均沿用模型配置。首条正式任务只有 agent.started/agent.failed，没有模型增量、
完成事件或工具调用；连接记录为 network_error。旧日志没有异常类别，具体原因仍未知，
不能归因于结算、额度、授权或代理。见 gemini-native-session-snapshot.json、
gemini-failure-audit-current.json；旧失败没有重放。

推断诊断补修区分四种超时、代理、连接、协议、网络中断和无效响应，持久事件只增加固定
白名单分类与中文提示，不保存异常原文或请求/响应信息。旧异常和未知分类保持兼容。
GUI 错误栏和时间线共用诊断提示，取消、超时、预算等普通摘要不展示内部原因。
后端两个相关测试文件 **102项**、公开协议/SDK **20项**、GUI **205项**及类型/隔离构建通过；
完整格式、ruff、mypy和离线锁通过。83个协议/生成文件不变，GUI冻结278项输入、61项输出。
这次增量的完整新 CI 与原生接线仍待验收；不能用旧门禁或模拟失败算真实 Gemini 调用通过。
见 gemini-inference-diagnostics-report.json、inference-protocol-sdk-report.json、
inference-integration-gates.json、gui-inference-diagnostics-report.json 的 review 记录。

用户限定只用免费额度，并选择另建不绑定结算的验收项目，保留原项目。官方 AI Studio 已创建
Operant Free Verification，显示“免费层级”，未创建 API 密钥或启用结算。原项目的停用确认
已取消，没有充值、自动充值或付费切换。见 gemini-free-project-created.json；新项目的
OAuth 连接与真实流式工具调用仍待完成。

### 授权码交换失败的诊断补修

用户提供的新截图显示 ChatGPT 已返回授权码，但 Operant 显示 `OAuth code exchange failed`。
旧实现将所有非200响应归为同一文本，当前服务日志也未保存状态，无法追溯此次具体原因；
不能断言仍是早期组织策略拒绝，也不能把“返回授权码”算作登录凭据或模型调用成功。
未保存或重放用户截图中的回调地址与授权码。

现保留实际 HTTP 状态及白名单内的错误码，忽略原始正文、错误描述和未知代码；超限、非JSON
或其他格式只保留状态。GUI按已知错误给出下一步，未知403仅列权限、配置与网络的核对入口，
不推断具体原因。失败回调提示回到应用查看原因，不刷新旧地址。8项新增负例覆盖错误格式、
敏感内容过滤、清理临时状态与禁止重复换取；模型连接**33项**、GUI**199项**、类型/构建/包体、
格式/ruff/mypy/离线锁/diff通过。已有完整c8门禁不证明此次新增后端改动通过，仍需新完整门禁。

新Core wheel `14643d4df53797c66cee564718d0e193726200bb222df015b5e17ed5b2ff3990` 的283项源码/资源与
安装文件一致，依赖版本未变。隔离Core18778沿用v25测试库及原凭据文件，没有新增迁移或复制凭据。
首次依赖比对误跟随解释器链接读到系统环境，使用虚拟环境原路径修正；首次健康回读用了错误的
`/health`，改查`/healthz`，未因此重启新Core。前端静态服务器缺API代理的初次挂载失败保留，
恢复Vite代理后，原生API连接、个人工作区与已连接状态回读通过。
已通过原生入口发起新的ChatGPT授权并打开官方登录页，待用户完成；尚未记录此次换取结果。
证据见oauth-exchange-diagnostics-wheel-readback.json、verify-oauth-exchange-diagnostics-processes.json、
oauth-exchange-diagnostics-pytest.log及gui-oauth-exchange-diagnostics-checks.json。

用户随后提供的截图仍显示失败，详情为`OAuth request failed`。4ad版只补非200响应，网络/代理异常、
成功HTTP下的无效JSON以及本机凭据异常仍被通用catch抹掉；因此前次诊断补修不完整，不能称登录
问题已修复。新截图对应的Core日志为空，无法恢复此次具体异常。用户明确要求停止反复登录，
后续不再创建授权请求、打开新登录页或要求用户重复操作，除非用户重新明确授权该动作。

现在补充固定失败阶段和安全异常分类，覆盖凭据交换及身份验证中的超时、代理、连接、协议和
网络错误，成功HTTP的无效/超限正文，以及凭据保存失败。15个网络/格式负例与1个凭据保存负例
验证不泄露异常原文、code/token、state或URL，不自动重放，原有API连接保留；这只证明诊断与
失败边界，不能证明真实登录已恢复。最新定向测试和后续完整门禁仍按新提交记录，旧结果保留。


### 默认账号与个人工作区授权（2026-10-09）

用户明确授权后续必要登录可由Agent操作，使用默认账号和个人工作区；此前停止重复登录的要求
保留为历史，当前按新授权继续。20秒候选的正式个人授权返回
`OAuth request failed (token_exchange; timeout)`，同一代理下无凭据的公开GET和空表单POST
均有响应；这不能解释含授权码请求超时的具体原因。没有重发该授权码。
60秒单次读取候选的原生状态随后为`HTTP 400; invalid_grant`，未取得Token或模型调用权限。
原生App和Core分类日志支持此结果；当前选中的Chrome窗口为用户其他页面，未操作该窗口，
不把它作为回调归属证据。见oauth-personal-exchange-timeout-routing.json、
oauth-empty-form-transport-probe.json及oauth-personal-invalid-grant.json。

官方流程要求invalid_grant后保留已签发的client ID，以新授权码继续。原实现仅在成功换取后
保存client ID，失败后每次重新登记，缺少这条恢复路径。现补显式“继续连接”：只复用该次登记
与连接编号，新建state/nonce/PKCE，不重放旧code，不提前保存账号或凭据。原失败状态保留，
重复请求幂等；超时、未知结果、其他失败及已消费登记不能创建重复续行。已验证正式回调/状态/
启动接口和真实签名验证的确定性恢复场景，不能算真实OAuth通过。

f723完整CI37894614058已收齐：Python3.10/3.12各**1523通过、16跳过、1警告**，TUI各**34项**，
GUI**200项**、类型/构建，以及格式593、ruff、mypy204、锁均通过。此结果只覆盖f723。
本次登记续行和等待预算改动的定向、GUI和新完整门禁另记，不能复用旧CI证明新后端通过。
CodeQL #45仍开放；独立源码复核未发现越出已登记真实根的具体扫描漏洞，但loopback/Host/Origin
不认证同机进程身份。不得用路径白名单删减任意目录需求或用伪净化、抑制告警宣称通过。

登记续行候选：模型连接**53项**、GUI**201项**、类型/构建/包体、格式/ruff/mypy/离线锁通过。
独立Review发现并修复两项边界：已验证连接断开后不能借旧失败Attempt重新建立该编号；
不同请求编号的并发继续只能消费一次登记标识。线程并发、正式回调/启动幂等、客户端标识不符、
多次显式续行和真实签名校验均有定向覆盖。Schema/digest未变，不新增字段或迁移。
新wheel的283项源码/资源与安装文件一致，摘要
`f670488a791c56737f6a209f9e33536fe68bb0f3c2102b024683dde757333488`；前端278项输入冻结，
原生恢复仍待验证。见oauth-registration-continuation-checks.json、
oauth-registration-continuation-wheel-readback.json和gui-oauth-registration-continuation-report.json。


### 原生登记恢复与 Mountain 工作区（2026-10-09）

冻结c3c4a88已推送到Draft PR45，隔离Core18778/PID81494使用
runtime-oauth-registration-continuation，前端3028/PID81495使用独立冻结构建。原用户Core18768/
PID22137保持原样；仅备份并沿用v25验收库，不复制凭据或新增迁移。

个人工作区首次登记仍返回400/invalid_grant。原生点击新增“继续连接”后，授权URL复用该次
已签发的client ID并生成新的state；选择默认账号后进入官方权限页，页面提示
“所需权限不可用。你无法继续使用此工作空间和套餐。”且继续按钮禁用。已从Operant取消待处理
登录，原API连接保留；未取得Token，不算登录、发现或调用通过。
见oauth-personal-registration-continuation-native.json。

用户随后明确改选Mountain。账号选择曾显示Operation timed out，原生Chrome窗口读取仍保留
旧页面，未用它推断工作区权限。通过CUA正式Chrome Tab的DOM读取核对超时页，点击该页重试
恢复当前未完成请求，选择默认账号、Mountain及名称Operant。官方权限页的继续按钮可用，
仅请求基本资料和ChatGPT套餐调用。最终提交被自动审批拒绝：Mountain为新的工作区访问，
须当次确认；已请求User确认，尚未提交或取得Token。没有回放旧callback/code或更改工作区
管理员权限。见oauth-Mountain-consent-prepared.json。

完整CI37906688180对应c3c4a88仍在运行，GUI已成功，双Python作业未结束；不能算完整通过。
此前f723的完整通过保留，当前未覆盖项仍为两家真实OAuth调用/续期/撤销及安全警报#45。

用户当次确认Mountain授权后，原请求已超过10分钟（到期09:04:34Z），未换取凭据；新请求
在同一冻结候选建立，有效至09:15:55Z，重新选择默认账号、Mountain和Operant名称，权限
仍为基本资料与套餐调用。官方页面随后明确返回
`3p_delegated_access_policy_denied`及“你的组织尚未为此应用启用访问权限”。
因此当前Mountain阻碍确为组织策略，不能混同个人工作区的套餐不可授权或先前交换超时。
已在Operant取消新待处理请求，原API连接仍可用，未取得Token、未改管理员权限。
管理员开关的具体路径没有取得可核实的官方或实际管理页证据，不编造设置位置。
见oauth-Mountain-organization-denied.json；服务商请求ID为068df745-44d8-4e22-b52f-244a53a62e6e，
证据不保存回调URL、code、state或Token。原生Chrome窗口的账号页读取曾滞后；正式Chrome Tab
的DOM读出Operation timed out后，恢复的是同一未完成账号选择，没有重放授权码。


### 候选构建与 Gemini 配置准备（2026-10-09）

发现同一外置构建目录多次构建后残留77项旧输出；原页面引用的产品输入仍是c3，但不把多余
旧文件当作本次构建产物。以同一278项输入在全新目录重新构建，得到61项当前输出，构建及
包体检查通过。仅将隔离前端3028切至gui-oauth-registration-clean-dist/PID36195；
Core18778/PID81494及原用户Core18768/PID22137保持不变。正式HTTP入口返回的HTML摘要
与新构建一致，两个Core健康ok；未迁移数据库或复制凭据。
见gui-oauth-registration-clean-report.json、verify-oauth-registration-clean-processes.json及
oauth-registration-clean-preview-readback.json。GUI源码未变，201项/类型证据按输入摘要复用。

新CI37906688180的已完成GUI作业原始日志通过正式GitHub Job日志接口取得，确认**201项通过**。
双Python检查仍在运行；早前gh run view --log在整次运行未结束时取不到日志，不据此重启CI，
也不将日志暂不可用当作作业失败。

默认Google账号的现有项目已启用Gemini API，
但Google Auth Platform尚未配置。打开官方向导并准备应用名称Operant；选择当前账号支持邮箱
及进入下一步被自动审批拒绝，原因是向Google Cloud提交应用资料需要明确当次许可。已请求
用户许可并保留页面，尚未提交配置、创建客户端、接受用户数据条款或开始免费试用/结算。
没有读取或复制其他API密钥。见gemini-existing-project-prepared.json。
该步骤仍属原OAuth范围，未删除；登录、模型、续期及撤销未验收。


### 冻结产品完整门禁与配置交接（2026-10-09）

完整CI37906688180已结束且四作业均success，精确产品提交为
`c3c4a88085c0d6505842b61f32561212f68496f9`。Python3.10/3.12各**1527通过、16条件跳过、1警告**，
TUI各**34项**；GUI**201项**、类型/构建通过，格式593、ruff、mypy204及锁检查通过。
全部作业原始日志已收齐，见ci-c3c4a88-final-status.json、ci-c3c4a88-final-summary.json及
ci-c3c4a88-job-{changes,gui,py310,py312}.log。跳过仍不作为真实系统/OAuth验收。

User随后明确允许在当前Google项目以Operant名称和默认账号支持邮箱准备测试配置。原浏览器
标签已不在可操作列表中，恢复同一现有项目的官方入口后，重填应用名称、选择支持邮箱并进入
受众步骤；内部选项禁用，外部选项说明仅供添加的测试用户使用。执行外部选项前，实际页面已
变为免费试用/付款信息验证，匹配的OAuth控件已不存在；立即停止，没有填写或提交付款资料。
付款操作须User本人完成或退出，再恢复OAuth配置。未由Agent点击最终创建、接受条款或开始
免费试用/结算，也未取得客户端凭据。此记录不推断User是否另有手动操作。
之后CUA Chrome控制提示需更新ChatGPT浏览器扩展，保留已观察结果，不通过其他入口继续
付款步骤。User要求先继续其余工作，登录许可仍保留，整体OAuth验收未完成。


Gemini表单原标签“项目编号”与实际请求的project_id不一致，容易让用户填数字Project number。
现统一为“Google Cloud 项目 ID”，不改变字段值、请求或权限。GUI**201项**、类型/构建/包体通过；
仅一项GUI源输入变化，独立新构建61输出。原生模型设置页核对新标签、字段焦点、高级折叠及
原API连接可见，未输入凭据、发起授权或调用模型。Core283项输入与c3完整门禁一致，不重复跑
未受影响的后端场景。见gui-gemini-project-label-report.json、verify-gemini-project-label-processes.json
及native-gemini-project-id-label.json。

当前PR的CodeQL #45实例在精确c3提交仍open（全局state字段未返回值，以实例state为准）；
未关闭、抑制或dismiss，不能把分析作业成功当作无安全问题。
