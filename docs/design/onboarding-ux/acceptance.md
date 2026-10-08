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
| UX-08 自动技能目录 | 已实现 | 未测 | S4 默认来源、缺目录、坏项隔离及重启恢复当前工作区通过；原生显示真实候选，最新来源恢复修复待复验 |
| UX-09 标准技能格式 | 已实现 | 通过 | S4 普通 metadata、多行 description、资源、损坏 YAML、大小和解析安全边界通过 |
| UX-10 技能链接与去重 | 已实现 | 通过 | S4 登记根内顶层链接、真实路径去重、目录外拒绝与添加来源通过；资源内链接仍按安装完整性边界拒绝 |
| UX-11 内置技能就绪 | 已实现 | 通过 | S4 两个工作区六技能进入新快照；正式 gpt-6-luna documents 命令经一次审批产出 Word，正文回读通过 |
| UX-12 空会话状态 | 已实现 | 通过 | S2 未绑定时不查询技能命令；原生新对话显示输入入口，没有技能读取错误 |
| UX-13 本机能力使用流程 | 部分 | 失败 | 原生安装、启用和目标绑定成功，真实模型首次观察因内层审批未接通而中断；已补 Session 审批桥和原请求键，定向测试通过，修复后真实浏览器操作、接管/取消待复验；TextEdit 授权待取得 |
| UX-14 唯一设置与导航 | 已实现 | 未测 | S6 四入口、六分类与内容分离已核对；最新旧路由及全部管理入口待完成回读 |
| UX-15 共用选择组件 | 已实现 | 通过 | S6 原生深浅主题、1280/700/390 宽度；模型/项目/模板选择的搜索、方向键、Enter、Escape、焦点和弹层位置通过 |
| UX-16 基础团队模板 | 已实现 | 通过 | S5 正式三成员完成；旧详情和回复缓存问题已修。最新原生点击回复自动刷新到16对话，显示审查确认的正确标记和 APPROVED；原生恢复/取消按钮均禁用，运行图默认折叠 |
| UX-17 精简文案 | 部分 | 未测 | 审计说明已删，知识高级操作折叠；普通错误改为中文，详情收起。原生 Memory 页现仅展示常用开关，迁移和配置折叠并跳转唯一高级页；标题误报和重复恢复提示已修，全部日常页面仍待回读 |
| UX-18 演示开关归位 | 已实现 | 通过 | S6 默认实时连接，原生日常侧栏没有演示开关；开发设置保留演示入口 |

## 场景与环境

- S1：干净库启动、连接、默认工作区与角色、首条消息及重启。
- S2：创建/初始化幂等、自动名称、改名、切换及断线/未知结果。
- S3：ChatGPT/Gemini 登录、取消、目录、流式工具调用、续期与撤销。
- S4：技能目录、YAML、链接、重名、坏文件、来源变化、安装权限及真实产物。
- S5：原生本机能力启用、目标选择、实际操作、接管/取消；基础团队任务。
- S6：导航、旧链接、选择器、键盘、主题、宽窄屏和文案。

正式模型先经 CLI 发现，使用精确 `gpt-6-luna` 和正式 ModelProfile/Session/Graph 入口。
测试数据、凭据、Core 和桌面候选均隔离；SQLite v24 只用于测试库，未改真实用户库。
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
| S6 | 原生模型表单、六设置、五角色、深浅主题和三种宽度；Select 弹层在窗口内、键盘选择与关闭后焦点恢复 | 原生界面回读；GUI 选择器及路由测试 |
| S6 | 原生记忆设置默认只显示全局/项目开关；展开高级区后可进入唯一高级设置。项目内容页不再展示常驻审计说明 | native-verify-ui-copy.json |
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
原用户候选 Core 18768 未重启。一次 GUI 构建错误写入3018的静态输出，随后按848ecf3源码恢复，60项文件与恢复构建一致；没有刷新原窗口、改变数据库或复制凭据。见 original-gui-output-restored.json。后续 Verification 构建使用独立输出目录；独立代理18779只用于故障验收。

## 检查记录

检查输出保存在 `.operant/onboarding-ux/checks/`。范围和版本变化分别记录，不相加为整轮结论。

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
- 技能边界 **19项通过**，覆盖旧目录别名/符号链接收据读回、禁止重复登记/扫描、真实目标变化、未知结果及批准前不读取目录；见 skill-boundary-tests.log、skill-verify-audit.json。
- 本机应用扫描负例覆盖顶层/Contents/Info.plist 链接、超限文件和重复应用；模型私有状态只允许不透明签名/加密引用。

## 已发现的问题与处理

- OAuth 轮询曾丢登录链接；改为仅驻内存、取消/过期清除、响应 no-store，关闭含回调 code/state 的原始访问日志。
- 原生链接曾没有打开外部登录页；受限 Tauri 登录命令已在最新候选成功打开 OpenAI 官方 Operant 授权页。用户已批准本次授权，官方组织策略拒绝，未获得 code/token；本机取消入口通过，原 API 连接和对话保留。见 chatgpt-real-auth.json。
- 团队详情曾进入旧 Workflow 页并报 task not found；修为正式 Graph 深链，原生补验通过。最新回复入口虽然 URL 正确，但旧线程投影导致误报找不到对话；手动刷新后看到正确中文标题和回复，前端已补自动刷新，最后原生团队回复跳转通过，见 team-final-real.json。
- 工作区技能来源曾在重启后退回错误目录；从已登记工作区恢复。普通 GET 不再重复扫描。
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
- TextEdit 的具体目标授权待用户答复；没有点击安装授权，没有执行本机操作。
- 12 项条件跳过不作为能力通过。整轮、OAuth 和本机操作不能标记完成。

交付边界仍为可审阅代码、隔离桌面候选与真实验收记录；合并、发行、安装版更新和真实用户库迁移另行授权。
