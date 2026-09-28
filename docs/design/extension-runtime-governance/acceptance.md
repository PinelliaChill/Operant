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
