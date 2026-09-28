# 前四部分主线联调与验收

> 2026-09-28；记录身份：Codex 主线程；范围：已交付的第一至四部分。基线是 `origin/main@4dd2ec13cb29d142280bda637b833e53825b2996`，源码树与第四部分验证提交 `2fa7038b589cef13b19bfaa3175dd03f02e3fcef` 相同。本次修复在 `codex/four-part-integration`；以下本轮结果仅代表该工作树，未更新已安装 App、真实用户库或公网服务。

## 联调路径与结论

| 路径 | 本轮核对 | 结论 |
| --- | --- | --- |
| 第一部分会话工作台 | 父子 Agent、定向消息、引用/命令、取消与恢复的定向 Python 回归；GUI 相关映射测试 | 通过。合并主线前的真实 GUI/TUI、模型与重启证据见[第一部分记录](../session-workbench/acceptance.md)；本轮未重复原生终端视觉或桌面壳操作。 |
| 第二部分 Graph/Hook | 混合 Graph、持久 Hook 去重/取消、正式 Graph Tool 与 Session 组合回归；隔离正式 `BoundedGraphExecutor.run` 使用精确模型 `gpt-6-luna`，Agent 与文件 Tool 均成功 | 通过。模型发现由当前正式 `operant model discover` 返回该精确 ID；真实场景仅读取临时工作区合成文件，未让模型写入。Hook→Graph 的原 loopback/GUI 证据见[第二部分记录](../orchestration-system/acceptance.md)。 |
| 第三部分配置与审批 | 有效配置/冻结、正式 Session 审批、Graph Tool 权限交叉回归；真实 Graph Agent 的冻结提示词含本轮全局配置 | 发现并修复一处权限漏接，详见下节。Goal/Plan/BTW 原隔离 GUI 与真实模型证据见[第三部分记录](../config-approval-task-control/acceptance.md)。 |
| 第四部分扩展与 Job | 第三方 Tool、浏览器/电脑适配器及持久 Job 的定向回归；GUI 136 项与构建覆盖结果读回。真实 Chrome、macOS 临时 App、正式 Agent Tool 与沙箱的证据绑定于同一基线源码树，见[第四部分记录](../extension-runtime-governance/acceptance.md) | 本轮未改 Worker、Connector、插件包或 Job 协议；这些真实场景复用原验收，不把定向测试冒充新一轮设备验收。 |

## 修复：Graph 动作使用有效工具权限

Graph 的 Tool/Script 节点原先虽然创建了正式 Session，却在构造 `WorkspaceTools` 和扩展 Tool 时使用原始 `RolePreset.tool_policy`。当全局、项目、工作区或角色配置收窄权限时，Agent Session 会遵守收窄后的权限，而同一 Graph 的直接 Tool 节点仍可执行原 Role 允许的动作。隔离回归把 Role 授权设为 `read_file`、全局有效配置设为空白名单，修复前 Graph 仍完成并读到文件，证明漏接。

现在 Tool/Script 先创建 Session，再以冻结的 `RoleSnapshot.tool_policy` 构造内置与扩展工具。原回归由失败变为通过：Graph 终态 `failed`、读取节点 `failed`、无文件内容输出。相同代码路径覆盖 Script 与第四部分扩展 Tool；Action Gateway、审批和 Attempt 边界保持原裁决顺序。另用正式 `gpt-6-luna` 在独立临时库执行允许的 Graph Agent→文件 Tool，终态 `completed`，两个节点均 `succeeded`，并回读 Agent 的有效配置快照。可重跑脚本是 `tests/acceptance/four_part_graph.py`，需要显式模型 ID 和未提交的环境文件。

## 本轮验证状态

- 定向 Python 回归：第一至四部分相关用例通过。启动既有 Docker Desktop 后，受修复影响的 Graph Script 审批测试在 Host 与 `python:3.13-slim` 容器各通过一次；没有把未启用的其他条件用例算作设备验收。
- GUI：`npm --prefix clients/gui test` 为 136/136 通过，`npm --prefix clients/gui run build` 通过（包含类型检查）。
- TUI：复用当前 Textual 依赖，在现有 Python 测试环境执行 `clients/tui/test` 的 18 项测试，通过；没有据此声称原生终端字体已经目视验收。
- 静态：Ruff format/check、mypy、离线锁检查与 `git diff --check` 均按最终源码复核。
- 完整 Python：本机两次全量运行在 12% 左右随执行会话中断，没有最终退出码，不能算通过或失败。当前变更的最终完整基础门禁须按本分支冻结 HEAD 的 PR CI 回读；本机已补齐 CI 以外的 `git diff --check`、真实模型和 Docker 定向链路。运行时使用显式临时 `OPERANT_DB_PATH`，没有打开默认 `.operant` 用户库。

## 剩余缺口与停止边界

- 第一部分的模型执行中强杀恢复、原生终端逐字排版；第二部分的 Human Input/Approval/Wait/Subworkflow/Artifact/Merge 正式节点、文件/Git watcher 与模型建议持久对话；第三部分的 TUI 等效配置/Goal/Plan 管理；第四部分的通用 Command/Event/Provider/Runtime 插件、第三方能力驱动、GUI 直接能力操作与真人浏览器接管，仍按各部分原记录保留。
- 本轮没有重装或实际启动 `/Applications/Operant.app`，没有迁移真实用户库、跨设备部署、生产远程或正式发布。第四部分记录中的旧无标记 Chrome Profile 未在本轮重新盘点，不据此更改原保留判断。
- 新修复目前只在本轮工作分支；未把测试通过写成已合并、已安装或已发布。任务到前四部分联调与文档同步为止，不启动后续 H 项。

## 流程改进

Graph 的 Agent 和直接 Tool 会分别创建 Session。今后跨配置与编排联调应核对两者实际冻结的 `RoleSnapshot`，用一条被配置收窄的 Graph Tool 负例守住权限边界；仅检查 Role 原值会漏掉这种问题。
