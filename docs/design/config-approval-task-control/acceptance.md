# 配置、审批与任务控制验收记录

> 2026-09-26；记录身份：Codex 主线程。源码分支 `codex/config-approval-task-control` 基于 `47ebe36`。本记录只覆盖第三部分 H-08/H-09/H-12；隔离 Core 与 GUI 使用 loopback 端口 18080/13000、临时库 `/private/tmp/operant-phase3-acceptance/core.sqlite3` 和绝对临时工作区，未迁移真实用户库、更新已安装 App 或部署公网。

| 范围 | 实际验证 |
| --- | --- |
| H-08 配置继承 | Live 设置页选择正式 `role_main` 后显示 Core 返回的有效值、逐字段来源、四层 scope 和修订号；通过 GUI 保存全局提示词，Core 回读 revision 1。随后在审批页保存 Reviewer 设置，回读全局 revision 2 且两项覆盖并存。旧 Session `session_932348cf23bf4e87af57bb9d1639d9df` 的冻结提示词未变化；新 Session `session_61214597d0364212aa1b7c13f53df9ee` 包含新提示词，来源为 `global:default`。定向测试覆盖全局→项目/工作区→角色→Run、预算/权限收窄、温度能力、CAS 重置与新旧快照；还验证普通 Live 的 Thread 推导已注册 Project、Workflow 传入工作区，以及 Run 预算不能放宽继承限额。 |
| H-09 审批 | Live 审批页可设置关闭/人工/自动、独立模型 Profile、严格度、自定义规则，显示 ASK 审计和人工回退。真实 GUI 将人工模式与一条仿真规则保存到隔离 Core。[宽屏](approval-live.png)和[390px 窄屏](approval-narrow.png)均已实际挂载检查。定向测试用隔离 Provider 验证 Phase45 与正式 Session 的 ASK 自动触发、Pending→决定、审计、重复请求、MCP 路径、模型失败留待人工及硬 DENY 先行。真实 `gpt-6-luna` 先通过 `ApprovalModelReviewer.review` 对未执行的仿真 ASK 给出 `deny`；随后从正式 `ApplicationService.run_session` 入口复验：隔离主 Agent 提出临时 Git 工作区的 staging 动作，真实审批模型给出 `deny`，持久记录 `decided_by=reviewer`、无需人工回退，Agent 正常完成且动作未执行。第一次复验因代理连接失败未到达 Discovery，第二次成功；两次均未触及真实项目文件。 |
| H-12 Goal/Plan/BTW | Live 任务页通过正式接口创建 `goal_8154bae6f5834efab12a5699712ac4d2`、`plan_a46e23bd532e4148847a1ce8f77bb13c`、清单项 `check_73cb13b154eb4c90b4523ff28e1d4b6b`，SQLite 回读各一条。Goal/Plan revision CAS、清单前置/证据、只读 Planner Sidecar 和 BTW 显式提升有定向测试。Discovery 返回精确模型 ID `gpt-6-luna`；正式 ModelProfile 在绝对隔离工作区调用 `generate_plan_draft/BTW Sidecar`，零工具生成状态为 `draft` 的 `plan_726da2965bdd495da82600d1bf600537`，没有自动执行或发布。 |
| 默认能力包 | 从本机可信根实际发现并在独立临时项目安装、启用六项：`grill-me`、`documents`、`Presentations`、`pdf`、`skill-creator`、`find-skills`。安装与项目绑定回读数量均为 6；`grill-me` 的 manual-only 标记使其不被自动注入。安装验证不等于六种任务的内容质量或其外部依赖都已验收。 |
| 协议与迁移 | SQLite v20 增加 `scope_configs`、Goal、Plan、清单表，旧迁移冻结 checksum 不变。B2、Phase45、Workbench 与新增 `phase3.v1` 从 Core OpenAPI 重生成；Python/TypeScript 生成物与 digest 随分支提交。 |

真实模型场景仅核对本部分受影响的 Plan 与审批链；没有对模型结论作正确率评估。审批模型默认人工，自动模式的模型失败或进程重启保留待审并可人工决定；重试精确动作会重新触发审核。默认 Skill 包要求管理员配置可信根，并不把主机上的密钥或源目录复制进仓库。TUI 尚无本部分的等效配置/Goal/Plan 管理页；Tauri 原生壳、生产远端、真实用户库和正式签名发布不属于本次验收。
