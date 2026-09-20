---
task_id: UI-REFINE-20260920-2-3
owner: Agent1
status: implementing
base_head: adda6bb31d20d065c4237d962db9d9bbcf190bf9
code_head: null
governance:
  root: /Users/bigo/agentworkspace/codexworkspace/operant
  version: workflow-20260920.1
implementation:
  worktree: /Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/ui-refine-2-3
  branch: codex/ui-refine-2-3
agents:
  - id: Agent1
    role: 聊天/壳层、集成、隔离Core与真实桌面验收、交付
    session_ref: 当前用户会话
    parent_agent: null
  - id: Agent2
    role: 管理与技能页视觉
    session_ref: /root/visual_foundation
    parent_agent: Agent1
  - id: Agent4
    role: 模型与角色页视觉
    session_ref: /root/model_page
    parent_agent: Agent1
  - id: Agent3
    role: 独立审查，冻结后恢复原会话
    session_ref: /root/review_foundation
    parent_agent: Agent1
---

# 目标与边界

User 已授权完成统一计划第9节第二、第三阶段：关键页面优化与集中验收。
延续第一阶段，保留中性底色、深绿色、清晰中文排版；设计可根据真实内容调整。
不删功能，不改服务端状态权威、正式模型/项目/Thread/Session绑定、审批及未知写保护。
不合并主线、不覆盖已安装应用、不部署、不迁移真实用户库。

## 文件归属

- Agent1：LiveChatView、LiveSidebar、RailLayout、聊天/壳层专用样式、呈现决策测试、文档及验收。
- Agent2：LiveManagementView.tsx、新 ui-refine-management.css。
- Agent4：LiveAgentsView.tsx、新 ui-refine-agents.css。
- 不同执行者不改共享样式；既有第一阶段 tokens 直接复用。Agent2/4 源码自检后交权，
  Agent1统一窗口挂载自检，避免并发操作桌面；这项分工不把构建当成视觉验收。

## 验收矩阵

| 范围 | 检查 | 状态 |
| --- | --- | --- |
| 客户端 | 当前GUI测试、typecheck、build、diff检查；协议/依赖不变 | 待验 |
| 聊天 | 正式创建Thread/Session、真实模型发送与历史、取消/审批原守卫；未知结果与错误不隐藏 | 待验 |
| 配置/技能 | 有数据与空态呈现、表单/折叠、项目范围；原命令/权限/确认保留 | 待验 |
| 桌面 | 冻结构建+隔离Core/合成库，宽窄屏、明暗主题、长文本/焦点、错误与断线 | 待验 |
| 审查与文档 | Agent3独立审查，当前实现/目标设计必要更新，计划与进度回写 | 待验 |

后端源码、Schema、SDK与锁文件无计划改动，按影响矩阵复用其既有证据。
不以样式后缀豁免受影响真实模型链路。模型发现先行，凭据仅进程注入；受控调用使用独立绝对workspace与有限预算。

## 验收环境

Agent1持有；未冻结前不标记验收通过。使用 `/tmp/operant-ui23-accept` 合成库和workspace，
新Core端口18002，经本地静态预览端口3002供独立Tauri Debug壳使用；用户8000服务只做健康读取、不重启。
冻结时所有源码写入停止，故障测试只停止自建Core。每段记录实际源码/构建摘要与限制。
