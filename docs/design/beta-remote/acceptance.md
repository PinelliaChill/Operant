# Beta 任务 5：H-17 跨设备验收

> 记录身份：独立验收 Agent；适用对象：Beta 任务 5；基线 `85f280063178d6ebf7748d246902b6403cba7f57`。
> 本文件是任务内用例和结果入口。部分负例已在真实 macOS↔Ubuntu 链路实测；其余用例仍须按下表逐项裁决，不能把源码审查或本机仿真记为真实双设备通过。

## 范围与边界

H-17 要求设备配对、授权、状态诊断、远程控制与受控执行、断线恢复和撤销形成可使用的闭环。Remote Control 控制本机 Core；Remote Execution Target 在另一台机器上执行受租约约束的动作。两条链路分别验收。`host_ack` 必须来自 Core 持久命令收据，Relay 投递回执不算。远程命令的 `completed` 只表示命令执行器已返回；若命令启动了 Session，还须从正式 Session Query/Event 读取模型运行终态和结果。

正式验收设备为本机 macOS 和另一台 Ubuntu。两端使用本次任务的临时私有目录和隔离 SQLite；不使用日常 Core、OpenClaw、真实用户库或公网监听。Core 仅监听本机 loopback 并启用 TLS/WSS；Ubuntu 通过 SSH 反向隧道访问**仅放行配对、WSS Gateway、签名加密结果 Query 和 Target 逐次 Lease 验证的受限入口**，不能直通整个 Core HTTPS 端口。Target 验证路由也须绑定目标身份、Lease 与 Job，不能变成管理入口。否则反向隧道在 Core 看来来自本机 loopback，设备可绕过 Scope 直接调用免认证管理 REST，RC-06 必须判失败。远端 Target 仅监听 Ubuntu loopback，本机经 SSH 正向隧道访问。证书必须校验受信 CA/SAN，不能关闭 TLS 验证。Relay 如需验证，应使用单独用户/目录/凭据，且结果仍要回到 Host 收据。

证据只保存源码 HEAD、构建或 wheel 摘要、Python/CLI 版本、两台机器的系统类型、监听地址、隧道方向、请求 ID、设备/会话/Target/Lease/Job ID、状态、游标、结果摘要和退出码。票据、密钥、Lease Token、Bearer、模型凭据、原始 prompt 和完整响应不得写入本文件或日志；设备本地状态文件权限应为 `0600`，目录为 `0700`。正式模型先用 `uv run operant model discover` 发现精确 ID，再用正式 ModelProfile 和受控临时工作区调用。不得因模型失败改用 Mock 声称通过。

## 验收用例

