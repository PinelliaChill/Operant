# 更新记录

## v0.1.0-beta.2 · 2026-10-05

相比首个源码 Beta，本次整合会话式多 Agent 工作台、14 类可视化编排节点、可调自动审批、记忆治理与经验 Skill、本机扩展和私有跨设备能力。补齐文件/Diff/终端、配置与计划、上下文压缩、资源管理，以及六项默认 Skill 和隔离安装、备份回退流程。修复长任务记忆召回和桌面退出后的自启 Core 残留。

数据库为 SQLite v23。提供固定标签的源码、Core/TUI 包与 macOS arm64 桌面 Beta 包；桌面未经 Developer ID 签名或公证。支持范围、安装步骤和检查结果见[完整发行说明](docs/releases/v0.1.0-beta.2.md)。

本版改用 PolyForm Noncommercial 1.0.0；超出允许用途的商业使用须协商书面授权，历史 Apache-2.0 权利保持不变，见 [商业授权说明](COMMERCIAL.md)。

以下保留各开发批次的原始时间与边界；这些“未发布”记录描述当时状态，现已纳入 beta.2，不改写历史验收结论。

## 未发布 · 日常组合与分发（2026-10-05）

- 六项默认 Skill 随 Core 包分发，增加可选文档依赖和限定工作区的 DOCX/PPTX/PDF、Skill 创建与发现工具。
- 修复长任务记忆检索词被截断，并明确已发布版本与内容信任的区别。
- 桌面退出回收自己启动的 Core，增加完整数据备份、校验、隔离升级与回退和候选启动说明。

SQLite 保持 v23。日常真实任务、安装与整轮 Beta 结论见[任务 6 验收](docs/design/beta-daily/acceptance.md)；本轮不更新用户已安装 App 或真实用户库，不创建新发行版本。

## 未发布 · 私有跨设备远程闭环（2026-10-05）

- 新增正式设备 CLI、GUI/TUI 配对与 Scope、签名加密的远端 Session 创建/运行/结果 Query；Host Ack 由 Core 持久收据提供，断线后按追加 Cursor 追平，待确认命令仅重送原签名帧。
- 新增只开放配对、加密 Query、WSS 和 Target Lease 验证的本机 TLS edge；私有 SSH 隧道不暴露完整 Core 管理 API。
- 新增 HTTPS 远端 Target 的只读文件与精确 argv 白名单执行、逐次 Core Lease/Job 验证、签名结果回读；未知非幂等结果保留人工核对。

本任务工作分支的 SQLite Schema 升至 v23。实现说明见[项目架构](docs/PROJECT_ARCHITECTURE.md)，真实 macOS↔Ubuntu 验收结果以[任务 5 记录](docs/design/beta-remote/acceptance.md)为准；不创建新发行版或更新已安装 App，也不代表公网部署验收。

## 未发布 · 本机操控与扩展（2026-10-04）

- 本地扩展包补齐工具、命令、事件、Provider、Runtime 和能力驱动，GUI 提供清单检查、分类授权与安装管理；不同安装保留独立身份和数据。
- GUI/TUI 会话发现并显式调用扩展及 Skill 命令，校验参数、遵守冻结权限和预算，结果进入正式历史；未知结果不会自动重放。
- GUI 接通本机浏览器和白名单 App 的观察、输入、截图、剪贴板及人工接管；输入加密入队，截图与剪贴板正文仅在短期内存中保留。
- 观察与插件授权绑定独立代次；撤销后旧租约失效，未知动作暂停目标，即使回执断线也不会继续执行后续作业。

支持范围、验收结果与失败历史见[任务 4 验收](docs/design/extension-runtime-governance/acceptance.md)。Schema 仍为 v22；不创建新发行版本或自动更新已安装 App。

## 未发布 · 编排与自动化（2026-10-03）

- 编排画布补齐人工输入、流程审批、等待、固定版本子工作流、工件与合并节点；GUI/TUI 共用正式协议和持久状态。
- 子工作流继承父级权限与冻结配置，按循环和重试预留独立预算，取消及恢复不会重复创建子运行。隔离 Git Writer 经 Action Gateway 执行，显式审查后合并；未知结果保留人工核对。
- 智能创建和修改工作流保留脱敏对话、候选与差异，仍需用户显式保存、校验和发布。
- 本地文件/Git HEAD 变化可触发已发布工作流，保留监控基线、去重和失败诊断；暂停停止领取尚未执行的请求。

本次 Schema 升至 v22。逐项证据和真实模型验收见[任务 3 验收](docs/design/beta-orchestration/acceptance.md)；不创建新发布版本。

## 未发布 · main（核对日期：2026-09-26）

以下变化已经合入主线，但不属于 9 月 14 日的 `v0.1.0-beta.1` 标签：

- **会话工作台**：加入会话内子 Agent、定向后续消息、父子历史树，以及 GUI/TUI 引用、正式命令、上下文压缩和清理。工作台数据使当前 Schema 升至 v19。
- **协作与上下文**：补齐正式编排入口、Agent 节点运行、群聊/定向消息、任务与工件板，以及实际记忆使用检查。
- **记忆治理**：增加任务相关召回、来源/冲突/时效管理、批量审阅和可选后台候选整理。
- **经验复用**：支持经验 Skill 的版本与生命周期、工作区知识晋级、显式跨项目共享及撤销，远程传递保留授权边界。
- **候选交付**：完成 B2-7 范围内的故障/迁移、真实对照与安装验证；内置插件和协议资源纳入 Python 分发。该批数据库为 v18，之后会话工作台增量升至 v19。
- **界面改进**：统一白底深绿图标、文字与控件样式，整理会话、模型及管理页面，减少重复信息和常驻设置。

对应合并：[PR #20](https://github.com/PinelliaChill/Operant/pull/20)、
[PR #21](https://github.com/PinelliaChill/Operant/pull/21)、
[PR #22](https://github.com/PinelliaChill/Operant/pull/22)、
[PR #23](https://github.com/PinelliaChill/Operant/pull/23)、
[PR #24](https://github.com/PinelliaChill/Operant/pull/24)、
[PR #25](https://github.com/PinelliaChill/Operant/pull/25)。

会话式多 Agent 工作台由 [PR #28](https://github.com/PinelliaChill/Operant/pull/28) 合并。
本次文档同步不创建新版本，也不改变已安装 App；正式签名、公证、自动更新、生产远程及性能限制仍保留。

## v0.1.0-beta.1 · 2026-09-14

首个 GitHub Beta 源码预发布，包含多模型角色协作、本地任务与审批、网页/终端/桌面入口、
记忆插件与基础管理，以及开源仓库的安全扫描、CI 和社区模板。

本次修复了测试对开发机临时路径的依赖，并让文档更新走轻量检查。

安装方式、验证范围与已知安全告警见 [完整发布说明](docs/releases/v0.1.0-beta.1.md)。
本版本不包含签名桌面安装包或 B2-4 及后续开发内容。
