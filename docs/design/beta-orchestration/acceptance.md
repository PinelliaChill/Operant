# Beta 任务 3：编排与自动化验收

> 2026-10-03；记录身份：Codex 主线程。基线 `main@c5b2b778350015010ae8cc5d36f3eec515869443`，实施分支 `codex/beta-orchestration-task3`。对应六次 Beta 完善计划任务 3，沿用 H-03～H-06；历史交付保留。

| ID | 输入与操作 | 预期 | 实现 | 验收与证据 |
| --- | --- | --- | --- | --- |
| BO-01 / H-03 | Live 画布配置节点、连线；保存、校验、确认发布、回读 | Definition/Compiler/Core 裁决，候选不自行发布 | 已实现 | 通过：实际 GUI 应用两轮建议，连接 Agent→Artifact，保存 v3、校验、发布 v4、启动至完成；新增六类节点编辑器逐一挂载并修改字段。`clients/gui/test/live-graph-model.test.ts` 覆盖节点与端口契约；其他组合经正式 SDK 发布并回读 |
| BO-02 / H-04 | Human Input→Approval→Wait→Agent；拒绝、取消、精确 token 与 Core 重启 | 持久等待和决定，不重复执行、不绕过 DENY | 已实现 | 通过：`tests/test_graph_boundary_api.py`；实际 GUI 输入后停在审批，重启 Core 保留相同 approval/token，显式恢复并批准后 Agent 成功；TUI 实际提交输入和审批完成全组合 |
| BO-03 / H-04 | 固定版本 Subworkflow、两轮 Loop、权限/预算/递归边界与恢复 | 子运行独立份额、用量累计、取消/恢复沿同一绑定 | 已实现 | 通过：`tests/test_graph_subworkflow.py` 12 项；真实模型两轮 Loop 创建两个不同 child Run（iteration 0/1），父及两个子 Agent 成功；最终源码在子图等待期间重启 Core，原 child Run/token 恢复，父子各一次 Agent attempt。父运行等待子任务关停与恢复两个用例连续 25 轮通过；配置漂移、递归/深度/子 Agent 超限在 Provider 前拒绝 |
| BO-04 / H-04 | 工件发布、隔离 Git Script Writer×2→Merge；DENY/ASK、冲突、未知结果和跨 Run 工件 | 完整性与归属可核对，副作用经 Gateway，未知结果不重放 | 已实现 | 通过：`tests/test_graph_artifacts.py` 5 项；`tests/test_graph_typed_writer.py` 4 项在真实临时 Git worktree 生成提交、经正式 HTTP 创建/完成 Merge、目标两文件可读；Phase 6 套件覆盖审查、冲突和三入口跨 Run/错节点拒绝。该 Git 验收前置 Agent 用确定性 Provider，未声称 Writer 使用真实模型 |
| BO-05 / H-05 | 正式模型创建→继续修改→重启读对话；应用候选并手动发布 | 脱敏持久历史参与后续请求，差异/确认可见 | 已实现 | 通过：`tests/test_workflow_assistant_api.py`；真实 `gpt-6-luna` 两轮候选，名称由 initial 改为 revised、历史 2 轮；建议时 Definition 不存在，显式保存才创建；Core 重启后历史仍 2 轮，GUI 可选择并应用 |
| BO-06 / H-06 | 绝对临时工作区内真实文件变化/Git commit→正式 Graph Agent | 持久基线、稳定事件 ID、单一请求，终态可读 | 已实现 | 通过：`tests/test_scheduler_watch.py`；真实文件修改和真实 Git HEAD 变化各产生一个成功请求，Graph Agent 经正式 Profile 返回 TASK3_OK；GUI 显示两类 Watcher 的 generation 1 及已完成队列 |
| BO-07 / H-06 | 重复观察、暂停/恢复、取消、重启、慢探测、失效租约和非法路径 | 未执行队列暂停不可领取，去重/错误有持久证据 | 已实现 | 通过：Watcher 7 项及既有 Scheduler 套件；暂停前排队的请求不能领取，恢复仅领取同一请求，取消清掉后续排队；保留基线，重启可观察暂停期间变化；符号链接失败不入队、旧租约不能提交，慢探测不阻塞 API。内部维护任务使用无自动计时 occurrence 的 enabled application.signal Hook，单次手动请求与真实模型 Graph 均成功 |
| BO-08 / H-03～06 | GUI 宽窄屏/键盘/断线，TUI 边界操作，协议与门禁 | 共用正式协议，网络错误显式，适用门禁通过 | 已实现 | 本地通过：GUI 140 项、类型与构建；TUI 31 项及实际挂载的真实 HTTP/model 组合；1440×1000、390×844 页面无横向溢出，键盘焦点可达，离线显示提示和 Failed to fetch。协议单一 Schema 重生成；Ruff/mypy/锁/diff 通过。最终完整跨版本门禁以本分支 PR 的 quality (3.10)、quality (3.12)、GUI 检查为准 |