| 用例 | 输入与操作 | 必须观察到的结果与证据 | 实现 / 验收 |
| --- | --- | --- | --- |
| RC-01 配对和授权 | macOS 本地启用 Host，预授权最小 Scope，生成短时一次性票据；Ubuntu 正式设备客户端以独立 Ed25519/X25519 身份配对，建立会话 | 设备 ID、批准 Scope、票据消费、会话绑定与过期时间；扩大 Scope 和重用票据被拒，票据正文不进入持久收据 | 已实现 / 通过私有双机实测；见独立负例记录 |
| RC-02 远端正式任务 | Ubuntu 设备经 WSS 提交签名加密的 Session 创建、运行命令；本机批准必要 ASK，模型使用发现的精确 ID | 每条命令从 WSS 得到 `host_ack.receipt`，本机 `GET /v1/remote-control/commands/{id}` 同 ID/Action Hash/终态；正式 Session Query/Event 显示模型 Run 终态及可核对输出，副作用只经 Action Gateway | 已实现 / 通过 |
| RC-03 断线恢复 | 保存事件游标，断开 SSH/WSS，再恢复同一会话并执行 `cursor_sync(after_cursor)`；查本机 Session 投影 | 只返回游标之后的持久事件，继续页读到追平；远端断线不取消本地 Run；Query 与 Event 终态一致，不以客户端缓存作权威 | 已实现 / 通过 |
| RC-04 重复命令 | 将同一已签名 Command 原帧再次提交；对已提交但未收到 Ack 的命令按原 ID 回读 | 相同 ID/幂等键对应同一持久收据和单次副作用；冲突绑定被拒，不能生成第二个 Session/Run | 已实现 / 通过 |
| RC-05 撤销和紧急关闭 | macOS 撤销设备并关闭 Host；Ubuntu 旧会话重连、发送旧签名帧，再尝试新帧 | 新连接/命令被拒；本地设备、会话、Gateway 连接诊断可见已撤销/关闭；已接收的本地 Run 不因网络断开而悄然取消 | 已实现 / 通过：独立设备及主设备撤销、旧连接/旧帧拒绝、Host 紧急关闭 |
| RC-06 设备隔离与只读 Scope | 另配一个 observe-only 设备，尝试 command；两个设备交叉查询、运行或取消对方创建的 Session；配对时请求超出预授权的 Scope，并尝试从远端走管理 REST 路由 | Scope 扩大和所有跨设备操作被拒；远端普通 REST 管理不能绕过设备签名/归属校验；设备本人通过加密 Query 读取自己的 Session 结果 | 已实现 / 部分通过：跨设备负例与 edge REST 拒绝已测；本人 Query 正例由主线程证据汇总 |
| RT-01 远端受控执行 | 本机登记 Ubuntu Target 的公开身份、精确能力与私有 endpoint ref；心跳后获取一次性 Lease；Core 经正式 Connector/Action Gateway 派发只作用于 Ubuntu 临时工作区的任务 | Ubuntu 真实进程执行，Job ID、fencing、单次副作用和目标文件摘要可从两端核对；本机 `GET /v1/remote-targets/jobs/{id}/result` 返回签名验证后的终态/结果 | 已实现 / 通过 |
| RT-02 Lease 与身份拒绝 | 释放/过期 Lease 后，用旧 token/fencing 轮询、完成和再次派发；执行中的任务再触发释放/过期；用错误目标身份/签名测试 | 所有旧 Lease 或漂移身份请求被拒，远端没有第二次动作；执行中撤销的副作用与 Core 终态保守且一致，Target 状态和拒绝原因可诊断 | 已实现 / 通过 |
| RT-03 未知结果 | 在一个非幂等任务已送出后切断返回链路；恢复并按原 Job ID 查询，不直接重试动作 | Core 将未知结果标为 `manual_reconcile_required` 或等价保守状态，远端动作次数至多一次；人工核对事实后才可新授权，不自动重放 | 已实现 / 通过 |
| UX-01 用户流程 | 本地 GUI/TUI 按已实现入口完成 Host/Scope/票据、设备和 Target 状态诊断、结果回读与撤销；在 Ubuntu 实际使用设备端入口 | 错误/断线/撤销明确显示，宽窄屏与键盘焦点可用；不靠手写数据库、测试 Fixture 或隐藏状态完成用户流程 | 已实现 / 通过 |

## 基线源码审查与接线点

- `src/operant/remote_control/gateway.py` 中 WSS `command` 返回 `host_ack.receipt`；`cursor_sync` 读 Host 持久事件。`scripts/smoke_remote_gateway.py` 只覆盖本机 hello/ping/cursor，不能覆盖 RC-01～RC-05。
- `src/operant/server.py` 需要 TLS 证书、`OPERANT_REMOTE_GATEWAY_TOKEN_REF` 和 `OPERANT_REMOTE_GATEWAY_ALLOWED_ORIGINS_JSON` 才启用正式 WSS。配置 Core 为 loopback 后经隧道到另一台设备；不暴露普通 `/v1/*` 到公网。
- 基线 `src/operant/api.py` 创建 Remote Control 时没有通用 Session executor；`src/operant/api_b2_6.py` 仅接入 memory query/command。`src/operant/remote_control/runtime.py` 默认 executor 会报告 `remote.operation_unavailable`。RC-02 需要本任务正式补齐，并核对权限映射。
- 基线 Remote Control 事件查询用命令收据的 `rowid` 作游标，状态更新只改原行。若设备先读到中间态，随后断线，终态更新不会产生更大游标。RC-03 须明确测试这个时序，并修复为可恢复的追加事件或等价机制。
- `src/operant/remote/http_connector.py` 只有向 `/v1/target/jobs/execute|cancel` 发请求的 HTTPS 客户端，基线没有对应远端服务端。RT-01～RT-03 需要本任务真实 Ubuntu Target Worker；只用本机 Mock HTTP Server 不通过。
- `clients/gui/src/features/remote/LiveRemoteView.tsx` 基线已有本地启用 Host、生成票据、列表与撤销、Target/Job 列表；设备端配对、控制任务/HostAck、断线与 Target 执行诊断仍需核对本任务实现。

## 双设备运行顺序

