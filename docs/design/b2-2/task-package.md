# B2-2 基础任务与 Host

- 唯一负责人：Codex；记录身份：Codex；适用对象：本批 Codex 子 Agent、Antigravity、Reviewer。
- 授权：仅 B2-2；不重做 B2-1，不进入 B2-3/MP-2，不推送、合并、部署或迁移用户库。
- 治理根：`/Users/bigo/agentworkspace/codexworkspace/operant`；根 AGENTS、沟通规则 `workflow-20260910.3`（2026-09-10）。入口按需读，不复制历史。
- 实施：`/private/tmp/operant-b2-2`，`codex/b2-2-tasks-host`，基线 `8851a237cc961afa8649cffe57d407472838887f`；新建时干净。治理根旧分支/未提交客户端与设计保留。
- 范围来源：治理根 `docs/design/Operant-Beta-2.0更新计划.md` §4 B2-2、§6 无插件基础任务/信任隔离；`docs/design/记忆系统设计草案.md` §13.4 MP-1.1～1.5。
- 前置：`src/operant/contracts/b2_1.py`、B2-1 handoff；旧协议冻结，additive 扩展由负责人持有。

## 归属与交接

| 路径 | 唯一写入负责人 | 交还 |
| --- | --- | --- |
| `src/operant/plugins/`、`tests/test_plugin_host*.py`、`examples/plugins/` | Codex（已接受 host 归属交回） | Codex |
| `clients/gui/` 除下列视觉 CSS；相关 GUI 定向测试 | Codex（已接受 gui 归属交回） | Codex |
| `clients/gui/src/features/tasks/b2-task-visual.css` | Codex（已接受 Antigravity a0a4a5d 归属交回） | Codex |
| `tests/test_b2_2_api.py`、`tests/test_b2_2_protocol_sdk.py` | Codex（已接受 api_tests 归属交回） | Codex |
| 公共 Schema/生成 SDK、API、Service/持久化集成、架构与证据 | Codex 负责人 | 最终交付 |

子 Agent 不改公共契约；缺口发字段/方法最小建议给 Codex。共用隔离实施树，限负责路径，不提交他人文件。提交时明确停止写入与归属交回。Reviewer 实现后启动 `gpt-5.6-luna/max`，当前工具无 Fast 开关，不声称 Fast。

## 验收表

