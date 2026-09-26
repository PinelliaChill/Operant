# 可操作的编排系统验收记录

> 2026-09-26；记录身份：Codex 主线程。验收源码为 `codex/orchestration-system-work`（基于 `cf25546`）的交付增量。真实验收只使用 loopback Core、临时 SQLite 和 `/private/tmp/operant-orchestration-ui-workspace`，未改真实用户库或已安装桌面 App。

| 范围 | 结果与证据 |
| --- | --- |
| H-03 Live 画布 | 在正式 GUI 选择 Core 的 Workflow 草稿，编辑节点并编译；Core 返回 valid，发布为不可变 v2 后回读；启动 Graph Run，页面显示 Agent 节点完成和 Core Cursor。画布模型、连线、节点参数、版本差异及运行状态的定向 GUI 测试通过。宽屏及 430px 视口挂载检查通过；430px 页面无横向文档溢出。 |
| H-04 混合 Graph | 定向测试覆盖 Agent、Tool、Script、Condition、Fan-out、Join、Loop、Timer，分支跳过、Loop 上限、审批后的 Host Script、取消/中断后的未知副作用人工核对和不重放、Agent 新身份恢复、Timer 截止时间恢复。正式 GUI 的 Agent Graph 由 `gpt-6-luna` 完成；Script 与混合分支的真实执行证据是受控工具/测试，不把它称作真实模型组合演练。 |
| H-05 模型建议 | 正式 Model Discovery 找到精确 ID `gpt-6-luna`。用已配置 ModelProfile 调用 `/v1/graph/workflows/suggest`，返回经 Compiler 校验的候选；正式 GUI 基于已发布 v2 生成 `Reviewer QA` 修改建议，显示差异、写入权限和预算。应用后 Core 保存[草稿 v3](/private/tmp/operant-orchestration-suggested-v3.png)，回读核对只改变名称，节点与冻结角色保持一致；另一轮建议应用后画布仅显示名称未保存，不再误报节点修改。候选不会自行保存、发布或运行。一次生成与基于已保存版本的继续修改已实现，尚无持久对话历史。 |
| H-06 应用 Hook 与真实 Agent | 正式 GUI 创建 `application.signal` 调度，绑定已发布 Workflow v2 和隔离绝对工作区；事件 `qa-hook-20260926-1` 两次发送仅有一个 RunRequest `request_636d42ee62f944869879017370aed602`。Core 回读请求 `succeeded`，对应 `graph_run_scheduler_f9ad591ee87b1b744c0a582cfee3572d1f27e23e6c66b69ba193d39c2297c76e` 为 `completed`，Agent 节点 `reviewer` 为 `succeeded`，有真实 AgentInstance/Attempt。页面截图：[宽屏完成状态](/private/tmp/operant-orchestration-hook-ui.png)、[430px 窄屏](/private/tmp/operant-orchestration-hook-narrow.png)。取消、暂停、去重、重启接管、非法工作区与远端来源拒绝由 `tests/test_h06_scheduler_hook.py` 验证。 |
| 协议与安全 | Phase23/Phase45 从单一 Schema 生成 Python/TypeScript Client 和 digest；生成器确定性由协议测试核对。Tool/Script 经冻结 Role、Tool Policy、Action Gateway 与审批执行，未知写入不自动重放。智能建议先脱敏再传给模型，并拒绝不符合 Schema/Compiler 的结果。 |

首次门禁：`uv run pytest -q` 退出码 0，1 项 Docker 环境跳过，1 条 Starlette/httpx 弃用提示；`ruff format --check`、`ruff check`、`mypy src`（139 个源文件）、`uv lock --check --offline`、`git diff --check` 通过。GUI 135 项测试、`npm run typecheck` 与 `npm run build` 通过。Phase23/Phase45 协议生成一致性测试通过。所有 Python 检查使用隔离 `OPERANT_DB_PATH`，避免触及用户运行库。

