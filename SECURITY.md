# Operant 安全边界（草案）

> 最后更新：2026-08-19

Operant 会把模型输出视为不可信输入。模型只能请求由当前 `RoleSnapshot` 的 Tool Policy
允许的工具；Runtime 和工具层会再次校验，不把“模型遵守提示词”当作安全边界。

## 默认角色与命令执行

- Planner、Explorer 和 Reviewer 只获得读取、搜索和 `git_diff` 工具，不能写文件或执行命令。
- 新初始化的默认 Coder 可改写 workspace，但其 `run_command` 使用 Docker Runner。
- Docker 不可用、未启动或所需镜像不存在时，命令会明确失败，不会静默回退到宿主机执行。
- `host` Runner 仅用于用户明确认为可信的本地 workspace；它不是操作系统级沙箱。

角色快照会保存命令执行策略。修改角色后，既有 Session 不会继承新权限或新限制。

## Docker Runner 的边界

Docker Runner 不直接挂载用户 workspace，而是先创建过滤后的临时快照。快照排除：

- `.git`、`.env`、`.env.local`、`secrets.json`；
- `.operant`、`.venv`、`node_modules`、Python/pytest 缓存。

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

文件工具在解析路径后仍要求目标位于给定 workspace 内，并拒绝访问敏感文件和 Git 元数据。该
检查不等于对任意宿主命令参数的完整约束，所以不可信任务必须选择 Docker Runner。

## 失败、取消与审计

- `run_command` 只接受参数数组，不进行字符串拼接或隐式 Shell 执行；
- stdout、stderr 和 Git diff 会截断并标记是否截断；
- 测试失败被压缩为结构化反馈，最多保留 12,000 个字符的关键信息；
- 连续两次相同测试失败签名会产生 `agent.no_progress` 并停止，避免无界修复循环；
- Session Event 会记录角色、角色版本、模型、Provider、effort、工具、审批和纠错事件。

日志、Role Profile、Role Snapshot 和 Git 文件中只能保存 `secret_ref`（环境变量名），不得保存
真实 API Key、Token、Cookie 或密码。

## 验收方式

单元测试始终验证 Docker 命令参数、过滤快照、路径保护、审批、超时、测试反馈与无进展停止。
真实 Docker 集成测试只有在本机存在 `docker` 且设置 `OPERANT_DOCKER_TEST_IMAGE` 时运行；未满足
该条件时会跳过，而不是把静态测试描述为容器运行成功。
