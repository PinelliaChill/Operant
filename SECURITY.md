# Operant 安全边界

> 最后更新：2026-09-04

Operant 会把模型输出视为不可信输入。模型只能请求由当前 `RoleSnapshot` 的 Tool Policy
允许的工具；Runtime 和工具层会再次校验，不把“模型遵守提示词”当作安全边界。

Phase 4 又在 Tool Policy 之上增加统一的 Action Policy/Capability 栅栏。这一层不取代既有
RoleSnapshot、workspace 路径保护、Docker Runner、Receipt 和 Session Approval；两层中任一层拒绝，
副作用都不能执行。

## Phase 4 Action Policy 与 Capability

每个受管副作用首先规范化为 `ActionRequest`，并把以下事实绑定到稳定 Action Hash：

- principal 和可选 Session/Workflow/Node/Agent scope；
- tool、operation、规范化 target/arguments 与 workspace identity；
- 数据分类、所需 Capability、sandbox/network profile 与幂等等级；
- Secret Ref、dry-run、Policy Version 和调用方幂等键。

路径必须在已解析 workspace 内；URL 要求 HTTP(S)、无 userinfo，并规范化 scheme/host/port、
去除 fragment。Secret 只能用受限环境变量名形式的 reference 声明，不允许把真值放入 Action。

Policy 规则按 System、Workspace、Role、Workflow、Session、Approval 和 Default 层匹配。同一 Action 中：

- 任一 Capability 命中 DENY，整体 DENY；
- 无 DENY 时 ASK 优先于 ALLOW；
- 无规则命中默认 DENY；
- System hard DENY 不能被低层规则、客户端、人工审批或 LLM Reviewer 覆盖。

`ApprovalReviewerAdapter` 只能审查 `reviewer_eligible` 的 ASK，只获得脱敏的最小 Action 摘要，且只能
返回 ALLOW 或 DENY。超时、异常、非法 JSON 或仍返回 ASK 都固定收口为 DENY。Phase 45 系统动作使用
独立持久 Approval：决定绑定精确 Action Hash、Target、Policy Version 和有效期；客户端不能伪造
`decided_by`，只能由 User 入口或 Core 配置的 Reviewer Adapter 决定。批准后仍重新评估 Policy 且只能
消费一次；DENY、过期、已消费和 hard DENY 均不会继续。它不替代既有 Session Tool Approval。

Capability Lease 只能从精确 ALLOW 的 Action 发放，并绑定 Action Hash、principal、Capability、target、
workspace、Policy Version、TTL、约束和最大使用次数。消费使用 SQLite CAS 重新核对这些字段、
过期、撤销和用量；旧、越目标、超量或被撤销 Lease 均失败关闭。

Secret Broker 只在精确 ALLOW 且包含 `secret.use` 的 Action 执行时解析已声明 `secret_ref`，并生成
短 TTL、单目标 Secret Lease。真实 Secret 值只进入目标 transport 的进程内 resolver，不写入 SQLite、
API、Policy 解释、Audit 或错误；返回输出还会按实际值再次脱敏。legacy SSE 当前把 endpoint 与 bearer
限制在最短 60 秒 Lease 内，到期关闭连接并清空 resolver。

Security Action 和 Audit 是不可变/只追加事实。Audit 仅保存决策、规则 ID、有界详情、结果 hash
和安全错误码，不保存原始参数/结果或 Secret。重复拒绝仅通过不含参数值的 signature 计数，
达到 no-progress 阈值也不会自动放宽 Policy。

## 默认角色与命令执行

- Main、Planner、Explorer 和 Reviewer 只获得读取、搜索和 `git_diff` 工具，不能写文件或执行命令。
- 新初始化的默认 Coder 可改写 workspace，但其 `run_command` 使用 Docker Runner。
- Docker 不可用、未启动或所需镜像不存在时，命令会明确失败，不会静默回退到宿主机执行。
- `host` Runner 仅用于用户明确认为可信的本地 workspace；它不是操作系统级沙箱。

角色快照会保存命令执行策略。修改角色后，既有 Session 不会继承新权限或新限制。

