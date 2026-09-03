# Operant 安全边界

> 最后更新：2026-09-03

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
返回 ALLOW 或 DENY。超时、异常、非法 JSON 或仍返回 ASK 都固定收口为 DENY。当前 Phase 45
MCP/Scheduler API 没有独立的持久 Approval continuation；既有 Session Tool Approval 不能被当作 Phase 45
ASK 的续传。因此这些后台或外部副作用在 ASK/DENY 时都会 fail-closed，不会静默自批。

Capability Lease 只能从精确 ALLOW 的 Action 发放，并绑定 Action Hash、principal、Capability、target、
workspace、Policy Version、TTL、约束和最大使用次数。消费使用 SQLite CAS 重新核对这些字段、
过期、撤销和用量；旧、越目标、超量或被撤销 Lease 均失败关闭。

Secret Broker 只在精确 ALLOW 且包含 `secret.use` 的 Action 执行时解析已声明 `secret_ref`，并生成
短 TTL、单目标 Secret Lease。真实 Secret 值只进入目标执行环境，不写入 SQLite、API、Policy 解释、
Audit 或错误；返回输出还会按实际注入值再次脱敏。

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
路径扩大扫描范围。每个根必须是绝对、已存在、非软链接真实目录，并用 device/inode 去重。

发现器只检查根和一层子目录中的 `SKILL.md`，对根、候选、`scripts/`、`references/` 中的
每个组件拒绝软链接、路径逃逸和非普通文件。文件用 no-follow 打开，读取后复核 device/inode/
size/mtime，避免把替换竞态当作稳定快照。候选数、层级、文件数、单件/总字节、frontmatter/body
和 JSON 集合全部有上限；仅允许简单、白名单 frontmatter，拒绝 YAML tag/anchor/alias/调用或变量替换形状。

发现和持久只产生 `untrusted_candidate` 及 manifest/resource hash，不会因为候选位于“受信根”就自动
信任内容、安装 Skill、加载代码或执行脚本。受信根只限定可见范围，不是对候选内容的安全背书。

## MCP 边界

MCP Server、工具描述、JSON Schema、工具输入和结果全部是不可信输入。MCP Server 本身不是沙箱、
Policy 或权限边界；“连接成功”也不等于任何工具获得执行权。

当前支持：

- `stdio`：显式 argv 直接启动，不经 Shell；子进程不继承 Core 的整个环境，只获得显式映射的
  environment reference。但它仍是宿主机进程，未受 Docker 或 OS 网络沙箱隔离；
- `legacy_sse`：兼容旧 MCP SSE 接收流 + POST 消息端点。它明确是 legacy transport，不是新推荐的
  远程安全协议。默认拒绝 redirect、userinfo、query/fragment、非 HTTP(S)、不安全 HTTP 和未明确允许的
  loopback HTTP；从 SSE 推导的 POST 端点还必须与已校验端点保持安全绑定。

两种 transport 都限制启停/请求超时、frame/Schema/stderr 大小、工具数、JSON 深度/总项/字符长度、
request ID 和 content type。`initialize`/`tools/list` 的结果经验证后持久为版本化工具快照。每次
`tools/call` 仍必须命中快照、通过本地有界 Schema 验证、重算 Action Hash，并在外部请求前经
Action Gateway 发放和一次消费精确 Capability Lease。结果先有界校验和脱敏，Audit 只记录结果 hash/
耗时/错误码，不保存原始参数和原始结果。

默认 balanced Policy 把 stdio 启动/调用视为 `process.exec`，把 legacy SSE 视为 `network.egress`，显式
Secret Ref 另需 `secret.use`；这些默认都是 ASK。因当前 Phase 45 无独立审批 continuation，ASK 和 DENY
都会在启动或工具调用前失败关闭。

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
重放返回同一 Graph Run，pending 绑定的结果未知不会创建第二个 Run。FastAPI 停机只释放当前
Coordinator 精确持有的租约；崩溃接管仍以 SQLite 已提交事实和 fencing 为准。

## Web 与 API 部署边界

`/web` 和 `/v1/*` 当前没有身份认证、CSRF 防护或多租户隔离。Web 页面虽然不使用 CDN，并通过
`textContent` 等 DOM API 展示模型输出，但这不构成网络访问控制。默认只能绑定受信任的本机地址；
不得直接暴露到公网、共享局域网或不受信任的反向代理后面。若必须远程访问，应由外层受信任网关
提供 TLS、强身份认证、来源限制和审计。

