# 更新记录

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