## Docker Runner 的边界

Docker Runner 不直接挂载用户 workspace，而是先创建过滤后的临时快照。文件名匹配不区分大小写，
快照排除：

- `.git`、`.env` 与 `.env.*`；
- `.credentials`、`.git-credentials`、`.netrc`、`.npmrc`、`.pypirc`；
- `credentials.json`、`service-account.json`、`service_account.json`、`secrets.json/yaml/yml`；
- `id_rsa`、`id_dsa`、`id_ecdsa`、`id_ed25519` 及 `.key`、`.pem`、`.p12`、`.pfx` 文件；
- `.operant`、`.venv`、`node_modules`、Python/pytest 缓存。

因此 `.ENV`、`.NPMRC`、`ID_RSA`、`CREDENTIALS.JSON` 等大小写变体也会按同一规则排除。

容器使用以下固定限制：

- `--network none`，工具命令不能联网；
- 每个 Role Policy 指定 CPU、内存和 PID 上限；
- `--cap-drop ALL`、`no-new-privileges`、只读容器根文件系统；
- 临时 `/tmp` 和 `/var/tmp`，并以当前宿主 UID/GID 运行；
- 仅把过滤后的快照挂到 `/workspace`；当角色不允许写 workspace 时，该挂载为只读。

测试命令在临时快照中的写入不会回传宿主 workspace；源码修改必须使用受 Tool Policy 保护的
`apply_patch`。如果 Docker 命令超时或运行被取消，Runtime 会终止 Docker 客户端进程组，并在
拿到容器 ID 时执行强制清理。

Docker 不能替代宿主机隔离：Docker daemon、镜像供应链、内核漏洞和恶意依赖仍属于风险面。
对高度不可信的代码，应使用单独的虚拟机或受管 CI 环境，并采用项目专用、预先审计的镜像。

## 审批与路径保护

以下类别默认必须由人工审批后才会执行：

- Git 写操作；
- 删除性命令和识别到的数据库 `DROP` / `DELETE` / `TRUNCATE`；
- 网络命令；
- `sudo` / `doas` 等特权命令；
- Shell 解释器调用，防止以 `curl | sh` 等方式绕过无 Shell 的工具接口。

文件工具在解析路径后仍要求目标位于给定 workspace 内，并使用不区分大小写的规则拒绝访问上述
敏感文件、私钥/证书后缀、Git 元数据和 `.operant` 本地权威运行数据。`.venv`、`node_modules` 与
普通缓存只从 Docker/Evaluation 隔离副本排除，不作为文件工具的通用禁读目录，避免误伤用户明确放在
workspace 内的依赖源码；该检查也不等于对任意宿主命令参数的完整约束，所以不可信任务必须选择
Docker Runner。

## 失败、取消与审计

- `run_command` 只接受参数数组，不进行字符串拼接或隐式 Shell 执行；
- stdout、stderr 和 Git diff 会截断并标记是否截断；
- 测试失败被压缩为结构化反馈，最多保留 12,000 个字符的关键信息；
- 连续两次相同测试失败签名会产生 `agent.no_progress` 并停止，避免无界修复循环；
- Provider 异常只持久化经过清洗的错误类型，不保存可能含上游响应或请求信息的原始异常文本；
- Session Event 会记录角色、角色版本、模型、Provider、effort、工具、审批、纠错、usage 和耗时事件；
- Workflow Run/Event 会记录任务阶段、角色、Session、模型输出摘要和工具结果，因此本地 SQLite 本身
  属于敏感运行数据，不应上传到公共位置；
- Trace JSONL 默认不导出完整任务正文、workspace 绝对路径、模型消息或工具结果，只保留聚合指标、
  长度和必要标识。脱敏 Trace 仍可能暴露项目结构与运行模式，分享前仍需复核。

日志、Role Profile、Role Snapshot 和 Git 文件中只能保存 `secret_ref`（环境变量名），不得保存
真实 API Key、Token、Cookie 或密码。

## Workflow 恢复与 Memory

