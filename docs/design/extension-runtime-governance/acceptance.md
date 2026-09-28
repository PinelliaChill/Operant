# 第四部分：扩展能力与运行治理验收记录

> 2026-09-27；记录身份：Codex；适用对象：本实施分支及后续集成者。
> 基线：`d13c6b1d9e962b68cb6f752125268558bae4d0e5`；实施分支：`codex/extension-runtime-governance`。最终提交、CI、PR 与合并状态待完成后补录。

## 已实现的可验证范围

- H-15：`CapabilityPluginRegistry` 对随 Core 发布的 Browser/Computer 适配器提供显式安装、精确目标白名单、源码摘要绑定、启停、卸载和进程间文件锁。版本化 `ToolExtension` 把受信能力工具接入 Role 的工具白名单、Agent/Graph 工具定义和 Action Gateway；`role add/update` CLI 能显式授予或撤销扩展工具并保留历史 Role 版本。Agent 审计及持久 `model.completed` 事件只保存扩展参数摘要，具体动作仍由 Core 的正式 Capability Gateway 再裁决。未认证外部包不能安装；记忆 `PluginHost` 仍独立，未宣称通用第三方插件 Host 已交付。
- 默认能力包沿用第三部分的 `/v1/skills/default-pack/install`：只从配置的可信 Skill 根发现并安装 `grill-me`、文档、演示文稿、PDF、创建/查找 Skill，不联网下载或额外授予工具权限；本部分复用了该入口，没有重新实现一套包管理器。
- H-16：`LocalCapabilityWorker` 复用现有 Remote Target 注册、身份心跳、短期 Lease/fencing、Action Gateway、观察哈希、Job 和持久 Receipt。Worker 不直读 Core 数据库，租约 Token 只从环境变量进入 Worker，不进入 Chrome/osascript 子进程环境。禁用插件后拒绝新任务，未知非幂等动作转人工核对。
- Browser：独立 Chrome Profile、精确来源白名单（协议/主机/端口）、DNS 地址检查与固定解析、CDP 请求拦截及本机过滤代理；代理没有直连回退，逐次验证请求/CONNECT 目标，未授权 HTTP 写请求拒绝。公网域名只能解析到公网地址，本机名只能解析到回环地址；Worker 强制排除 Core 来源和本机 Core 端口。类型化观察、导航、非密码文本输入和点击；观察列出可点击元素/字段并给表单状态哈希；动作前复核观察，用户接管修改表单会让旧动作失败。`fill` 值以短期 Lease Token 派生的 AES-GCM 密钥封装，并绑定目标、租约、观察哈希、字段与幂等键；Core 拒绝旧明文字段，持久 Job 只保存密文，Worker 执行前解封。Core 也拒绝带查询参数的导航 URL。可选 `--visible` 展示专用窗口。正文限长并脱敏，已输入值和页面回显同值文本不进入观察回执，页面自身请求的 URL 查询参数只留摘要。正常退出即删除 Profile；异常退出后一小时，只回收本用户持有、标记有效、原进程退出的 Profile。CLI 可预览 Profile 大小、状态和保留原因，再执行安全清理。
- Computer：macOS 前台 App 观察和指定按钮点击的受限适配器；目标 App、窗口、按钮及观察哈希必须匹配。窗口及按钮文本脱敏，变化哈希保留复核能力。系统设置、终端、钥匙串和已列入保护集的密码管理器硬拒绝。无辅助功能权限时明确失败。`capability-computer` CLI 通过生成客户端发出观察与点击，并按原 Job ID 查询结果；实际输入验收仍需系统授权。

## 仿真与门禁