Phase 45 的 Policy、Capability、Skill、MCP 和 Scheduler API 同样不构成身份认证。SQLite 中的 Action、
Audit、MCP 配置引用、Schedule/Queue/Attempt 和 Graph 绑定均是敏感本地运行事实，不应上传或公开。
本阶段不包含 OAuth、Remote/Relay、TUI、Tauri、Browser/Computer 工具或通用多 Writer；相关页面、
模型或目标文档不是已实现安全能力。

## 目标 Remote Control 边界（尚未实现）

Operant 2.0 的目标远程能力会把“用户从手机/浏览器操控本地 Core”和“Core 在远程主机执行”拆成
两个独立能力。计划中的自托管 Relay 只负责连接、路由、最小设备治理和可选通知，本地 Core 与
SQLite 仍是 Thread、Workflow、Approval、Audit 和恢复权威。

在 Host Connector、Remote Gateway、RemoteDevice 配对、端到端加密、Command 幂等/签名、Action
Hash、设备撤销和安全审计真正实现并通过测试前，**不得**把当前 `/web` 或 `/v1/*` 直接接到公网 Relay，
也不得把“外层反向代理有密码”描述为目标 Remote Control 已经完成。

目标实现必须满足：

- Remote Control 默认关闭，由本地用户通过短时码/二维码显式配对每个设备；
- 每个设备使用独立身份与最小 Scope，可撤销，并在本地提供全部断开和紧急关闭入口；
- Remote Client 与 Host 之间使用应用层端到端加密；Relay 不读取会话、代码、Diff、命令、Artifact
  正文、模型消息或 Secret；
- Remote Command 带请求 ID、幂等键、Host/Device Identity、过期、nonce、签名和 Host Ack；Relay
  收到消息不等于 Core 已接受动作；
- RemoteDevice Scope、远程用户操作和 Relay 消息都不能覆盖本地 Policy `DENY`；Approval 继续绑定
  精确 Action Hash、Target、Policy Version 和有效期；
- Client、Host 或 Relay 断线不改变本地 Run；重连后从本地 Event Cursor 和 Query Projection 校正；
- Host 离线时不在 Relay 无限期排队未来副作用；短期加密 Envelope 必须有严格 TTL 和大小上限；
- Relay 不保存模型凭据、SSH 私钥或项目数据，也不运行 Agent、工具、Browser、Computer 或 Workflow；
- Relay 与 Remote Execution Target 即使部署在同一云主机，也必须使用独立用户/容器、目录、凭据和
  网络权限；公网 Relay 默认不得拥有 Workspace、Docker Socket 或执行器权限。

Operant 2.0 不以此为由建设 SaaS、多个人类用户协作、多租户、分布式 Core、外部恢复数据库或高可用
控制面。远程操控只是同一用户跨设备连接自己的本地 Core。

## 验收方式

单元测试始终验证 Docker 命令参数、过滤快照、路径保护、审批、超时、测试反馈与无进展停止。
真实 Docker 集成测试只有在本机存在 `docker` 且设置 `OPERANT_DOCKER_TEST_IMAGE` 时运行；未满足
该条件时会跳过，而不是把静态测试描述为容器运行成功。

Evaluation Runner 的自动化测试使用隔离 fixture、确定性 Provider 和 Fake Runner，覆盖快照、
SQLite 契约、超时、软链接、指标、根因分类及 CLI/API 脱敏。它们不等同于真实 Provider、真实费用、
Exp 19—24 实验结论或“真实模型 + Docker Coder”的联合验收。

Phase 4/5A 自动化测试覆盖 Policy/Capability/Secret/Audit、Skill 路径与读取竞态、stdio/legacy SSE MCP、
Cron/Timer/DST/misfire、Queue/Lease/fencing/retry/DLQ/manual reconcile、Scheduler→Graph 幂等绑定、SQLite v10/v11
升级/回滚与 27-operation 生成 Client。这些确定性测试不等于真实第三方 MCP Server 安全审计、
长时稳定 Scheduler 运维、多进程压力/故障演练或真实模型联合验收。

2026-08-22 的六角色真实模型 Workflow 在可信的临时 fixture 中使用 Host Coder 完成，并由模型外
unittest 和 diff 再次复核。它证明编排、持久化、Trace 和 Memory 主链路可工作，不证明 Host Runner
对不可信项目安全，也不等同于“真实模型 + Docker Coder”的同一次端到端验收。