- Workflow 只从已持久化的阶段边界恢复，不把客户端断线等同于模型执行中断；
- 如果上次进程留下 Coder 已开始但没有确定结果，任务会进入 `manual_reconcile_required`，默认不重放
  写阶段。用户必须先核对 workspace，再显式允许 Coder 重放；
- 待审批工具调用的进程内 `Future` 目前不能跨进程恢复，重启后应重新运行安全阶段或人工处置；
- 自动召回只读取与绝对 workspace 精确匹配的 active Project Memory，不跨项目注入 Episodic 总结；
- 只有可识别且成功的安全测试命令可自动成为 active Project Memory。模型生成的 Explorer 项目结构/
  编码约定摘要与 Coder 总结分别默认是 candidate Project/Episodic Memory，需要人工确认；
- Memory 更新会新建不可变版本，不覆盖来源历史。SQLite Memory 内容可能包含项目事实，也应按本地
  项目数据保护。

## Evaluation Runner 边界

- Evaluation Suite 是本地、受信任的评测定义。它只能声明白名单中的测试或静态检查参数数组，
  不接受 Shell 语法、URL、环境变量赋值、绝对路径、路径逃逸或凭据形态的参数；执行时使用
  `create_subprocess_exec`，不会隐式启动 Shell。
- 每个 Case × Variant × repetition 使用独立 artifact workspace。复制时排除 `.env*`、
  `secrets.json`、`.operant`、虚拟环境、依赖缓存和常见构建缓存；指向源 workspace 外部的软链接
  不会被跟随，并会作为环境事实记录。
- 外部验证命令在 artifact workspace 内以新的进程组运行；超时会终止整个进程组。持久化结果只保存
  argv、退出码、耗时、输出长度、截断标记与输出哈希，不保存原始 stdout/stderr。
- Evaluation Result 保存实际 Role/Model/Prompt hash、Memory 引用、Workflow、环境、变更路径、Trace
  指针和本地 artifact 引用。API/CLI 的 Result 输出会移除本地 workspace 绝对路径，但 SQLite 与
  artifact 仍可能包含项目代码、模型输出和运行事实，必须按敏感项目数据保护。
- 每个组合在执行前先创建唯一 Pending Result。正常路径只允许一次 `Pending → 单一终态`；取消、流关闭
  或进程重启把遗留 Pending 原地更新为 Interrupted。Interrupted 只保留计划身份和 artifact namespace，
  清空实际快照、指标、验证、变更和 Trace 引用，避免把结果未知伪装为失败或成功。
- Evaluation Runner 的外部验证当前使用 Host 进程，不是操作系统级沙箱。Suite 的白名单只能限制
  入口命令，不能证明被测依赖可信；不可信 fixture 或依赖仍应放入专用容器、虚拟机或受管 CI。
- Eval 模式会禁用 Workflow 的 Memory 候选回写；Memory 关闭时不查询项目知识，开启时只读取源
  workspace 精确作用域的 active Project Memory，并把实际引用版本与哈希写入结果快照。

## 受控 Skill Discovery

Skill Discovery 只接受 Core 启动时配置的有界受信根 reference，API 调用者不能提交任意宿主
路径扩大扫描范围。默认应用从 `OPERANT_SKILL_ROOTS_JSON` 读取 reference→绝对路径映射；MCP stdio
从 `OPERANT_MCP_WORKSPACE_ROOTS_JSON` 读取独立映射。API 只投影 reference，不返回宿主路径。每个根
必须是绝对、已存在、非软链接真实目录，并用 device/inode 去重；MCP 额外拒绝文件系统根和用户 Home。

发现器只检查根和一层子目录中的 `SKILL.md`。它从重新验证 identity 的 allowlist root FD 开始，
对候选、`scripts/`、`references/` 与嵌套目录逐层使用 dir-fd + no-follow 打开，并在读取后复核
device/inode/size/mtime/ctime 和父目录 path binding；根或任一父目录被替换时丢弃该根暂存的全部候选。
缺少这些安全原语的平台直接拒绝发现。root entry 与每个候选跨 resource tree 的 entry 在枚举时消耗
独立预算，达到 cap+1 就在排序/stat 前停止；候选数、层级、文件数、单件/总字节、frontmatter/body
和 JSON 集合也都有上限。仅允许简单、白名单 frontmatter，拒绝 YAML tag/anchor/alias/调用或变量替换形状。