首次验收时 Docker daemon 不可用，容器 Script 未验收；后续补验结果见下节。Human Input、Approval、Wait、Subworkflow、Artifact、Merge 未纳入本次正式执行器；Hook 仅支持本地 `application.signal`，没有文件/Git watcher。Tauri 原生壳、已安装 App、公网与真实用户库均未在本次验收。验收后 loopback Core/GUI 服务已停止，保留隔离临时库与截图。

验收中曾发现模型建议返回的省略默认字段让画布误报节点修改，以及版本下拉框按 Enter 会触发保存。已分别改为返回完整 Definition 并显式点击保存；浏览器复验后，建议应用仅显示名称变化、Enter 不再自动保存。误触发生成的 v4 只留在本次隔离临时库。

## 2026-09-26 补验：独立仿真 Review 与 Docker Script

独立验收 Agent 只读复核 `2bbd141`：原版定向 Python 33 项、GUI 23 项通过；额外用临时库模拟 Hook 已派发后 Provider 阻塞时取消，RunRequest 和 Graph 都为 `cancelled`、Provider 只调用 1 次，同一事件 ID 仍复用原请求。Review 发现画布删除节点后可能保留悬空连线起点、模型建议生成期间切换 Team/版本可显示旧候选；主线程修复后，独立 Agent 复验 GUI 24 项、类型检查和 diff 检查通过。修复内容分别为连线前验证源节点/端口并清理临时起点，以及按 Team/基础版本与请求序号丢弃过期建议，应用前再次核对。主线程另在正式 GUI 实际创建 A/B 节点、选 A 输出、删除 A、点 B 输入，Core 画布差异仍为 0 条连线；生成建议期间切换 v2→v3，页面清除 pending 且没有显示旧候选。此浏览器检查只使用隔离库，截图见[复验页面](/private/tmp/operant-orchestration-review-ui.png)。

真实 Docker 补验前，Docker Desktop 4.87.0 因 `Docker.raw` 是 `root:staff`、权限 644 而无法调整虚拟磁盘大小；User 明确授权后只把该文件所有者改回 `bigo:staff`，未重置数据。原缓存 `python:3.13-slim` 的容器执行报 ELF 格式错误，镜像内 `libc` 前 64 字节为零；`hello-world` 可运行，证明 Engine 本身可用。用独立空 `DOCKER_CONFIG` 避开原凭据助手的阻塞，匿名拉取官方 arm64 `busybox:1.37` 和更新后的 `python:3.13-slim`；后者单独运行 `python -c` 成功。原失败保留为环境与镜像诊断，不计入 Operant 通过结果。

Graph Docker Script 首次真实运行暴露出 H-04 缺陷：Policy ALLOW、Role 的 ToolPolicy 仍要求 shell 审批时，Graph 在动作开始后才收到 `ApprovalRequired`，把尝试记成未知失败。现已在动作副作用开始前合并 ToolPolicy 的分类审批要求；Action Gateway 仍先裁决 DENY，审批继续绑定精确 Action Hash/Attempt。Host、BusyBox 和项目默认 Python 镜像的 Graph Script 定向测试通过；脚本在容器快照写入 `container-marker.txt`，宿主临时工作区没有该文件，`runner=docker`、退出码 0。整组 H-04 测试在 Docker 可用时 22 项通过，`docker ps -a` 未留下测试容器。

补验最终门禁：隔离 `OPERANT_DB_PATH`、显式 `OPERANT_DOCKER_TEST_IMAGE=python:3.13-slim` 下完整 `uv run pytest -q` 退出码 0，进度记录 1115 项通过、0 跳过，仍有 1 条 Starlette/httpx 弃用提示；日志与退出码分别在 `/private/tmp/operant-orchestration-docker-full-recheck.log`、`.exit`。GUI 136 项、类型检查、生产构建，`ruff format --check`、`ruff check`、`mypy src`（139 个源文件）、`uv lock --check --offline`、`git diff --check` 通过。独立 Agent 对新增分类审批前置做只读安全复审，确认 DENY 先行、审批与同一 Agent/Session/Receipt/Action Hash 绑定，未发现新绕过。
