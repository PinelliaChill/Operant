---
task_id: UI-REFINE-20260920-2-3
owner: Agent1
status: completed
base_head: adda6bb31d20d065c4237d962db9d9bbcf190bf9
code_head: 252f2a585297d4538dcc96366803e0151db0e344
delivery_head: null
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
| 客户端 | 当前GUI测试、typecheck、build、diff检查；协议/依赖不变 | 见验收记录 |
| 聊天 | 正式创建Thread/Session、真实模型发送与历史、取消/审批原守卫；未知结果与错误不隐藏 | 见验收记录 |
| 配置/技能 | 有数据与空态呈现、表单/折叠、项目范围；原命令/权限/确认保留 | 见验收记录 |
| 桌面 | 冻结构建+隔离Core/合成库，宽窄屏、明暗主题、长文本/焦点、错误与断线 | 见验收记录 |
| 审查与文档 | Agent3独立审查，当前实现/目标设计必要更新，计划与进度回写 | 见验收记录 |

后端源码、Schema、SDK与锁文件无计划改动，按影响矩阵复用其既有证据。
不以样式后缀豁免受影响真实模型链路。模型发现先行，凭据仅进程注入；受控调用使用独立绝对workspace与有限预算。

## 验收环境

Agent1持有；未冻结前不标记验收通过。使用 `/tmp/operant-ui23-accept` 合成库和workspace，
新Core端口18002，经本地静态预览端口3002供独立Tauri Debug壳使用；用户8000服务只做健康读取、不重启。
冻结时所有源码写入停止，故障测试只停止自建Core。每段记录实际源码/构建摘要与限制。


## 交接与适用规则

Agent2 已明确交付管理/技能页并释放写权。Agent4 的模型页改动已落盘，但其会话额度中断后没有收到
最终交接回执；确认执行者停止后由 Agent1 接管，修复 portal 样式作用域并独立验收，不代签 Agent4。
本批实际挂载视觉检查集中由 Agent1 完成，Agent2/4 的静态检查不计作各自原生自检。
Agent3 对冻结源码作独立只读审查，最终结论见[验收记录](acceptance.md)。

本批治理摘要（SHA-256）：

- AGENTS.md：`ccff99b56d0ae1123de50505ae477a793b56137e31c193b308b2109a3f26d8c3`
- MEMORY.md：`4252f5671d9600889f4af4bb3f375162fcfd2e99cd06054da85164c4c4979aa1`
- memory/communication/README.md：`9a435ed73015738a93611b390fe95c2f4fcc6823b080d47fbed139e930c004d2`

目标客户端规范已核对：延续系统字体、语义表单、焦点与服务端状态权威；本批未改变目标协议或产品边界，
因此不另改目标规范。最终文档提交号由 Git 回读，不在提交内部写自引用 SHA。