发现和持久只产生 `untrusted_candidate` 及 manifest/resource hash，不会因为候选位于“受信根”就自动
信任内容、安装 Skill、加载代码或执行脚本。受信根只限定可见范围，不是对候选内容的安全背书。

## MCP 边界

MCP Server、工具描述、JSON Schema、工具输入和结果全部是不可信输入。MCP Server 本身不是沙箱、
Policy 或权限边界；“连接成功”也不等于任何工具获得执行权。

当前支持：

- `stdio`：显式 argv 只在 operator allowlist 中的 workspace 与 digest-pinned Docker 镜像上启动，
  不经 Shell、不接收 environment Secret、不继承 Core 环境，也不挂载原 workspace。过滤快照只读挂载，
  容器固定 `--pull never --network none --cap-drop ALL --read-only`、no-new-privileges、UID/GID、CPU/内存/
  PID 与快照项数/字节上限；快照复制用目录 FD 锚定和 no-follow 打开，拒绝软链接、非普通文件及
  文件/目录替换竞态；Docker 或镜像不可用时明确失败，不回退 Host；
- `legacy_sse`：兼容旧 MCP SSE 接收流 + POST 消息端点。它明确是 legacy transport，不是新推荐的
  远程安全协议。默认拒绝 redirect、userinfo、query/fragment、非 HTTP(S)、不安全 HTTP 和未明确允许的
  loopback HTTP；从 SSE 推导的 POST 端点还必须与已校验端点保持安全绑定。

两种 transport 都限制启停/请求超时、frame/Schema/stderr 大小、工具数、JSON 深度/总项/字符长度、
request ID 和 content type。`initialize`/`tools/list` 的结果仅在通过明确白名单的递归有界 Schema
子集后持久为版本化工具快照；未知断言关键字会拒绝整个快照，不会静默放行。每次 `tools/call` 仍必须
命中快照、通过本地 Schema 验证、重算包含 Server 配置摘要的 Action Hash，并在
外部请求前经 Action Gateway 发放和一次消费精确 Capability Lease。发送前持久 durable receipt 并从
`reserved` 原子转到 `sent`；只有结果完成有界校验、按实际 Secret 再脱敏并持久后才进入 `completed`。
重复 completed 调用只回放持久结果；`sent` 或 `outcome_unknown` 不会自动重放，必须人工核对。Audit
只记录结果 hash/耗时/错误码，不保存原始参数和原始结果。

默认 balanced Policy 把隔离 stdio 启动/调用约束为 `process.exec.no_network` + `workspace.read`，可在
规则匹配时 ALLOW；legacy SSE 需要 `network.egress`，启动时的 endpoint/bearer 另需 `secret.use`，默认
为 ASK。ASK 只有在精确持久 Approval 决定后才能继续；DENY 永远不会被 Reviewer 或客户端覆盖。

MCP Server 启动还使用单独的 owner/token/fencing/TTL CAS。并发请求只有一个能从 stopped/failed 进入
starting 并启动 transport；请求取消会关闭 transport 并 fenced 地收口为 failed，过期 start lease 可由
新 owner 原子接管，旧 owner 不能提交 running 或工具快照。Core 重启会把没有进程权威的活跃生命周期
保守收口，不能把 SQLite 的 running 投影冒充仍在运行的进程。

## Scheduler 边界

Phase 5A Scheduler 只调度本地 SQLite 中已发布的 Graph Workflow Revision。它不是 Shell、不是任意函数执行器，
也不会给 Graph 中任意节点或 MCP 工具自动授权。Graph IR 里的 Timer 节点仍被 Compiler 拒绝；
Schedule Timer 是独立的单次绝对 UTC 触发。