| ID | 要求与准确来源 | 负责人 | 方法/环境/预算 | 阻断及理由 | 证据 |
| --- | --- | --- | --- | --- | --- |
| AC-01 | 安装/Registry/配置/资源；设计 §13.4 MP-1.1 | Codex | 隔离临时目录、定向故障测试；禁用零实例/不自动重装 | 是，MP-1 | 定向安装/绑定/private_index/restart测试通过；最终候选门禁状态见verification.json |
| AC-02 | 进程内/stdio 同一 API，复用/批量/取消/日志资源界限；MP-1.2 | Codex | 两种实际执行模式，有限超时与预算 | 是，Host 完成门 | 实际entrypoint进程内与macOS隔离stdio RPC通过；并发/RSS/CPU/idle定向通过 |
| AC-03 | 包/依赖/权限认证及撤销；MP-1.3、计划 §6 | Codex | 篡改/扩大权限/撤销，真实 macOS sandbox 拒绝受控 Home 探针与网络；不可用 fail closed | 是，安全门 | 真实macOS隔离探针及认证篡改/撤销/过期/issuer测试通过；Docker不算通过 |
| AC-04 | epoch/迟到提交/启动停止失败/重启/Run 依赖；MP-1.4 | Codex | 定向并发和故障注入 | 是，安全与恢复 | lease/scope/epoch/迟到结果/重启定向通过；最终候选门禁状态见verification.json |
| AC-05 | Host 资源 keep/delete/续做/残留，缺 Hook 可清理；MP-1.5 | Codex | 临时库/目录重启与中断测试 | 是，资源基础 | Host inventory无Hook删除、keep保留、清理中断续做定向通过 |
| AC-06 | 配置、任务发起/历史/详情/审批取消及恢复入口，受支持项目身份；计划 §4 GUI-L1、治理根客户端规范 §16.1 GUI-L1 与逐模块验收1～5 | Codex | 正式 API/生成 SDK + GUI 行为定向测试 | 是，任务闭环 | 正式API/SDK及Factory失败回归；原生Thread/Session/角色/审批/取消/历史闭环见client-acceptance.md |
| AC-07 | 无插件真实桌面任务/工具/历史/合法恢复；强依赖记忆 Graph 拒绝；计划 §6、AGENTS 真实模型 | Codex | 先 discover 精确模型，正式 ModelProfile/入口；隔离绝对 workspace，单个调用有限预算/120s，失败按规则停止 | 是，真实链路 | 真实gpt-5.6-luna/low、read_file与审批cat结果42；明确新轮继续；强依赖Graph定向拒绝 |
| AC-08 | 宽窄屏/键盘/对比度/错误断线/Action Gateway/实际 Tauri；AGENTS 客户端边界 | Codex | 本机 debug Tauri 与 UI，检查本批新增流程；不覆盖发布签名 | 是，客户端门 | 真实原生约1200/768宽，分页105条、错误断线/重连、重启回读、表单焦点/保存已观察 |
| AC-09 | 完整基础门禁；AGENTS 开发与验证 | Codex | ruff format/check、mypy src、pytest、uv lock --check --offline、git diff --check；集成版本一次 | 是，硬门禁 | 518bb3f完整门禁通过：847 pass/1 Docker skip；见verification.json |
| AC-10 | GUI/SDK test/typecheck/build、协议确定性；AGENTS | Codex | 当前 package scripts + SDK 定向测试/生成回读，旧 Schema 不变 | 是，契约门 | GUI91定向/类型检查通过；最终构建与SDK确定性已随完整门禁通过 |
| AC-11 | 架构同步、保留范围、独立 Reviewer 与进度；AGENTS/用户请求 | Codex | diff/证据覆盖审查，Luna max，同 Reviewer 增量 | 是，交付门 | 架构/客户端记录已同步；原Luna/max已通过caa2ba8 Session/B2/GUI切片，Host最后并发/配置增量因Reviewer额度失败待恢复，见review.md |

## 环境与证据规则

治理根与实施树分别核对；旧根工作区不改。依赖使用已核对的本树.venv（uv --no-sync），锁/解释器版本不改。Docker不可用的skip不算容器验收；本批实际隔离为macOS sandbox-exec。Core18000/Vite3000与独立debug Tauri仅用本批临时SQLite和绝对Workspace，凭据只在子进程注入，用户8000不触碰。

子任务只做定向检查，源码冻结后完整门禁一次；新增修复须覆盖最终代码。源码/依赖/环境未变的昂贵证据按指纹复用，Reviewer只审增量。运行/资源请求超时不自动重放；未收束可信代码返回restart_required并保留状态。

## 交接与当前候选

- Host、GUI、API工程子Agent已交还；Antigravity实名交付a0a4a5d并释放CSS，Codex按真实窄屏结果补修，当前无新视觉工单，条件静默。
- 审查修复子任务lease_fix与payload_fix已交还；负责人集成并验证生产授权入口、实际隔离、失败历史与取消。代码候选518bb3f；细节见host-review-increment.md、client-review-increment.json、review.md。
- 当前临时app/Core/Vite均已关闭，见cleanup.json。B2-3未授权。交付结果只写handoff.md和verification.json；总进度在治理根memory/current.md。
- Task统一resume未提供虚构接口；Session显式新轮和既有Workflow Graph安全恢复入口按客户端规范§16.1验收，不能把禁用按钮当作恢复验收。任务页首个API页与完整列表分页的限制在handoff.md明确。
