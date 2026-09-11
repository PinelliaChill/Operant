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
| AC-01 | 安装/Registry/配置/资源；设计 §13.4 MP-1.1 | Codex | 隔离临时目录、定向故障测试；禁用零实例/不自动重装 | 是，MP-1 | 待验 |
| AC-02 | 进程内/stdio 同一 API，复用/批量/取消/日志资源界限；MP-1.2 | Codex | 两种实际执行模式，有限超时与预算 | 是，Host 完成门 | 待验 |
| AC-03 | 包/依赖/权限认证及撤销；MP-1.3、计划 §6 | Codex | 篡改/扩大权限/撤销，真实 macOS sandbox 拒绝受控 Home 探针与网络；不可用 fail closed | 是，安全门 | 开工宿主 sandbox-exec 可用；Docker daemon 不可用 |
| AC-04 | epoch/迟到提交/启动停止失败/重启/Run 依赖；MP-1.4 | Codex | 定向并发和故障注入 | 是，安全与恢复 | 待验 |
| AC-05 | Host 资源 keep/delete/续做/残留，缺 Hook 可清理；MP-1.5 | Codex | 临时库/目录重启与中断测试 | 是，资源基础 | 待验 |
| AC-06 | 配置、任务发起/历史/详情/审批取消及恢复入口，受支持项目身份；计划 §4 GUI-L1、治理根客户端规范 §16.1 GUI-L1 与逐模块验收1～5 | Codex | 正式 API/生成 SDK + GUI 行为定向测试 | 是，任务闭环 | 待验 |
| AC-07 | 无插件真实桌面任务/工具/历史/合法恢复；强依赖记忆 Graph 拒绝；计划 §6、AGENTS 真实模型 | Codex | 先 discover 精确模型，正式 ModelProfile/入口；隔离绝对 workspace，单个调用有限预算/120s，失败按规则停止 | 是，真实链路 | Discovery 已通过：67 个精确 ID，含 gpt-5.6-luna；尚未真实调用 |
| AC-08 | 宽窄屏/键盘/对比度/错误断线/Action Gateway/实际 Tauri；AGENTS 客户端边界 | Codex | 本机 debug Tauri 与 UI，检查本批新增流程；不覆盖发布签名 | 是，客户端门 | 真实 debug WebView 已打开并连接隔离 Core；Tauri preflight 200/未知origin400，待完整行为验收 |
| AC-09 | 完整基础门禁；AGENTS 开发与验证 | Codex | ruff format/check、mypy src、pytest、uv lock --check --offline、git diff --check；集成版本一次 | 是，硬门禁 | 待验 |
| AC-10 | GUI/SDK test/typecheck/build、协议确定性；AGENTS | Codex | 当前 package scripts + SDK 定向测试/生成回读，旧 Schema 不变 | 是，契约门 | 既有依赖可复用，不改锁 |
| AC-11 | 架构同步、保留范围、独立 Reviewer 与进度；AGENTS/用户请求 | Codex | diff/证据覆盖审查，Luna max，同 Reviewer 增量 | 是，交付门 | 待验 |

## 环境与证据规则

开工发现 Docker daemon 不可用；MP-1 沙箱拟用已实测可启动的 macOS sandbox-exec，不能用普通子进程替代隔离。Python/GUI 既有依赖存在；uv 两处离线缓存缺 h11/cryptography，已复制 B2-1 已安装依赖至本树 .venv 并修正 editable link/脚本路径，确认 import 指向本树。使用 uv run --no-sync，不改锁。Provider Discovery 已通过。Core 使用正式 desktop 启动器隔离在18000/临时库，Vite3000；8000既有用户服务保留。桌面代码/构建输入无改动，复用 B2-1 debug 二进制并通过独立临时.app加载本树Vite；原生窗口与连接已观察。
执行者只做定向验证；集成后完整门禁一次。代码变更后完整基础门仍需覆盖最终代码；昂贵检查只复核受影响场景。失败一次最小诊断，无条件变化不循环重试。源码、依赖、构建和环境不变才复用明确 HEAD 的证据。

## 交付

- Codex 接受 api_tests 两文件归属交回；子 Agent 定向9测试与格式/lint通过，源码缺口已修复。此证据不替代集成基础门禁。

当前：工程组件已交回，集成验收中；证据、集成版本、Reviewer、限制于完成后填入 `handoff.md`，总进度仅治理根 `memory/current.md`。

- Codex 接受 host 的13项定向测试/真实隔离smoke交接及路径归属；GUI 90项测试/typecheck/build 与 Antigravity a0a4a5d 样式均已交回，样式import已集成。完整门禁/真实GUI/Reviewer仍待完成。
- 恢复范围必须按客户端规范§16.1审查：已有Session显式新轮、Phase23 Graph安全恢复入口保留；Task统一resume当前明确unsupported，不能将此标记替代恢复验收。

## 集成检查增量

- edd5870 完整基础检查：Ruff/mypy/lock/GUI 90测试/typecheck/build通过；pytest 756 passed/2 failed/1 Docker skip。失败分别为新增getTask未同步surface测试预期、沙箱禁止loopback bind。原始输出见临时gate-logs，最终候选需全门禁重跑；不能把本结果写通过。
- 真实原生空Workspace走查发现无Thread创建入口，补B2 createThread(workspace_id)及GUI入口，新增幂等/已登记scope定向测试；来源为AC06/07及客户端规范§16.1。
- MP1.2 资源上限按manifest/Host预算复核中；只有声明没有实际执行约束不能标通过。

- 集成修复：首个Thread创建、分页继续、Role预算保留、Host并发/RSS/CPU/idle与租约scope/issuer/认证时效。Host/API/SDK 31定向测试通过，真实macOS sandbox stdio两次RPC通过。
- 实际Tauri首次模型调用成功（gpt-5.6-luna/low，read_file，结果42），暴露canonical history未写入；修复正式Session绑定Thread运行的Event+Item同事务记录和GUI终态刷新。新增运行/幂等/分页/SQLite重开回读测试通过，原Context引用只读/压缩28测试通过。真实新代码审批/回读仍在验收。
- 原host/gui因workspace额度失败，恢复失败后负责人接回修复；原Reviewer无活动句柄，已按原范围启动替代Luna/max独立Reviewer。Antigravity按最新AGENTS条件静默挂起。

- 2026-09-11：0dd0d49基础门禁766 passed/1 Docker skip；GUI90/test/typecheck/build通过（随后API/GUI新增修复需最终集成门禁）。真实原生正式ModelProfile gpt-5.6-luna/low：read_file结果42；只读cat审批一次通过，最终42已自动写入并显示历史；实际取消回读agent.cancelled与Task已取消。旧中断审批明确拒绝，不自动重放。
- 补修复历史整页脱敏破坏schema（多Agent+历史页回归）、只读Query误锁manual_reconcile、取消终态错看Thread、运行实例分页可见性；API/SDK13定向通过。原生768宽任务卡断点冲突已实测修复，键盘焦点可见。
- 指定替代Reviewer 01a08baa-4872-7473-abb1-7ca6fe91517d已启动后因workspace credits失败，尚无独立结论；原Reviewer 01a08b5d-0c63-7411-a99c-1742707cddd9，保持证据待恢复，不再无条件重试。