Cron 只支持受限五段表达式和 IANA 时区，用 UTC occurrence 遍历并映射到本地时间，使 DST gap 不伪造
时刻、fold 中的两次真实发生保持可区分。停机后的 due occurrence 只按 `skip`、`fire_once` 或有界
`catch_up` 具象化。Schedule Version + occurrence 生成稳定幂等键，重复 tick 不会重复入队。

RunRequest、JobAttempt、重试次数、副作用开始点、DLQ、取消与 Graph Run 绑定均持久化。Scheduler Leader
仅负责把 due occurrence 物化到 Queue，Runtime Writer 仅负责 claim/dispatch。两个全局租约和每个 Job Lease
都绑定 owner、随机 token、单调 fencing 和 TTL；旧、过期或不匹配的执行者不能续租、取消或提交。
Job 续租不得超过 Runtime Writer 到期时间，claim 在同一事务中强制 Schedule `concurrency_limit`。
当前是单 Leader/单 Writer 模型，不是多 Writer 或高可用集群。

Worker 在调用外部 Gateway 前先持久 `side_effect_started`。可确定失败用有界指数退避，达 `max_attempts`
后进 DLQ；DLQ 只能通过显式、幂等 replay 创建一个指向原请求的新事实。已开始的非幂等
dispatch 如果 lease 过期或返回结果未知，只能进 `manual_reconcile_required`，不自动 retry/replay。

Scheduler 派发本身还要经 Phase 4 Policy/Capability/Audit；后台调度不会等待或自行批准 ASK。
`scheduler_graph_dispatches` 在创建 Graph Run 前保留 RunRequest/幂等键/Action Hash 绑定：已完成绑定
重放返回同一 Graph Run；pending 绑定按稳定 Graph Run ID 恢复已提交 Run，确定未提交时用原绑定安全
创建，只有冲突或无法确定的 create/start/完成异常才转人工核对。FastAPI 停机只释放当前
Coordinator 精确持有的租约；崩溃接管仍以 SQLite 已提交事实和 fencing 为准。

## Web 与 API 部署边界

`/web` 和 `/v1/*` 当前没有身份认证、CSRF 防护或多租户隔离。Web 页面虽然不使用 CDN，并通过
`textContent` 等 DOM API 展示模型输出，但这不构成网络访问控制。默认只能绑定受信任的本机地址；
不得直接暴露到公网、共享局域网或不受信任的反向代理后面。若必须远程访问，应由外层受信任网关
提供 TLS、强身份认证、来源限制和审计。

Phase 45 的 Policy、Capability、Skill、MCP 和 Scheduler API 同样不构成身份认证。SQLite 中的 Action、
Audit、MCP 配置引用、Schedule/Queue/Attempt 和 Graph 绑定均是敏感本地运行事实，不应上传或公开。
Phase 5B 的设备配对和端到端加密只保护 Remote Control 协议，不会自动给其余 `/v1/*` 增加身份认证。
OAuth、CSRF 防护、TUI、Tauri、完整 Remote PWA/WSS 和允许公网暴露的接入网关仍未实现。

## Phase 5B Remote Control 与 Relay 边界

当前实现把“用户从远程设备操控本地 Core”和“Core 在远程主机执行”拆成两个独立领域。Remote
Control 使用 `HostInstance`、`RemoteDevice`、`RemoteSession` 和 `EncryptedRemoteCommand`；Remote
Execution 使用 Target/Lease/Job/Result，不共享含糊的 Worker 状态机。本地 Core 与 SQLite 始终是
Thread、Workflow、Approval、Audit 和恢复权威。

已实现的单 Host MVP 包括本机显式启用、一次性短时配对、每设备 Ed25519/X25519 身份、
ChaCha20-Poly1305 会话加密、独立 Scope/撤销、Command TTL/nonce/签名/幂等/Host Ack、Cursor 查询和
Host Connector。私钥及 Session Key 只写入 `0600` 本地 key store，SQLite 只保存公开密钥和 key ref；
key store 采用原子替换和文件锁串行并发写。Pairing Challenge 持久绑定本机预授 Scope，设备不能用
一次性码扩大权限；配对票据只返回一次并带 `no-store`，durable Command Receipt 不保存其正文；丢失
响应时必须创建新票据，不能回放 Secret。