主线程负责启动隔离 Core、受限 edge、Ubuntu Target 和 SSH 隧道，并锁定最终 wheel/依赖与证书。远端设备使用同一冻结 wheel 的 `operant remote-device`，`OPERANT_REMOTE_CA_FILE` 指向受信证书，`OPERANT_REMOTE_ORIGIN` 与 edge/Core 的精确 Origin 白名单一致，`OPERANT_REMOTE_GATEWAY_TOKEN` 只注入设备进程环境。Ubuntu 的 `--core-origin` 必须指向 SSH 反向隧道的**受限 edge**，不能指向完整 Core。临时配对票据、设备状态和消息文件均为绝对路径、私有目录内的 `0600` 文件。

`scripts/acceptance/remote_cross_device.py` 是设备侧协调脚本，每次仅运行一个正式 CLI 步骤，输出不含票据、Token、消息正文和完整模型输出的 JSON 摘要。先在 Ubuntu `init`；本机 GUI/TUI 生成含全部字段的配对票据并安全交接后，Ubuntu `pair --ticket-file … --display-name …`。本机经正式管理入口为返回的 Device ID 创建 Remote Session；Ubuntu `bind --remote-session-id …` 会立即做一次 `cursor_sync`。这些步骤必须在一次性票据过期前衔接配对。

接着 Ubuntu `create --role-id … --project-id … --model-profile-id …`；命令的 `result_ref=session:<id>` 是新 Session ID。若 Host Ack 返回 `rejected/remote.approval_required`，其中 `result_ref` 是 Approval ID；本机管理入口先 `GET /v1/security/approvals/{id}` 核对精确动作，再 `POST /v1/security/approvals/{id}` 提交 `{"approved":true}`。Ubuntu 用 `retry-pending` 重新发送**原签名帧**，不能新建幂等键或换一个命令。批准/重提后的同一 Command ID 应完成且只创建一个 Session。再用 `run --session-id … --message-file …` 走正式模型；同样处理 ASK。`query --session-id …` 通过设备签名加密 Query 解密读取模型结果；`status --session-id …` 与 `cancel --session-id …` 可用于另一设备归属拒绝用例，负例应使用可丢弃的独立设备状态。`sync` 读取 Host 游标。拔掉隧道或 WSS 后先 `pending` 查本地未确认命令，再重连 `sync`/`retry-pending`，不能直接重跑 `create` 或 `run`。

主线程在 macOS 运行 `scripts/acceptance/verify_remote_readback.py`，把 Ubuntu 输出的 Command/Session/Job ID 作为参数，核对同一隔离 Core 的持久收据与正式 Session/Target 投影。远端 `run` 前先保存该 Session 的事件游标，回读时传 `--session-after-cursor`，防止旧的成功事件误算这次模型运行。Target 侧须由 Ubuntu `operant remote-target init/serve`、本机登记/心跳/Lease/Job 和 `operant remote-target dispatch` 完成；`serve` 必须 TLS 且仅监听 Ubuntu loopback，本机 Connector 必须信任精确 CA。Job 使用 Ubuntu 私有工作区内的固定白名单命令或相对路径只读动作。安全负例按上表逐项执行，任何真实修改只限临时目录。

## 结果记录

### 独立负例：2026-10-05，真实 Mac↔Ubuntu

实际入口为 Ubuntu 安装的同一冻结 wheel `operant remote-device`，经 SSH 反向隧道访问 Mac `https://127.0.0.1:18807` 受限 TLS edge；Mac 的管理请求只走本机 `https://127.0.0.1:18805`。完整脱敏状态与 Command ID 存在隔离运行目录 `.operant/beta-task5/evidence/negative-rc.json`，该目录不提交。测试仅使用新建的 observe-only 和 command-capable 设备；主设备 `device_6261b9f1892a4c459892e996232e12d8`、既有模型 Session `session_7ec72e8a3b60404887974f42b1fbbda8` 未被本项修改或取消。

