# 第四部分：扩展能力与运行治理验收记录

> 2026-09-28；记录身份：Codex；适用对象：本实施分支及后续集成者。
> 基线：`d13c6b1d9e962b68cb6f752125268558bae4d0e5`；实施分支：`codex/extension-runtime-governance`。草稿 [PR #32](https://github.com/PinelliaChill/Operant/pull/32) 已创建；最终 CI 和合并状态以 PR 回读为准。

## 已实现的可验证范围

- H-15：`CapabilityPluginRegistry` 对随 Core 发布的 Browser/Computer 适配器提供显式安装、精确目标白名单、源码摘要绑定、启停、卸载和进程间文件锁。版本化 `ToolExtension` 把受信能力工具接入 Role 的工具白名单、Agent/Graph 工具定义和 Action Gateway；`role add/update` CLI 能显式授予或撤销扩展工具并保留历史 Role 版本。Agent 审计及持久 `model.completed` 事件只保存扩展参数摘要，具体动作仍由 Core 的正式 Capability Gateway 再裁决。第三方代码不能进入内置能力驱动进程；记忆 `PluginHost` 仍独立。
- 第三方 Tool：`ExternalToolRegistry` 显式复制两文件本地包、绑定摘要/版本/命名空间，默认禁用；启用和每次调用都做真实沙箱探测。无隔离器的平台拒绝启用；插件只经有限 JSON/stdio 协议执行，不继承 Core 环境，限制参数、输出、CPU、内存及时间。托管目录拒绝未绑定文件，读取文件时拒绝符号链接。Role 白名单与 Host 注册同时满足才向 Agent 暴露，结果标为不可信并脱敏。每次安装生成新的授权名和安装 ID；重装后旧 Role 与旧会话缓存均不能调用新代码，也不能访问旧安装的数据目录。包格式、管理命令及当前类别边界见[第三方 Tool 包说明](external-tool-package.md)。
- 默认能力包沿用第三部分的 `/v1/skills/default-pack/install`：只从配置的可信 Skill 根发现并安装 `grill-me`、文档、演示文稿、PDF、创建/查找 Skill，不联网下载或额外授予工具权限；本部分复用了该入口，没有重新实现一套包管理器。
- H-16：`LocalCapabilityWorker` 复用现有 Remote Target 注册、身份心跳、短期 Lease/fencing、Action Gateway、观察哈希、Job 和持久 Receipt。Worker 不直读 Core 数据库，租约 Token 只从环境变量进入 Worker，不进入 Chrome/osascript 子进程环境。禁用插件后拒绝新任务，未知非幂等动作转人工核对。
- 浏览器和电脑动作回执明确保存动作前、后的观察哈希；浏览器点击会在有限的一秒窗口内观察页面变化，避免 CDP 刚确认输入就把旧 DOM 当成动作后状态。窗口内没有可见变化时仍保留实际观察，不把相同哈希解释为任务目标已完成。
- Browser：独立 Chrome Profile、精确来源白名单（协议/主机/端口）、DNS 地址检查与固定解析、CDP 请求拦截及本机过滤代理；代理没有直连回退，逐次验证请求/CONNECT 目标，未授权 HTTP 写请求拒绝。公网域名只能解析到公网地址，本机名只能解析到回环地址；Worker 强制排除 Core 来源和本机 Core 端口。类型化观察、导航、非密码文本输入和点击；观察列出可点击元素/字段并给表单状态哈希；动作前复核观察，用户接管修改表单会让旧动作失败。`fill` 值以短期 Lease Token 派生的 AES-GCM 密钥封装，并绑定目标、租约、观察哈希、字段与幂等键；Core 拒绝旧明文字段，持久 Job 只保存密文，Worker 执行前解封。Core 也拒绝带查询参数的导航 URL。可选 `--visible` 展示专用窗口。正文限长并脱敏，已输入值和页面回显同值文本不进入观察回执，页面自身请求的 URL 查询参数只留摘要。新 Profile 的所有权标记置于 Chrome 目录外，启动前写入；正常停止专用进程组并确认组消失后才删除。异常退出后一小时，只回收本用户持有、标记有效、主进程和进程组均已退出的 Profile。启动时未记录主 PID 的 Profile 保守保留供人工核对。CLI 预览与清理采用相同进程组判断。
- Computer：macOS 前台 App 观察和指定按钮点击的受限适配器；目标 App、窗口、按钮及观察哈希必须匹配。窗口及按钮文本脱敏，变化哈希保留复核能力。系统设置、终端、钥匙串和已列入保护集的密码管理器硬拒绝。无辅助功能权限时明确失败。`capability-computer` CLI 通过生成客户端发出观察与点击，并按原 Job ID 查询结果；专用临时 App 的真实按钮点击已通过。

## 仿真与门禁

| 范围 | 验证方法 | 结果 |
| --- | --- | --- |
| Chrome 与 Core 正式链路 | 隔离 SQLite、真实回环 HTTP Core、真实本机 Chrome 与本地页面；注册、心跳、租约、observe→navigate→observe→fill→observe→click→回执；生成 SDK 和 CLI 分别调用 | 通过；密码字段拒绝、Core 入队前拒绝明文输入；`fill` Job SQLite 只见密文封装，结果 API 无输入值；页面回显同值文本不进入观察回执，页面或表单改变后旧观察拒绝，插件禁用后 Worker 停止。混合来源 DNS 回归通过 |
| 既有 Remote Target 兼容 | 非本机 `REMOTE_ENDPOINT` 的 Browser Target 经正式 API 提交原有 `submit` 操作，保留目标引用、前置条件和原动作参数 | 通过；本机插件专属校验只作用于 `operant.chrome.browser` 与 `operant.macos.computer` Target |
| 正式 Agent 工具与真实模型 | 从 Provider Discovery 取得精确模型 ID `gpt-6-luna`；临时 ModelProfile、Role 工具白名单、`ApplicationService.run_session`、真实本机 Chrome/Target/Worker，执行观察→导航→填写非密码字段→点击→观察 | 通过：页面产生预期 `accepted`，`tool.started`、`tool.completed`、`agent.completed` 均出现，Worker 无错误；三次有副作用动作由测试场景内逐项审批，持久 `model.completed` 只含扩展参数摘要。可重复命令为 `OPERANT_PHASE4_REAL_MODEL=1 OPERANT_ACCEPTANCE_MODEL_ID=gpt-6-luna OPERANT_ACCEPTANCE_ENV_FILE=<未提交环境文件> uv run --offline python -m tests.acceptance.phase4_browser_agent`。没有真实账号或对外提交。发现列表中的 `gemini-2.5-flash-lite` 在同一 Chat Completions 入口返回 HTTP 404，未作为通过证据 |
| 隔离第三方 Tool 与真实模型 | 当前 Provider Discovery 的精确模型 ID `gpt-6-luna`；临时安装包、实际 macOS 沙箱、带安装 ID 的 Role 授权、正式 `ApplicationService.run_session` | `tool.started`、`tool.completed`、`agent.completed` 均出现；模型调用参数在持久事件中只留摘要，执行结果含不可信来源标记。可重复脚本为 `tests/acceptance/phase4_external_tool_agent.py`，临时包及数据库在脚本结束后删除 |
| Live Remote 结果读回 | 临时 Core 中放入结果不明的 Browser Job，正式 Vite Live 页面读取持久回执；在 1280px 与 375px、明暗主题检查可见性、指针点击、Escape 和焦点 | 通过：原 Job ID、中文“需人工核对”、错误码和不自动重试说明可见；Escape 关闭后焦点回到结果按钮。[桌面浅色](evidence/job-result-desktop-light.png)、[窄屏深色](evidence/job-result-mobile-dark.png)截图留证。页面不发起能力动作，临时浏览器、Core 和 Vite 已关闭 |
| 网络出口 | 单独测试代理的主机白名单、HTTP 写请求窗口、WebSocket Upgrade 和非白名单 CONNECT；真实 Chrome 页面尝试连接非白名单 WebSocket | 代理定向检查及 Chrome 负例通过；初版 CDP 拦截曾漏掉 WebSocket，已改用本机代理并复测两轮真实 Chrome 链路。仍不外推为经渗透测试的任意网页沙箱 |
| 可见窗口与接管仿真 | 用专用临时 Profile 启动 `visible=True` Chrome；独立 DevTools 会话在页面输入框中改值，随后让原 Connector 按旧观察点击，再以新观察点击 | 真实 Chrome 验证旧动作拒绝、新动作成功；点击回执在页面事件处理后记录更新的 DOM。外部会话模拟用户接管，尚未声称真人在窗口中完成验收 |
| Profile 保留边界 | 检查活动 Profile；构造过期原进程、无标记目录、主进程退出但进程组仍活和无关目录；回收后核对；再以真实 Chrome 检查启动与停止 | 仅主进程和进程组均已退出的合格 Profile 被删除；活动进程组、无标记及无关目录保留，预览同步显示保留原因。修复后真实 Chrome 回归未新增遗留目录；此前一次失败的验收留下 1 个约 4.4 MB 的无标记目录，按保留边界不自动删除 |
| CLI 生命周期 | 临时配置根执行 install→enable→list/readback→disable→uninstall | 全部成功，安装记录默认禁用；未触及用户默认数据目录 |
| 第三方 Tool 真实隔离 | 临时本地包安装、真实 macOS 沙箱探测、正式 Tool 工厂加载；插件尝试读包外临时文件、连本机监听端口、读取 Core 测试环境变量；重装后检查数据目录 | 最新安装 ID 和目录校验源码的 8 项真实沙箱测试通过；三项访问均被拒绝，合法 JSON 结果标记为不可信，禁用后旧 Tool 闭包不能继续执行；重装后新数据目录不含旧安装保留文件。Linux 无已验收隔离器，启用会拒绝；不外推到 Command/Event/Provider/Runtime 插件 |
| Computer | 策略、观察变化和敏感标签脱敏的定向仿真；临时回环 HTTP Core、Lease/Gateway/Worker/生成 SDK 与独立 Cocoa App 完成观察→真实按钮点击→持久回执；`osacompile` 编译 AppleScript | 授权前 System Events 返回 `-1719`；授权后 `doctor-computer` 可读取前台窗口。真实 HTTP 进程边界的 Core/Worker 组合链路完成临时 App 按钮点击，窗口标题变化、动作前哈希及原 Job ID 回执读回通过。临时服务与测试 App 已关闭清理；可重复测试源在 `tests/fixtures/local_computer/` |
| 未知写结果 | 定向注入 `RemoteOutcomeUnknown`，即使调用方把浏览器点击标为幂等也检查 Worker 回执 | `manual_reconcile_required`；不允许按调用方标志自动重放 |
| 静态与客户端 | `ruff format --check .`、`ruff check .`、`mypy src`、`uv lock --check --offline`、`git diff --check`；GUI test/typecheck/build | 冻结后的定向检查通过；GUI 136 项测试、类型检查和构建通过 |
| Python wheel | `uv build --offline` | 本机离线缓存缺少构建依赖 `hatchling`，未生成新 wheel；源码测试通过不等于已验证候选安装包 |
| 完整 Python | 隔离 `OPERANT_DB_PATH` 后 `uv run --offline pytest -q --tb=short` | 前一提交的本地完整测试运行到 100%，退出码 0，记录在 `/private/tmp/operant-phase4-final-gate-20260928b.log`。本轮 Profile/插件数据修复后，本机两次完整测试分别在 66% 和 73% 因会话中断结束，没有最终退出码；一次沙箱内首错定位在回环端口绑定时报 `Operation not permitted`，同一单测在允许回环的环境通过。最新源码的完整门禁以 PR HEAD 的 Python 3.10/3.12 CI 为准，合并前核对。默认 `.operant` 旧库未修改 |

真实 Chrome 测试需 `OPERANT_LOCAL_BROWSER_TEST=1` 和本机浏览器进程权限；普通 CI 明确跳过该平台场景，不能把跳过算真实验收。所有仿真只用了临时库、临时网页和专用临时 Profile，未使用真实账号、用户日常浏览器 Profile 或生产远程 Target。

Chrome 代理选择与 WebSocket 的行为依据 [Chromium Proxy 文档](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md)；实际浏览器仍以本机测试为准。

## 本部分交付判定与后续边界

按目标清单第四部分的交付范围，H-15 提供了 H-16 所需的内置能力驱动、正式 Agent Tool 接入和可核验安装的隔离第三方 Tool；默认能力包复用第三部分正式入口。H-16 在独立浏览器与临时 App 完成了正式 Core/Worker/Agent 的受控真实动作及仿真接管，保留审批、目标限制、凭据隔离和未知结果边界。这构成本部分的候选交付；跨版本 CI、PR 合并与主线回读后才标记完成。

- H-15 的通用 Command/Event/Provider/Runtime 插件 API、第三方能力驱动契约及客户端安装体验尚未实现；它们需要具体消费场景和各自的隔离、权限与恢复契约，不能由本次 Tool 包推定完成。
- H-16 浏览器可见窗口下的真人接管、更多日常页面动作和 GUI 直接操控仍需后续场景验收或实现；当前受控本地任务不代表完整电脑控制。
- H-17 真实跨设备控制与执行、H-14 全局临时资源治理、H-07 交互终端仍按真实远程环境、资源压力和日常使用需要触发。本次没有生产远程设备或真实跨设备任务；目前盘点有 1 个约 4.4 MB 的无标记旧 Chrome Profile，归属无法可靠核验，按安全边界保留待人工确认，修复后真实回归没有新增遗留；没有新增日常 PTY 场景。不使用本机模拟或 Profile 回收冒充这三项完成。
- 本部分代码提交到 PR #32 的工作分支；最新跨版本 CI、合并和合并后主线回读应按最终 PR HEAD 核对。用户已授权推送和合并。

## 流程改进

默认仓库路径下的旧 `.operant` 测试库在这条新主线上触发 v19 迁移校验和冲突。完整测试应始终给本任务指定临时 `OPERANT_DB_PATH`，并在需要监听回环端口的用例中使用允许本机网络的执行环境。这样既避开旧库，又不会把沙箱拒绝端口误判为产品失败。长时间门禁若随会话中断，应以独立的 PR CI 完成最终确认，不能凭本地进度条声称通过。

## Beta 任务 4 增量核对（2026-10-04）

本节记录身份：Codex 主线程及扩展后端实施 Agent；基于 `main@8384a4e3` 的 `codex/beta-local-extensions-task4` 工作树。上文是第四部分当时的交付与失败历史，保留原判定。本节记录本轮原始范围及当前证据；原生 WebView、日常 TextEdit 和正式模型浏览器链路已通过，GUI Skill 与扩展执行反馈已通过。最终 Modal 关闭及卸载、临时窗口清理的回读追加在本节末尾；整组交付仍须冻结提交完整 CI 和受保护 PR 合并确认。

| 需求与类别 | 本轮正式消费入口及边界 | 当前实现 / 验收状态 |
| --- | --- | --- |
| H-15 Tool | 新包按安装 ID 与 `tool` 类别授权，再经 Role 工具白名单、Agent Tool Policy、Action Gateway 和隔离进程调用 | 已实现 / 正式 `gpt-6-luna` Session 的 `tool.started`、`tool.completed`、`agent.completed` 通过 |
| H-15 Command、H-11 动态命令 | 服务端发现授权命令；会话 HTTP 调用校验平铺类型化参数，结果写 Session Event 和 Thread Item；独立持久幂等收据回放成功、未知不自动重试。GUI/TUI 使用生成 Client 接口 | 后端与 TUI 已实现 / 正式 HTTP 调用、同键回放，以及实际挂载 Textual Pilot 经真实 HTTP 发现、选择、错参拒绝、执行反馈通过；GUI 动态 Slash 的正规命令发现、实际执行和宽窄屏可见完成反馈通过 |
| H-15 Event | 已持久化的有限事件类型及脱敏字段名经隔离 hook 派发，回执写正式 Session Event；清单必须声明固定 `payload_json` 参数 | 已实现 / 正式 Session 中观察到 `extension.runtime_dispatched`；非法清单声明定向拒绝通过 |
| H-15 Provider | ModelProfile 的安装身份选择器接入正式 Provider；只允许同协议、主机、端口内改路径，模型 ID 须经同源 Discovery 精确验证；Core 保管凭据、消息与权限 | 已实现 / 精确发现 `gpt-6-luna` 并通过正式模型调用；跨来源拒绝且不发 Provider 请求的定向测试通过 |
| H-15 Runtime | 每次正式 Provider 请求前运行已授权适配器，只允许收窄 `max_output_tokens`，不接管取消、预算上限或 Run 终态 | 已实现 / 真实模型两次请求的观察上限均为 96，低于原 Profile 的 256；扩权输出拒绝测试通过 |
| H-15 Capability Driver、H-16 插件接入 | 隔离扩展只产出 `{operation,arguments}` 提案；本机控制的 `drive` 再走原 Target、观察哈希、租约、Gateway 和持久 Job。提案不能证明实际副作用 | 已实现 / 六类脚本验证提案形状，GUI 扩展驱动的真实观察/导航 Job succeeded；本机动作及真人接管按 H-16 的独立真实证据通过 |
| H-11 Skill 显式调用 | 当前项目安装授权与冻结 Role 绑定决定可发现的 Skill；显式会话调用须走正式 Session、预算、工具权限和历史，管理页安装不算调用 | 已实现 / 正式 `gpt-6-luna`、生成 Client 与实际挂载 TUI 通过；GUI 正规安装、发现及当前 Core 上真实模型完成反馈通过 |
| H-15 安装管理与生命周期 | GUI 检查清单、摘要绑定安装、逐类授权、停用及卸载；调用前复核版本、隔离证据、安装身份及摘要 | 已实现 / GUI 六类安装授权和隔离进程正式消费已通过；同源包重装后 Modal 取消保留安装、确认经 ASK 卸载通过；浏览器驱动最终停用/卸载回读见本节末尾 |
| H-16 日常网页与 App 操作 | 类型化导航、非密码输入、固定按键、精确截图、有界剪贴板；动作前后证据及成功 Job 回读 | 已实现 / 正式模型浏览器链路与 TextEdit 当前源码 10 Job/16 ASK 通过；GUI 导航、输入和截图通过，详见下文 |
| H-16 接管、撤销与未知结果 | 接管等待在途动作并撤销旧租约，恢复重新授权及观察；未知动作暂停目标并保留人工核对，不能盲重试 | 已实现 / 确定性权限/代次/未知边界通过，独立 Worker 同批未知后停发及回执断线回归通过；真人输入、GUI 接管/恢复与新观测通过，正式关闭及专用窗口回收通过；最终新 Modal 实际取消、确认、ASK 与会话 closed 回读通过，早期原生确认自动化等待失败仍保留 |

六类共用 `operant-local-extension.v1` 清单、显式逐类授权、包摘要及安装 ID，安装默认禁用，调用前重新检查启用状态与摘要；macOS 实际隔离探测失败时拒绝启用。旧 `operant-tool-extension.v1` 和 `capability-plugin *-external-tool` 入口继续只授予 Tool。格式、CLI 与 GUI 管理方法见[扩展包说明](external-tool-package.md)。本轮定向检查为 `tests/test_local_extensions.py tests/test_api_extensions_journal.py tests/test_external_tool_plugin.py tests/test_local_control_manager.py` 共 16 通过、2 个可选平台项跳过；Ruff、mypy 和所改文件的 `git diff --check` 通过。跳过项不算 macOS 隔离验收；六类真实脚本在 macOS 隔离器下另行通过。

本轮真实模型重跑入口为：

```bash
UV_CACHE_DIR=/private/tmp/operant-task4-uv-cache uv run python -m tests.acceptance.beta_task4_extensions_real --evidence-dir /absolute/fresh/synthetic-evidence-dir
```

脚本在进程内读取原工作树未提交的 `.env`，不复制或输出凭据；使用绝对合成工作区、临时 SQLite、正式 Discovery、ModelProfile 和 `ApplicationService.run_session`。最近一次证据是忽略目录 `.operant/beta-task4/evidence/extensions-six-category-real-03/result.json`：`status=passed`、精确模型 ID `gpt-6-luna`、正式 Tool/Agent 完成、Event 回执、真实请求输出上限 `[96,96]`、能力驱动仅提案、Command HTTP 200 且同键回放相同。`extensions-six-category-real-01/result.json` 的 `IsolationUnavailableError` 是受限执行环境不允许创建沙箱探测所需的本机哨兵文件；该失败保留，随后在允许本机沙箱探测的环境中复验通过。`-02` 是通过但未记录实际 Provider 请求上限的早期证据，`-03` 补齐了这一观察。

H-11 范围复核补齐 Skill 显式命令后，`tests/acceptance/beta_task4_skill_command_real.py --evidence-dir <新的绝对合成目录>` 通过正式 Discovery 确认精确 `gpt-6-luna`，使用 ModelProfile 和 Phase56 HTTP 路由进入正式 Session。`skill-command-real-final/result.json` 为 `passed`：`disable-model-invocation: true` 的安装包不进入普通自动上下文，显式调用进入该 Skill 上下文，真实回答匹配合成标记，同键回读不重复模型调用，命令开始/完成及 Agent 完成历史齐全。该脚本的 HTTP 路由由 httpx ASGI Transport 调用，不能据此声称独立网络服务或生成客户端已验收。

`tests/acceptance/beta_task4_skill_tui_pilot.py` 则以实际挂载的 Textual Pilot、生成 Phase56 Python Client 和独立回环 HTTP Core 验证发现、补全、错参拒绝及正式 Session 反馈，`skill-command-tui-standalone/result.json` 为 `passed`；此客户端场景使用确定性 Provider，真实模型证据在前一段单独记录。复跑为 `uv run --project clients/tui --no-sync python -m tests.acceptance.beta_task4_skill_tui_pilot --evidence-dir <新的绝对目录>`，常规 TUI `unittest discover` 为 32/32 通过。早期 `skill-command-tui-final` 保留当时 19 项结果；原 pytest Pilot 文件移出 TUI 常规测试目录，避免 CI 环境缺失测试辅助模块及仅被导入不执行的问题。

Skill 定向 9 项通过：覆盖冻结 Role、摘要/审批后授权变化、另一 Manager 持久停用、参数、历史、未知与同键。GUI 首次发现空安装根曾返回 403 `FileNotFoundError`；新增正常新项目 GET 200 空列表且不创建目录的回归，丢失已安装目录也不展示或执行，不以手工创建目录掩盖缺口。GUI 后续正式安装流程本身会创建托管包目录。

`tests/acceptance/phase4_browser_agent.py` 在本轮内置 Tool 授权代次改动后，以正式 Discovery 精确返回的 `gpt-6-luna`、ModelProfile、`ApplicationService.run_session` 和专用 headless Chrome 重跑。绝对临时工作区与本机合成页面完成观察、导航、输入、点击和再观察；`tool.started`、`tool.completed`、`agent.completed`、扩展参数脱敏及页面 `accepted` 均通过，证据为 `.operant/beta-task4/evidence/browser-agent-final/result.json`。原 `.env` 只在目标进程用于 Provider 鉴权，不复制或输出凭据；脚本退出时关闭 Chrome、Worker、Core 和页面服务。首次执行请求曾被自动审批以“真实模型发送尚缺明确授权”拒绝，未启动脚本；补交用户提供的 AGENTS.md 必要真实模型验收指令及合成输入证明后，同一路径获批重跑。此场景不覆盖新增的截图、剪贴板、GUI、原生 WebView 与真人接管；这些能力按本轮正式 Core/GUI 动作入口分别验收，无须让模型逐一选择每个操作。

TUI 动态命令独立重跑命令为：

```bash
UV_CACHE_DIR=/private/tmp/operant-task4-uv-cache clients/tui/.venv/bin/python -m tests.acceptance.beta_task4_tui_command_real --evidence-dir /absolute/fresh/synthetic-evidence-dir
```

它复用同一六类本地包，在真实 macOS 沙箱下只授权 Command，以临时 Core 和生成 Phase56 Client 连接实际挂载的 Textual 会话页，不调用模型、不占 GUI 焦点。`.operant/beta-task4/evidence/tui-command-real-04/result.json` 记录服务端发现的安装身份命令、Slash 选择项、错参 HTTP 400 及完成反馈，状态为 `passed`。`-01` 在受限环境的沙箱哨兵创建处失败；`-02`、`-03` 是验收脚本断言文案过窄，Core 实际已返回 schema 拒绝；收紧断言到明确的 HTTP 400/schema 后 `-04` 通过，历史失败均保留。

### 日常 macOS App 正式链路

本段记录身份：Codex 主线程；读取本机操控实施 Agent 的正式结果及脚本核对。`tests/acceptance/beta_task4_local_control_real.py` 使用隔离 HTTP Core、临时 SQLite 和当前生成的 Phase56/Phase45 SDK，通过精确 App 白名单操控真实 TextEdit 临时文档。默认策略为 ASK，16 次审批逐项完成，10 个能力 Job 均为 `succeeded`：非密码文本输入、固定按键、精确窗口截图、剪贴板写入与读回，以及操作之间的重新观测。PNG 为 1516×1060，正文经短期工件接口读取；SQLite 没有输入明文或 PNG 正文。脚本在结束时逐格式恢复原剪贴板并检查恢复结果，没有使用日常用户文档。

通过证据为忽略目录 `.operant/beta-task4/evidence/textedit-formal-05/result.json`；回读 `status=passed`、`db_plaintext_absent=true`、`clipboard_all_formats_restored=true` 和两套协议元数据。`-01` 保留了临时文档标题及窗口启动时序的前置检查失败；`-02`～`-04` 因直接脚本导入了环境中旧生成 SDK 而握手拒绝，未绕过协议协商。改用当前仓库模块入口后 `-05` 通过，并重新安装当前工作树 Python 包核对打包 SDK 的 digest 为 `1a0877e42bbe338bc442c4c6c7032fe6653af6ac9efa801da3615cdf4d2d9a04`。这不代表更新了已安装桌面 App。

观察代次与插件撤销代次修复后，在当前源码以同一正式入口复验，`.operant/beta-task4/evidence/textedit-formal-06/result.json` 为 `passed`：10 个能力 Job 全成功、16 次 ASK 审批、窗口 PNG 1516×1060、输入明文未落库、剪贴板所有格式已恢复，Core 会话关闭。这份结果覆盖本轮 Computer 内容校验的改动；`-05` 的早期通过仍保留。

### GUI 与原生 WebView 的当前核对

GUI 已实际完成六类包检查、安装及授权、动态 Slash 命令发现/参数输入/执行回执，并检查 390px 窄屏、键盘焦点和断线显示。当前证据位于 `.operant/beta-task4/evidence/gui/`，包括 `extensions-six-granted-wide.png`、`extensions-narrow-keyboard-focus.png` 和 `local-control-disconnected-narrow.png`。`extension-command-http-receipt.json` 保留实际 GUI 请求的安装身份命令、合成参数、HTTP 200、`status=completed` 及正式历史项 ID；旧 `extension-slash-completed-wide.png` 和 `-narrow.png` 的视区没有拍到完成提示，不能单独证明可见执行反馈。

主线程在独立 `dev.operant.task4acceptance` 临时 Tauri App 中关闭演示模式，连接本轮回环 Core；真实 macOS WebView 显示六类安装身份、能力驱动、会话和观测/人工接管入口。Tab 移动后的焦点轮廓和 Core 已连接状态见 `tauri-extension-focus.png`。这是开发壳真实挂载检查，不是候选安装包、签名或发布验收。 临时 Tauri App 和全部验收 TextEdit 文档已关闭；TextEdit 遗留空“打开”面板通过 CUA 取消后退出，回读 `isRunning=false`。接管所需的专用 Chrome 保留至该项结束，未关闭日常用户窗口，清理回执为 `gui/window-cleanup.json`。

实际 GUI 浏览器操作发现两个问题：长审批使观测过期，以及不变页面再次观测复用旧记录而无法刷新 TTL。Core 已拒绝副作用；GUI 已清除失败观测并按到期时间禁用操作。`browser-stale-observation-blocked.png` 保留拒绝证据。后端修复已由当前 Core 正式 GUI 复验，真人输入及恢复新观察通过；最终 Skill GUI 反馈和 Modal 收尾证据见本节后文。冻结提交完整 CI 通过并正常合并前，仍不标任务 4 整组交付完成。

后端已修复重复观测：每次本机观察绑定独立 Job 代次，旧记录不延长有效期；Worker 从该记录的内容摘要检查当前界面，密封输入仍绑定代次。27 项定向测试通过、3 个原有平台跳过，覆盖新观察可用、旧观察到期拒绝、伪造内容摘要被 Core 重算及旧密文不能配新代次。独立 Review 又发现跨 CLI 快速停用再启用会沿用旧租约，已增加持久插件代次并在会话、Worker 和 Agent Tool 调用前核对；22 项生命周期/管理测试通过。后台循环主动清除到期 PNG/剪贴板正文，空闲缓存清理也有断言。重启加载后的正式 GUI 已取得新观察并执行导航；真人输入改变表单后，恢复控制再次产生成功的新观察。

### 当前门禁与包检查

全仓 Ruff 格式（520 个文件）、Ruff、mypy（177 个源文件）、离线锁及差异检查通过；GUI 145 项测试、类型和构建通过，正常 TUI unittest 32 项通过。最终协议/本机管理/六类扩展的六份定向测试为 29 通过、1 个平台项跳过。Python wheel 构建成功，在仓库外的新环境安装后核对了正式路由、持久插件代次及打包 SDK digest；早期证据为 `.operant/beta-task4/evidence/package/wheel-smoke.json`；包含最终 Skill 空安装根及 admission 修复的包安装回读为 `package-final-03/wheel-smoke.json`，包内 Core/生成 SDK 均从仓库外 site-packages 导入，正式路由及 digest `d67d43db…41a61` 相符，空 Skill 根正常返回空列表且不创建目录。新环境独立解析兼容依赖，这个安装烟测不代替锁定依赖的基础门禁。

本机完整 pytest 曾运行到终端退出场景并长时间停在读帧等待，保留 `/private/tmp/operant-task4-pytest-complete.log` 的中断摘要：1235 通过、10 跳过、1 失败、退出码 2。唯一已执行失败是旧测试从 Provider 包装器读取 `calls`；修复为检查原始记录 Provider，保持恢复不重复调用的断言。卡住用例独立复验通过，尾段及该修复共 63 项通过（`/private/tmp/operant-task4-tail.log`）。该中断不记为完整门禁通过；最终提交须经完整跨版本 CI 后才能合并，跳过平台项仍由本轮真实验收分别覆盖。

最后范围 Review 找到独立 Worker 在同批首个未知结果后仍可能执行下一 Job，以及回执失败前尚未暂停的缺口，均已修复。Worker 与 Core 改为顺序领取单个 Job，本地未知判定先暂停再发回执；Core 对同目标、同租约的持久未知拒绝继续领取，即使另一个 Controller 实例或已写人工核对审计也不能复用旧租约。未执行的后续 Job 保持 queued，再由既有租约过期/撤销处理。四文件定向检查 28 通过、1 个原有平台跳过；补充回执断线及持久阻断三项通过，Ruff、mypy 177 源文件与差异检查通过。确定性测试覆盖 `test_local_capability_worker.py` 的失败回执及超额批量响应、`test_phase5b_remote_execution.py` 的多个排队 Job 和跨 Controller 旧租约阻断；没有用它们替代真人接管。

真人接管记录 `gui/human-takeover-readback.json`：用户回复“已输入”后，只读检查专用 Chrome 的指定输入框，33 字符合成标记精确匹配；Agent 未代填或提交。接管时自动观察 HTTP 422 且未创建作业。GUI 恢复后获取新 fencing 和成功观察 `remote_job_608d57968dc94f31a1ccd65e6f2d4955`，表单摘要由 `50d9…` 变为 `88f434…`。GUI 原生关闭确认导致 agent-browser guardian 等待，接受确认后 Core 仍 active；以正式关闭接口、Phase45 审批及同一幂等键完成，状态 closed、未知列表为空，专用 CDP 51710 已拒绝连接，Profile 已回收。这一工具故障不计作本次 GUI 关闭成功；此前 GUI 关闭成功证据仍保留。

最终 H-11 并发回归发现批准先消费而活动 Run 未获准的边界；修复先取得 Session admission，再消费 Gateway 批准，ASK 或异常路径释放。`test_skill_commands.py` 最终 9 项通过，包含已批准同键在会话忙时不被耗尽、Run 结束后可执行及异常释放；协议不变。修复后的实际挂载 TUI 重跑 `skill-command-tui-admission-final/result.json` 通过，正式独立 HTTP、生成 Client 与 Session 消费均覆盖。

最终 GUI H-11：`skill-slash-completed-wide.png`/`narrow.png` 可见当前已连接 Core 的 `/skill:skill_ead8…` 完成反馈及资源 Session `session_7b8e36f9f3d34cf396f0c1843fba28b7`，正式 `gpt-6-luna` 回复合成标记 `OPERANT_SKILL_GUI_OK`；Thread 历史保留合成用户请求和 Agent 回复。`extension-slash-visible-final-wide.png`/`narrow.png` 可见正规注册 Command 完成并提示结果已进入 Core 历史。主线程已实际读取宽屏 Skill 与窄屏扩展截图，不用早期仅有历史的截图代替反馈。

确认交互的收尾修复：新扩展面板两处卸载和本机控制一处关闭改用现有 `Modal`，复用焦点约束、Escape 与 portal，取消不发请求，忙碌/断线/目标状态变化拒绝提交；Core 审批与同键提交逻辑不变。原 agent-browser 原生 confirm 卡住记录保留：旧六类包曾以正式 DELETE 和同键 Phase45 审批兜底清理，该次不计为 GUI 卸载通过。最终 GUI Modal 的取消、确认与 Core 状态回读另行记录。

主线程在最终 Core 回读持久 Job（`gui/final-driver-job-readback.json`）：驱动后的导航 `remote_job_6f945ec24d614786b3071122213e7ab8` 为 succeeded、fencing 1；接管恢复的成功观察 `remote_job_608d57968dc94f31a1ccd65e6f2d4955` 为 succeeded、同目标的新租约及 fencing 2。不是只把能力提案形状或 Relay Ack 当作实际动作成功。

最终原生壳在当前前后端再次只读挂载：正式 `OPERANT_CORE_URL=http://127.0.0.1:18464` 注入后，独立临时 Tauri WebView 已连接当前 d67… 协议 Core，实际显示正式 Skill 请求/回复 Cursor 4/6 和 `OPERANT_SKILL_GUI_OK`，扩展页渲染当前卸载后的 Core 状态，Tab 焦点与截图保存于 `gui/tauri-final-connected.png`、`tauri-final-ax.txt`。首个启动遗漏外部 Core 参数，尝试寻找本机 `operant` 命令并退出，错误记录保留在 `/private/tmp/operant-task4-native-final.log`；修正验收启动参数后通过，没有修改安装程序或用户 App。

最终管理链路已通过实际 GUI Modal：六类同源包取消/Escape 保持 installed，确认经 ASK 后卸载；会话 `local-control-7afab5459f30f2f1317ee0c8` 取消保持 active 且无 Job，确认经 ASK 与原幂等键完成 closed；浏览器驱动经 GUI 停用、确认卸载。主线程独立正式 HTTP 回读 `gui/management-final-main-readback.json` 为 passed：插件和外部扩展列表为空、全部会话 closed、未知 Job 列表为空。`gui/source-manifest-delivery.json` 固定 441 份生产输入，相对最终后端冻结仅两处确认 Modal 的 GUI 文件变化；未受影响的模型、TextEdit 与协议证据继续有效。整组功能场景已覆盖，最终完整 CI 与正常 PR 合并仍为交付门禁。

`gui/modal-cleanup-final.json` 保存上述新 Modal 的取消/确认、GUI ASK、同键重试及最终状态；前端生产代码已冻结，Modal 改动后的 GUI 145/145、类型、构建、diff 全通过。专用会话 Chrome 已回收，`agent-browser --session task4-gui close` 返回 Browser closed，session list 已无该会话；临时 Tauri 与 TextEdit 亦已退出，用户日常 Chrome 与其他会话保留。

首轮冻结提交 `5a6a720` 的 CI `37204778544` 已完成：Python 3.10/3.12 各 1293 通过、16 跳过、1 警告，TUI 各 32，GUI 145/类型/构建通过。CodeQL 三个分析作业成功，但汇总检查 `111443664434` 报 alert 40（ref `refs/pull/37/head`）源路径注入；查询 merge ref 的空结果不能排除该告警。已在全部作业汇总后修复：inspect/install 固定已打开的目录 FD，只读固定 manifest.json/plugin.py，O_NOFOLLOW/O_NONBLOCK 和普通文件/长度校验拒绝符号链接、`..` 及 FIFO；保留可信本地来源和原协议。14 项定向通过、2 个 opt-in 跳过；另开实际 macOS 隔离器后旧 Tool 与六类扩展两项真实复验通过，正式 HTTP 检查/安装回归通过，Ruff/mypy/diff 通过。原受限沙箱哨兵权限失败保留。模型消费、GUI 命令及本机控制逻辑未变，复用未受影响证据；修复后的完整 CI 与 CodeQL 消除仍须新 HEAD 确认。

最终路径修复包 `package-final-04/wheel-smoke.json` 为 passed：新 wheel SHA-256 `c6e4b974b0e3d61680fa15901260370992096c7e42c5220a77e7702e969d4e04`，仓库外 site-packages 导入，合法来源 inspect/install 摘要匹配，符号链接和 FIFO 拒绝、正式路由齐全。`source-manifest-delivery-codeql.json` 固定最终生产输入；与前一冻结相比仅 external_tool.py 的源文件读取边界改变。

第二轮 `3dfb7b7` 的 CodeQL 汇总 alert 41 仍指向 Path.expanduser：第一版实际 FD 边界回归通过不足以声明静态告警消除。进一步修复在 os.path 规范化后校验选定父目录边界，再打开目录 FD；目录须当前用户/root 所有且不可被组或其他用户写。保持任意满足信任条件的本地来源。官方 CLI 2.27.1 + Python 查询包 1.8.11 与 CI 同版，当前源码重新抽取后定向 PathInjection.ql 的 external_tool.py 结果为 0；主线程核对数据库 src.zip 内该文件摘要与当前源码一致，记录 `ci-pr37/local-codeql-path-check.json`。14 项定向及 2 项真实 macOS 沙箱再次通过，静态检查通过；历史全库其他路径告警未据此宣称清零。最终 wheel `package-final-05/wheel-smoke.json` 仓库外安装、合法来源摘要、symlink/FIFO 拒绝及正式路由通过，SHA-256 `d2bb1bf951edb0f4f16ccd2b316e07b2d3c13ac358e21963c6ab30b150b2df98`。远端最终新 HEAD 的完整门禁与汇总 CodeQL 仍待确认。