自托管 Relay MVP 只接受单独配置的 Bearer 运维鉴权，只保存严格 TTL/大小上限的 opaque ciphertext、
nonce 和投递状态。它不解密业务消息，不持有设备/Host 私钥，不运行 Agent/Tool，不裁决 Policy 或
Approval。Relay delivery/ack 只表示信封流转，不是 Host Ack；Host Connector 解密后仍重验设备签名、
Session/Scope/TTL 和本地 Action Gateway。当前 transport 是 HTTPS polling API，不是 WSS/直连 Gateway
或经过公网部署审计的完整套件，因此 `/web` 与普通 `/v1/*` 仍不得直接暴露到公网。

实现强制满足：

- Remote Control 默认关闭，由本地用户通过短时码/二维码显式配对每个设备；
- 每个设备使用独立身份与最小 Scope，可撤销，并在本地提供全部断开和紧急关闭入口；
- Remote Client 与 Host 之间使用应用层端到端加密；Relay 不读取会话、代码、Diff、命令、Artifact
  正文、模型消息或 Secret；
- Remote Command 带请求 ID、幂等键、Host/Device Identity、过期、nonce、签名和 Host Ack；Relay
  收到消息不等于 Core 已接受动作；
- RemoteDevice Scope、远程用户操作和 Relay 消息都不能覆盖本地 Policy `DENY`；解密后的动作必须
  命中 Host 预注册的 `(tool, operation) → capabilities` 精确映射，observe 不能和副作用 Capability
  混用；Approval 继续绑定
  精确 Action Hash、Target、Policy Version 和有效期；
- Client、Host 或 Relay 断线不改变本地 Run；重连后从本地 Event Cursor 和 Query Projection 校正；
- Remote Command 的 `received`/`accepted` 都绑定 execution owner 与可续租 TTL；其他 Core 不会收口
  尚存活的 owner，只有租约过期项才分别保守拒绝或进入 `outcome_unknown`。人工核对除本机鉴权外，
  还绑定原 Action/Target 通过 Action Gateway 并消费一次性 Capability Lease，再以 CAS 落到已知终态；
  旧 owner 的晚到结果不能覆盖，不自动重放；
- Host 离线时不在 Relay 无限期排队未来副作用；短期加密 Envelope 必须有严格 TTL 和大小上限；
- Relay 不保存模型凭据、SSH 私钥或项目数据，也不运行 Agent、工具、Browser、Computer 或 Workflow；
- Relay 与 Remote Execution Target 即使部署在同一云主机，也必须使用独立用户/容器、目录、凭据和
  网络权限；公网 Relay 默认不得拥有 Workspace、Docker Socket 或执行器权限。

Operant 2.0 不以此为由建设 SaaS、多个人类用户协作、多租户、分布式 Core、外部恢复数据库或高可用
控制面。远程操控只是同一用户跨设备连接自己的本地 Core。

## Phase 5B Remote Execution Target 与 Browser/Computer

Remote Execution Target 是本地 Controller 发起、远端受租约约束执行的独立能力。注册只保存
`endpoint_ref`/`credential_ref`，不保存 endpoint credential 真值；Target Identity、Heartbeat、
Capability Manifest、Workspace/Artifact Namespace、Job/Result 和状态仍由本地 SQLite 裁决。Target
Lease token 只在首次 `no-store` 响应返回，SQLite 只保存 SHA-256；所有 poll/complete/renew/release
都在事务中核对 target、lease ID、token hash、fencing、TTL 和在线状态。

每个外部动作先经 Action Gateway，Capability/operation 还必须同时存在于 Target Manifest。Job 参数、
并发数、Artifact bytes 和 checksum 都有上限。Browser/Computer 动作采用 observe-before-act：Action
绑定未过期 observation hash、精确 target ref 与递归 precondition，成功结果再核对 postcondition；
观察或目标漂移会失败关闭。新 owner 获取 Target Lease 前会先收口过期 lease 的残留 Job；已运行的
非幂等 Job 在断线、租约失效或结果未知时进入
`manual_reconcile_required`，不得自动重放。当前 connector 是可注入边界和确定性内存实现，生产网络
connector、凭据下发隔离和真实浏览器/桌面驱动仍需部署方实现与单独审计。