- RC-01：Host 给一次性票据只预授 `remote.control.observe`。Ubuntu 先请求再加 `remote.control.command`，edge 返回 HTTP 403 `remote.scope_not_approved`；随后同一票据由正式设备 CLI 成功配对，仅获 observe Scope 并建立 Remote Session。第二个独立设备用已消费票据配对失败。票据、密钥与完整响应未写入本文件。
- RC-06：Ubuntu 对受限 edge 请求管理 REST `/v1/remote-control/hosts` 返回 404。observe-only 设备对主设备 Session 的加密 Query 失败，WSS `status` 的 Host Ack 为 `rejected/remote.session_scope_denied`；其 `run` 在命令前被 Scope 拒绝，设备保留原 pending，Core 没有该命令收据。另一个具有 command Scope 的设备对主 Session 的 Query 和 `status` 也被拒。其 `run`、`cancel` 先各自触发本机精确 Action Hash 的 ASK；只批准这两个测试 Action 并重发原签名帧后，Core 两条持久收据均为 `rejected/remote.session_scope_denied`。因此拒绝由 Session 归属裁决，不能只归因于 Scope 或未审批。
- RC-05：Mac 经正式管理 API 仅撤销上述两个测试设备。Ubuntu 两个旧会话的正式 `sync` 均失败，撤销后 command 设备的加密 Query 也失败；主设备未撤销，Host 未关闭。旧签名帧重送和 Host 紧急关闭仍需主线程在环境收尾时核对，故本行只记部分通过。

RC-04 的主设备重复消息、RC-02/03 正式模型与断线、RT-01～03 和 UX-01 的最终裁决由主线程补入证据；本段不替代它们。这是独立负例完成时的状态；后续主线程补测结果见下节。

运行后按用例追加：冻结 HEAD、设备与入口、输入摘要、预期、实际状态、证据文件相对路径、实现状态与验收状态。每项分别标记“未实现/部分/已实现”和“未测/通过/失败/受阻”。失败历史和修复后的结果都保留。RC/RT/UX 必需用例有失败、受阻或未测时，H-17 不标整体完成。

Host 可在真实远端操作之后运行 `scripts/acceptance/verify_remote_readback.py`，传入临时 Core HTTPS URL、受信证书、Command/Session/Target Job ID。脚本只读正式 API，核对持久 Host Ack、Session 的 `model.completed` 与 `agent.completed`、Target Job 终态，并仅输出脱敏摘要。设备自己的结果须再通过正式加密 Query 读取；Host 脚本不代替设备端验收。


### 主线程最终集成：2026-10-05

记录身份：Codex 主线程。范围为六次计划任务 5 H-17；任务 6 未启动。真实机器为本机 macOS/Python 3.13.3 与现有云服务器 Ubuntu/Python 3.12.3。全部新操作位于隔离工作树及云端 0700 临时目录；生产 OpenClaw 的只读健康回读始终为 `ok=true/status=live`。Core/edge/Target 仅监听 loopback，未暴露公网接口。依赖来自同一锁文件；最终 wheel SHA-256 为 `2356bb4810b9fc47232f40554541ad6c92a4788959d2b68516b235b335e75aee`，两端文件摘要一致，wheel 中所有 Core Python 文件与冻结源码逐字节相同。完整输入清单在隔离证据 `frozen-inputs.json`，发布/已安装 App 和真实用户库未变。

