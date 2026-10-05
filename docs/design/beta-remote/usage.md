# 私有双设备远程使用说明

本说明对应 Beta 任务 5 工作分支。Mac 上的 Core 保留管理权威，另一台 Ubuntu 用设备 CLI 控制该 Core，或作为受控 Target 执行限定动作。当前范围是私有网络和 SSH 隧道；实际双设备通过情况见[验收记录](acceptance.md)。`main`、旧标签和已安装 App 不会因工作分支变化自动更新。

## 连接边界

在 Mac 的私有临时目录运行启用 TLS 的 Core（管理端口只监听 `127.0.0.1`），并运行 `operant remote-gateway serve` 作为受限 TLS edge。SSH **反向**隧道只把 Ubuntu 的 loopback 端口接到 edge；**不要**把 Core 管理端口通过反向隧道交给远端。edge 只允许设备配对、签名加密结果 Query、WSS Gateway 和 Target 单 Job Lease 验证，其他 `/v1/*` 与 `/web` 不可从远端到达。Target 在 Ubuntu loopback 启动，本机如需派发可通过 SSH **正向**隧道连接它。Core、edge 与 Target 都使用受信证书校验，不能关闭 TLS 验证；不开放公网端口。

两端分别使用 `0700` 临时目录、`0600` 身份/票据/Token 文件和隔离数据库。密钥、票据、Bearer、Lease Token、模型凭据与原始任务正文不要放进命令历史、验收日志或 Git。Remote Control 的设备身份和 Remote Execution Target 的身份分别生成，不能混用。

## 从 Ubuntu 控制 Mac 上的 Session

1. Mac 的 GUI「设置→远程」或 TUI `Alt+6` 启用 Host，选择最小 Scope，生成一次性 PairingTicket。完整 JSON 只在短时窗口中交给 Ubuntu 私有文件。设备端的 `OPERANT_REMOTE_CA_FILE` 指向受信证书，`OPERANT_REMOTE_ORIGIN` 与 Host 白名单精确一致，`OPERANT_REMOTE_GATEWAY_TOKEN` 只注入当前进程；`--core-origin` 指向 Ubuntu loopback 上反向隧道的 **edge**。
2. Ubuntu 使用 `operant remote-device init --state-file ABS` 创建设备身份，再运行 `operant remote-device pair --state-file ABS --ticket-file ABS --core-origin https://127.0.0.1:EDGEPORT --display-name NAME`。票据只能使用一次，过期后从 Mac 重新生成。
3. Mac 为返回的 Device ID 建立 Remote Session。Ubuntu 用 `operant remote-device bind-session --state-file ABS --session-id REMOTE_SESSION_ID` 绑定，再运行 `sync --state-file ABS --core-origin https://127.0.0.1:EDGEPORT` 读取 Host 持久 Cursor。
4. 在 Mac 上先准备已初始化 Project、冻结 Role 和正式 ModelProfile。Ubuntu 用 `operant remote-device command --state-file ABS --core-origin https://127.0.0.1:EDGEPORT --operation create --target-id new --arguments-file ABS`，参数 JSON 为 `{"role_id":"...","project_id":"...","model_profile_id":"..."}`。`host_ack.receipt` 的 `result_ref=session:<id>` 给出新 Session ID。随后发送 `run`，`--target-id` 为该 ID，参数 JSON 为 `{"message":"..."}`。运行结果通过 `query-result --state-file ABS --core-origin https://127.0.0.1:EDGEPORT --session-id ID` 的签名加密 Query 读取；Host Ack 只证明命令进入 Core 持久收据，还须看到正式 Session 的模型和 Agent 终态。
5. 若 Host Ack 是 `rejected` 且 `error_code=remote.approval_required`，其 `result_ref` 是本机 Approval ID。先从 Mac 管理入口读取审批并核对精确动作，再在 Mac 批准；Ubuntu 执行 `retry-pending --state-file ABS --core-origin https://127.0.0.1:EDGEPORT`，只重发原签名帧。断线后先运行 `pending` 与 `sync` 并追平事件；原帧过期或副作用结果未知时人工核对，不新建命令盲重跑。

Mac 可撤销设备或紧急停用 Host；旧设备会话、Scope 或密文不能继续执行。设备只能查询自己创建的 Session，observe-only Scope 不能发送命令。GUI/TUI 显示配对、设备、Gateway/Cursor、Host Ack 和撤销状态；最终状态以本机 Core 投影为准。

## Ubuntu 作为受控 Target

Ubuntu 用 `operant remote-target init --state-file ABS --target-id ID` 生成独立公钥。Mac 在 GUI「设置→远程」或 TUI `Alt+6` 登记该 Target 的公开身份、私有 endpoint/credential 引用、精确 Capability Manifest 与工作区命名空间，心跳后获取短期 Lease。当前 Target 只支持私有工作区内非保护文件的 `read_text`，以及配置文件中完整 argv 精确匹配的 `run_allowlisted`；命令没有隐式 Shell，进程输出和运行时间有界。

Ubuntu 使用 `operant remote-target serve --state-file ABS --ledger-path ABS --workspace ABS --lease-id ID --fencing N --expires-at ISO --allow-argv-file ABS --core-origin https://127.0.0.1:EDGEPORT --core-ca-file ABS --tls-certfile ABS --tls-keyfile ABS --port PORT`。服务固定监听 `127.0.0.1`，本次 Lease Token 与 Target Bearer 分别只通过 `OPERANT_REMOTE_TARGET_LEASE_TOKEN`、`OPERANT_REMOTE_TARGET_BEARER_TOKEN` 进程环境注入。Mac 经正式 GUI/TUI 建立 Job 后，执行 `operant remote-target dispatch --target-id ID --lease-id ID --fencing N --db-path ABS --ca-file ABS`；其 endpoint 必须是受信 HTTPS 隧道地址。Mac 从正式 Job Result 读取状态，Ubuntu 临时工作区核对真实效果。

Target 每次执行或取消前会向 Core 核对当前 Lease、已领取 Job、工作区和能力。Lease 释放、过期或身份不符时旧 token/fencing 失效；执行中断线或非幂等结果未知时进入人工核对，不自动重放。一个 Lease Token 丢失后不要从 SQLite 提取真值，等待旧 Lease 过期并重新申请。