## Phase 6 Multi-Writer 边界

多个 Writer 只能写不同 `WriterWorkspace`；Graph Compiler 要求每个写节点声明唯一 writer key、
独立 isolation ref 和不重叠 ownership paths，并由覆盖全部 Writer 的显式 Merge Node 收口。
Writer Lease 使用 token hash、单调 fencing 和 TTL，过期、释放或旧 fencing 无法发布 Artifact 或参与
Merge。Patch/Commit Artifact 必须绑定冻结 base、changed paths、SHA-256 和测试证据；冲突按路径、
ownership 与 stale base 确定性持久化，不静默覆盖。

可信 Git adapter 只解析管理员通过 `OPERANT_MULTIWRITER_ROOTS_JSON` 映射的绝对 Git worktree；发布和
Merge 时均重新核对 commit ancestry 或 patch、SHA-256、changed paths 和 ownership，patch 在同一次
验证中读成有大小上限的不可变内存快照，后续不再按可变路径打开。target 必须是另一个干净 worktree
且 HEAD 等于冻结 base；SQLite partial unique 约束只允许同一 target 有一个 `RUNNING` Merge，并用
可续租 execution owner + CAS 防止旧进程覆盖恢复权威。Git adapter 先在私有 checkout 计算预期 tree，
目标 index 必须精确匹配，再用该固定 tree 创建 commit 并以 old HEAD 做 `update-ref` CAS。检测到外部
干扰或 owner 崩溃时保留隔离 worktree 现场并进入 `outcome_unknown`，只允许本机鉴权且经过 Action
Gateway 的人工 reconcile，不执行可能删除外部数据的 reset/clean，不自动重放。最终 merge 要通过
`workspace.write + git.commit` 的 Action Gateway，默认需要精确 Approval。普通确定失败的回滚仅允许
作用于管理员映射的专用、可重建隔离 target，不能指向用户共享 checkout。当前未实现 Container Writer
的创建/挂载/销毁 adapter，也未把
多 Writer 描述为分布式 Core 或高可用。

## 验收方式

单元测试始终验证 Docker 命令参数、过滤快照、路径保护、审批、超时、测试反馈与无进展停止。
真实 Docker 集成测试只有在本机存在 `docker` 且设置 `OPERANT_DOCKER_TEST_IMAGE` 时运行；未满足
该条件时会跳过，而不是把静态测试描述为容器运行成功。

Evaluation Runner 的自动化测试使用隔离 fixture、确定性 Provider 和 Fake Runner，覆盖快照、
SQLite 契约、超时、软链接、指标、根因分类及 CLI/API 脱敏。它们不等同于真实 Provider、真实费用、
Exp 19—24 实验结论或“真实模型 + Docker Coder”的联合验收。

Phase 4/5A 自动化测试覆盖 Policy/Capability/Secret/Audit、Skill 路径与读取竞态、stdio/legacy SSE MCP、
Docker argv/快照边界与替换竞态、递归 Schema/未知关键字拒绝、User/Reviewer Approval timeout、
start 取消/过期接管、receipt 并发与 unknown、Cron/Timer/DST/misfire、
Queue/Lease/fencing/retry/DLQ/manual reconcile、Scheduler→Graph 幂等绑定、SQLite v10/v11/v12
升级/回滚与 32-operation 生成 Client。这些确定性测试不等于真实第三方 MCP Server 安全审计、
长时稳定 Scheduler 运维、多进程压力/故障演练或真实模型联合验收。

2026-08-22 的六角色真实模型 Workflow 在可信的临时 fixture 中使用 Host Coder 完成，并由模型外
unittest 和 diff 再次复核。它证明编排、持久化、Trace 和 Memory 主链路可工作，不证明 Host Runner
对不可信项目安全，也不等同于“真实模型 + Docker Coder”的同一次端到端验收。