实际入口为 loopback Core、固定构建 GUI、正式 Phase23/Phase45 生成客户端与 Textual TUI。TUI 使用真实挂载的 `run_test` 窗口操作控件，Core/Provider 未 Mock；未验证原生终端或 Tauri 系统能力。本任务未改变这些系统能力，历史 Docker 条件跳过不计为容器验收。

真实模型先运行正式 `operant model discover`，使用发现的精确 `gpt-6-luna` 与正式 ModelProfile。全部运行固定隔离 SQLite 和 `/private/tmp` 绝对工作区，凭据仅注入 Core 进程。未迁移真实用户库、更新已安装 App、部署或创建正式版本。

可复跑自动测试位于上表路径；真实 HTTP/model/watch 入口为 `tests/acceptance/beta_orchestration.py`，需显式 opt-in 与隔离 seed。原始本机证据在忽略的 `.operant/beta-task3/evidence/`：`real-http/` 包含候选、历史、Definition、节点、审批、队列和模型绑定；同目录截图和断线/编辑器文字记录保留实际挂载状态。原始数据库与运行日志不入 Git。

关键真实 Run：GUI Agent/Artifact `graph_run_4e90d41b75f24c6499d9e8bfb4fe9bbd`；TUI 全组合 `graph_run_b8139ca256a7442fb19878f39b1b88cb`；真实子工作流循环 `graph_run_aa017c9515b34b1994fa40232d5dde77`。三者均回读 COMPLETED。

CI 修复后的真实 Run：子图等待跨 Core 重启的父运行 `graph_run_53659170921f47e9b62c550d110ca630` 与原 child `graph_run_21549977618d27313e747b5c0263f230` 均 COMPLETED，沿用原等待 token，父子 Agent 各一次成功 attempt。内部维护 Hook 的 `scheduler_e20df2219e78fa7a0ca1ab29abfd80a2a5c586355bd399e79252af683f06bd28` COMPLETED，维护 job/request SUCCEEDED、仅一次 attempt；使用符合发现能力的 LOW Role 与同一 `gpt-6-luna` Profile。

保留初始失败：首个模型候选使用错误 NodeSpec 字段，被正式校验拒绝且未保存，随后明确生成契约并复验成功；初次全组合最终 Artifact 因相同内容的媒体类型与已有工件不一致被拒绝，统一显式 `text/plain` 后新 Run 通过，未覆盖旧失败。最后补测发现并修复 Loop 复用首轮 child、暂停队列仍派发，以及 Merge 跨 Run 归属缺口，各有回归用例。

初始完整本地 pytest 为 1245 passed、11 skipped、5 failed：迁移测试硬编码未来版本 22 与新增 v22 冲突，已改为当前最大版本+1，整文件 25 项复验通过；另 4 项为沙箱禁止 loopback bind，获准执行后 4/4 通过。最终源码另通过受影响编排、Scheduler、Phase 6 与协议集；同一最终提交的完整 CI 用于最终门禁，不复用初始失败结果。

首轮 PR #36 的完整 CI run `37126631284`：GUI 和 CodeQL 通过；Python 3.12 为 9 failed/1246 passed/13 skipped，Python 3.10 为 10 failed/1245 passed/13 skipped。9 个维护用例暴露内部 paused Timer 被正确的暂停保护拦截，已改为单次手动入队的内部 Hook；另一个恢复用例暴露父运行未等子任务关停，已补 drain 与确定性延迟取消测试。相关 Scheduler/维护和 Graph 组合全通过，本机 Python 3.10 依赖不足，跨版本结果仍由修复提交 CI 核对。旧失败日志保留于 `ci-pr36/`。

最终生产输入清单保存在 `source-freeze.json`，500 个文件按路径排序的紧凑 JSON 聚合 SHA-256 为 `cd3350beed3c0bb2272e530261606a3881f63f25a685b39d77703e0396008785`。首轮 PR 清单保留为 `source-freeze-pr36-initial.json`（原聚合 `88d796ee86997847bcbb2c25821bbf06a6fe0e2903e9b7d553f444f1f9eebc32`）。本轮仅 Graph 子任务关停与内部维护调度两处生产文件变化，均已补真实模型验收；此前循环、队列、Merge 已复验，未受影响的建议历史、GUI 构建和触发观察证据沿用。

推送、最终 CI 与 main 合并以 GitHub PR 和远端回读为准；本文件不以本地阶段通过代替受保护合并。