- RC-02：先正式 `model discover` 发现 `gpt-6-luna`，建立正式 ModelProfile、无工具/写入权限的 Role 与绝对临时 Project。Ubuntu 正式设备 CLI 配对、创建 Session `session_7ec72e8a3b60404887974f42b1fbbda8`，原创建帧经本机精确 ASK 批准后完成；远端发起模型任务。Core 同 ID/Action Hash 的持久 Host Ack 完成；正式 Query/Event 有 `model.completed` 和 `agent.completed`。设备通过签名加密 Query 解密结果，输出摘要 SHA-256 `96ac084a78e06e18714b69a845db2899bf43a82ce3f4f26ef948b6ca0314fe1b`。证据：`device-create-approved-preflight.json`、`create-receipt.json`、`device-run-approved-preflight.json`、`device-query-preflight.json`、`host-readback.json`。后续修复仅影响 Target 租约失效结果持久化，模型/设备链路源码、依赖和入口不变，因此复用此证据。
- RC-03/04：Command `remote_command_d2bc120936bd4f50a37ffad80f039c04` 在 Core 进入 accepted 后，终止 Ubuntu 实际设备 CLI 使 WSS 断开且 Ack 丢失。Core 继续完成真实模型；设备以原 pending 重连，正式 CLI 返回 `source=recovered_event/status=completed`。保存的同一签名帧又实际提交两次，得到完全相同的 completed 收据，Session 游标 12→16 仅新增一个 `model.completed`；没有第二次模型副作用。SSH 隧道中断期间远端 sync 明确退出 1，恢复后从原游标追平。证据：`disconnect-recovered.json`、`disconnect-session-readback.json`、`device-replay.json`、`sync-offline.txt`、`device-sync-final.json`。签名冲突、分页和 v22→v23 游标迁移另由确定性测试覆盖。
- RC-05：独立设备撤销后，主设备也通过正式 Core 管理接口撤销；同一 Host 经正式 enable 接口 `enabled=false` 紧急关闭。旧设备 sync 和旧签名帧重送均退出 1，Host enabled=false，三个 Remote Session 全为 closed。证据：`host-disabled.json`；设备拒绝与本地模型完成状态同时保留。
- RT-01：Ubuntu Target `task5_ubuntu_target2` 正式 init/serve，Core 正式登记、心跳、短期 Lease 与 Action Gateway ASK；仅白名单的绝对 executable+固定 argv 能执行，临时计数文件用于核对单次动作。最终修复后的 Job `remote_job_07a4c1032a9740109bb186f6f3e638a8` 在 Ubuntu 真进程执行，签名结果为 succeeded、exit_code=0、stdout=`TASK5_TARGET_OK`。前后各一次明确授权的正常运行使 count.txt 从 1→2，未授权重放没有新增。正式 GET 读回成功，GUI/TUI 已回读此前同链路成功结果。证据：`target-normal-final-readback.json`、`target-normal-dispatch-final.json`、客户端 `client-real/acceptance.md`。
- RT-02/03：已释放 Lease 的 poll/verify 与 1 秒 TTL 过期后的 poll 均返回 409，错误 Target 公钥 heartbeat 返回 409。最终 Job `remote_job_4af6b58c17634f78995c50c7c993155a` 在 Ubuntu 写入计数后，运行中释放 Lease 并切断 SSH 返回链路；Core 正式 GET 结果与 dispatch 返回逐字段相同：`manual_reconcile_required/remote.lease_invalidated_outcome_unknown`，来源幂等键为 `core-lease-invalidated:<job_id>`，没有伪称 Target 已签名成功。unknown-count.txt 从 1→2 表示旧修复前一次和最终复验一次，最终 Job 仅新增一次动作。旧 Lease 不再能派发/完成；未知结果不自动重放。证据：`target-boundaries.json`、`target-unknown-final-readback.json`、`target-dispatch.json`。当前 Lease 与整个已领取 Job 绑定、未经 Core cancel 拒绝、签名漂移、protected path、输出上限和运行中 expiry 的异常边界另由定向测试覆盖。
- UX-01：GUI 正式 Live TLS Core，1175px/390px 无 Remote section 横向溢出，Tab 焦点可见；Host/Scope/紧急关闭确认、Host Ack/游标/Target 状态可读。TUI Alt+6，72×32/110×45/80×35 正式协议及结果回读可用；两端成功 Job 状态和输出一致。同名 Target 增显 ID。设备和 Target CLI 完成配置→配对→任务→结果→撤销，全部使用正式 API/SDK/CLI，不写数据库补状态。GUI 创建票据最终点击及 Target 表单每个写按钮未逐个做系统 UI 点击；本次同一用户流程由正式 TUI/SDK、CLI和真实结果读回覆盖，不外推所有 GUI 写交互已验收。

失败历史保留：最初验收脚本误把 httpx 无凭据 URL 的空字符串判为有凭据，已修；第一次 Target 配置误用不支持的 `exec_argv`，正式失败 `remote.operation_denied`，改为实际 `run_allowlisted`。旧 Target 进程未随 SSH 协调进程退出导致新服务端口占用，Core 保守标未知，确认云端无动作后释放旧 Lease、获取新 fencing 才继续，未重放原 Job。断线协调脚本最初只终止父进程，不能证明实际 WSS 已断，改为终止实际 CLI 后重新验收。真实运行中 Lease 释放还发现 dispatch 临时 unknown 回执未持久；已改为与 Job 同事务写 Core 来源结果，定向 14 项和上面的最终两机复验通过。前一轮本机完整 pytest 在 96% 时随执行环境中断，不能记为通过；保留日志，由最终冻结提交 CI 补足完整门禁。

最终定向结果：后端/协议/设备/Edge 原 69 项通过，最后持久结果修复后的 Remote Execution 14 项通过；GUI 145、TUI 34 通过，GUI 类型/构建通过，Ruff、mypy、锁和 diff 检查通过。16 个既有 opt-in 跳过及其他未受影响的历史限制不冒充真实验收。公共 Schema/生成 SDK 一致且确定性测试通过。私有 Mac↔Ubuntu 的控制与受控文件/白名单命令执行是本次环境范围；公网、任意远程桌面/App、多租户、分布式 Core 和生产环境部署不因此获得验证。