| 范围 | 验证方法 | 结果 |
| --- | --- | --- |
| Chrome 与 Core 正式链路 | 隔离 SQLite、真实回环 HTTP Core、真实本机 Chrome 与本地页面；注册、心跳、租约、observe→navigate→observe→fill→observe→click→回执；生成 SDK 和 CLI 分别调用 | 通过；密码字段拒绝、Core 入队前拒绝明文输入；`fill` Job SQLite 只见密文封装，结果 API 无输入值；页面回显同值文本不进入观察回执，页面或表单改变后旧观察拒绝，插件禁用后 Worker 停止。混合来源 DNS 回归通过 |
| 既有 Remote Target 兼容 | 非本机 `REMOTE_ENDPOINT` 的 Browser Target 经正式 API 提交原有 `submit` 操作，保留目标引用、前置条件和原动作参数 | 通过；本机插件专属校验只作用于 `operant.chrome.browser` 与 `operant.macos.computer` Target |
| 正式 Agent 工具与真实模型 | 从 Provider Discovery 取得精确模型 ID `gpt-6-luna`；临时 ModelProfile、Role 工具白名单、`ApplicationService.run_session`、真实本机 Chrome/Target/Worker，调用 `ext_browser_observe` | 通过：`tool.started`、`tool.completed`、`agent.completed`，Worker 无错误；持久 `model.completed` 只含扩展参数摘要。可重复脚本为 `tests/acceptance/phase4_browser_agent.py`。仅验证只读观察，不外推到真实模型自主提交。发现列表中的 `gemini-2.5-flash-lite` 在同一 Chat Completions 入口返回 HTTP 404，未作为通过证据 |
| Live Remote 结果读回 | 临时 Core 中放入结果不明的 Browser Job，正式 Vite Live 页面读取持久回执；在 1280px 与 375px、明暗主题检查可见性、指针点击、Escape 和焦点 | 通过：原 Job ID、中文“需人工核对”、错误码和不自动重试说明可见；Escape 关闭后焦点回到结果按钮。[桌面浅色](evidence/job-result-desktop-light.png)、[窄屏深色](evidence/job-result-mobile-dark.png)截图留证。页面不发起能力动作，临时浏览器、Core 和 Vite 已关闭 |
| 网络出口 | 单独测试代理的主机白名单、HTTP 写请求窗口、WebSocket Upgrade 和非白名单 CONNECT；真实 Chrome 页面尝试连接非白名单 WebSocket | 代理定向检查及 Chrome 负例通过；初版 CDP 拦截曾漏掉 WebSocket，已改用本机代理并复测两轮真实 Chrome 链路。仍不外推为经渗透测试的任意网页沙箱 |
| 可见窗口 | 用专用临时 Profile 启动 `visible=True` Chrome，观察 `about:blank` 后关闭 | 启动与关闭通过；人工在窗口中接管的完整交互尚未验收 |
| Profile 保留边界 | 检查活动 Profile；构造过期原进程、无标记目录和无关目录；回收后核对 | 仅原进程已退出的合格 Profile 被删除；活动、无标记及无关目录保留 |
| CLI 生命周期 | 临时配置根执行 install→enable→list/readback→disable→uninstall | 全部成功，安装记录默认禁用；未触及用户默认数据目录 |
| Computer | 策略、观察变化和敏感标签脱敏的定向仿真；真实回环 Core/Lease/Gateway/Worker/生成 SDK 完成观察→按钮点击→持久回执；`osacompile` 编译 AppleScript；另编译并启动独立临时 Cocoa 测试 App 做前台真实点击 | 仿真链路与脚本语法通过。真实测试 App 能启动，读取进程标识可用，但读取窗口/按钮时 System Events 返回 `-1719`（不允许辅助访问）；`capability-plugin doctor-computer` 也明确报辅助功能未授权。真实输入未验收，测试窗口已关闭，临时 App 已清理。可重复测试源保留在 `tests/fixtures/local_computer/` |
| 未知写结果 | 定向注入 `RemoteOutcomeUnknown`，即使调用方把浏览器点击标为幂等也检查 Worker 回执 | `manual_reconcile_required`；不允许按调用方标志自动重放 |
| 静态与客户端 | `ruff format --check .`、`ruff check .`、`mypy src`、`uv lock --check --offline`、`git diff --check`；GUI test/typecheck/build | 冻结后的定向检查通过；GUI 136 项测试、类型检查和构建通过 |
| 完整 Python | 隔离 `OPERANT_DB_PATH` 后 `uv run --offline pytest -q --tb=short` | 冻结源码的监督运行到 100%，退出码 0；结果保存在 `/private/tmp/operant-phase4-frozen-gate.log` 与对应 `-status.json`。默认 `.operant` 旧库的 v19 校验和不匹配，未修改该库 |

真实 Chrome 测试需 `OPERANT_LOCAL_BROWSER_TEST=1` 和本机浏览器进程权限；普通 CI 明确跳过该平台场景，不能把跳过算真实验收。所有仿真只用了临时库、临时网页和专用临时 Profile，未使用真实账号、用户日常浏览器 Profile 或生产远程 Target。

Chrome 代理选择与 WebSocket 的行为依据 [Chromium Proxy 文档](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md)；实际浏览器仍以本机测试为准。

## 本部分仍需完成

- H-15 已有受信 Tool 与能力驱动扩展接口；通用 Command/Event/Provider/Runtime 插件 API、第三方认证和隔离、客户端安装体验尚未实现。
- H-16 电脑实际输入、浏览器可见窗口下的人工接管、更多日常页面动作和 GUI 直接操控仍需验收或实现；GUI 已有持久结果读回，Agent 入口已完成真实只读观察，提交/输入尚未做真实模型自主动作验收；当前浏览器真实链路是受控本地任务，不代表完整电脑控制。
- H-17 真实跨设备控制与执行、H-14 全局临时资源治理、H-07 交互终端均未由本次局部实现验收。原顺序把三项分别绑定真实远程环境、资源压力和日常使用需要；本次没有生产远程设备或真实跨设备任务，专用浏览器 Profile 盘点为 0 个/0 字节，且没有新增日常 PTY 场景。故保留条件触发，不用本机模拟或临时 Profile 回收冒充这三项完成。
- 推送、PR、CI、合并和合并后主线回读尚未完成。用户已授权推送和合并；完成代码验收后执行。

## 流程改进

默认仓库路径下的旧 `.operant` 测试库在这条新主线上触发 v19 迁移校验和冲突。完整测试应始终给本任务指定临时 `OPERANT_DB_PATH`，避免收集期碰用户数据，也避免先运行后才发现环境不匹配。真实浏览器仿真只在代码冻结后的集中验收运行，减少多次启动 Chrome 与重复门禁。
