---
task_id: UI-REFINE-20260920-1
owner: Agent1
status: implementing
scope: 第一阶段全局视觉基础，不进入第二阶段页面重排
base_head: d8311f39ed4ae797c1f20808ea9a39f66b2dc8ae
code_head: null
governance:
  root: /Users/bigo/agentworkspace/codexworkspace/operant
  version: workflow-20260920.1
implementation:
  worktree: /Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/ui-refine-1
  branch: codex/ui-refine-1
agents:
  - id: Agent1
    role: 全局控件与遗漏样式、集成、实际挂载验收及交付
    session_ref: 当前用户会话
    parent_agent: null
  - id: Agent2
    role: 字体与主题色、空状态呈现
    session_ref: /root/visual_foundation
    parent_agent: Agent1
---

# 目标与边界

User 授权完成统一计划第 9 节的第一次实现任务。保留中性底色、深绿色强调色，
统一字体、层级、基础控件和表单，补齐现有类名缺失的样式。预览只作为方向。
治理及计划入口位于治理根目录；实施树不复制历史治理或凭据。

本次仅呈现变化，不修改 API、业务状态、模型调用、审批、导航或客户端契约。
没有后端/依赖/Schema 改动，不启动真实模型调用或数据库迁移，不安装覆盖用户应用。
现有主目录改动和独立 icon-green 图标分支均保留；本次从实时核对的 origin/main 开始。

## 文件归属与协作

- Agent2：`clients/gui/src/styles/theme.css`、`clients/gui/src/components/EmptyState.tsx`。
- Agent1：通用布局/控件、管理样式、Modal、模型表单呈现及文档。
- 本批为同一工具内子 Agent 协作，没有冒用跨工具待工登记。Agent2 完成源级颜色核对后交权，
  由 Agent1 统一实际挂载下的宽窄屏、主题、焦点与桌面视觉检查，避免并发操作同一窗口。
- 同一 Agent2 在交权后可只读检查集成差异；不把自身代码自检称为独立审查。

## 适用治理摘要

适用根目录 AGENTS.md 的 workflow-20260920.1 与沟通规则改动影响矩阵：
编号身份、限定文件归属、真实验收环境独占、Live 不回退 Mock、纯样式定向验证、普通分支提交授权。
不改变历史记录与权限/数据正确性要求。具体文件摘要与验收记录随交付补齐。

## 验收

| 项目 | 适用范围与依据 | 结果 |
| --- | --- | --- |
| 源码边界 | diff 核对只有样式、呈现属性和文档；不变更事件处理与服务语义 | 待验 |
| GUI 构建 | 当前 package.json 的 build，含 TypeScript 与 bundle 检查 | 待验 |
| GUI 回归 | 当前 GUI 已有测试；不新增镜像 CSS 实现的单测 | 待验 |
| 挂载效果 | 实际 Live 页面、表单、空态；浅深色、宽窄屏、长文本、键盘焦点 | 待验 |
| 桌面 | 当前源码构建的 Tauri 壳；只读连接与页面/表单检查 | 待验 |
| 文档/差异 | 路径、敏感值及 git diff --check | 待验 |

后端完整测试与真实模型验收不在纯呈现变化的适用范围；不将未运行项目标为通过。
总计划第二、三阶段保持未执行，本次仍完成第一阶段自身所需验证。

## 验收环境

待固定构建后登记。持有人 Agent1；实际验收开始时 Agent2 停止写入。
既有用户 Core 不重启、不写数据；新构建单独运行，不覆盖 /Applications/Operant.app。

## 结果

待交付。
