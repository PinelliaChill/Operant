# 可操作的编排系统验收记录

> 2026-09-26；记录身份：Codex 主线程。源码为 `codex/orchestration-system-work`（基于 `cf25546`）的未合并实施树。真实验收只使用 loopback Core、临时 SQLite 和 `/private/tmp/operant-orchestration-ui-workspace`，未改真实用户库或已安装桌面 App。

| 范围 | 结果与证据 |
| --- | --- |
| H-03 Live 画布 | 在正式 GUI 选择 Core 的 Workflow 草稿，编辑节点并编译；Core 返回 valid，发布为不可变 v2 后回读；启动 Graph Run，页面显示 Agent 节点完成和 Core Cursor。画布模型、连线、节点参数、版本差异及运行状态的定向 GUI 测试通过。宽屏及 430px 视口挂载检查通过；430px 页面无横向文档溢出。 |
| H-04 混合 Graph | 定向测试覆盖 Agent、Tool、Script、Condition、Fan-out、Join、Loop、Timer，分支跳过、Loop 上限、审批后的 Host Script、取消/中断后的未知副作用人工核对和不重放、Agent 新身份恢复、Timer 截止时间恢复。正式 GUI 的 Agent Graph 由 `gpt-6-luna` 完成；Script 与混合分支的真实执行证据是受控工具/测试，不把它称作真实模型组合演练。 |
| H-05 模型建议 | 正式 Model Discovery 找到精确 ID `gpt-6-luna`。用已配置 ModelProfile 调用 `/v1/graph/workflows/suggest`，返回经 Compiler 校验的候选；正式 GUI 基于已发布 v2 生成 `Reviewer QA` 修改建议，显示差异、写入权限和预算。应用后 Core 保存[草稿 v3](/private/tmp/operant-orchestration-suggested-v3.png)，回读核对只改变名称，节点与冻结角色保持一致；另一轮建议应用后画布仅显示名称未保存，不再误报节点修改。候选不会自行保存、发布或运行。一次生成与基于已保存版本的继续修改已实现，尚无持久对话历史。 |
| H-06 应用 Hook 与真实 Agent | 正式 GUI 创建 `application.signal` 调度，绑定已发布 Workflow v2 和隔离绝对工作区；事件 `qa-hook-20260926-1` 两次发送仅有一个 RunRequest `request_636d42ee62f944869879017370aed602`。Core 回读请求 `succeeded`，对应 `graph_run_scheduler_f9ad591ee87b1b744c0a582cfee3572d1f27e23e6c66b69ba193d39c2297c76e` 为 `completed`，Agent 节点 `reviewer` 为 `succeeded`，有真实 AgentInstance/Attempt。页面截图：[宽屏完成状态](/private/tmp/operant-orchestration-hook-ui.png)、[430px 窄屏](/private/tmp/operant-orchestration-hook-narrow.png)。取消、暂停、去重、重启接管、非法工作区与远端来源拒绝由 `tests/test_h06_scheduler_hook.py` 验证。 |
| 协议与安全 | Phase23/Phase45 从单一 Schema 生成 Python/TypeScript Client 和 digest；生成器确定性由协议测试核对。Tool/Script 经冻结 Role、Tool Policy、Action Gateway 与审批执行，未知写入不自动重放。智能建议先脱敏再传给模型，并拒绝不符合 Schema/Compiler 的结果。 |

最终门禁：`uv run pytest -q` 退出码 0，1 项 Docker 环境跳过，1 条 Starlette/httpx 弃用提示；`ruff format --check`、`ruff check`、`mypy src`（139 个源文件）、`uv lock --check --offline`、`git diff --check` 通过。GUI 135 项测试、`npm run typecheck` 与 `npm run build` 通过。Phase23/Phase45 协议生成一致性测试通过。所有 Python 检查使用隔离 `OPERANT_DB_PATH`，避免触及用户运行库。

Docker daemon 不可用，容器 Script 未验收；Human Input、Approval、Wait、Subworkflow、Artifact、Merge 未纳入本次正式执行器；Hook 仅支持本地 `application.signal`，没有文件/Git watcher。Tauri 原生壳、已安装 App、公网与真实用户库均未在本次验收。验收后 loopback Core/GUI 服务已停止，保留隔离临时库与截图。

验收中曾发现模型建议返回的省略默认字段让画布误报节点修改，以及版本下拉框按 Enter 会触发保存。已分别改为返回完整 Definition 并显式点击保存；浏览器复验后，建议应用仅显示名称变化、Enter 不再自动保存。误触发生成的 v4 只留在本次隔离临时库。
