# Beta 2.0 候选包使用说明

本目录记录 B2-7 / MP-6。是否完成及验收范围以 [任务包](task-package.md) 为准。
候选交付不是正式发布；没有自动更新、Developer ID 签名、公证或 DMG 分发承诺。

## 安装与启动

候选包含 macOS Apple Silicon 桌面壳、Python Core 和可选 TUI。桌面壳需要单独启动配套 Core，
不能只复制 `.app` 就视为完整安装。底层包版本暂沿用 `0.1.0`，以候选清单中的 Git SHA 和
SHA-256 区分具体构建，不从这个版本号判断包含哪些 B2 功能。

优先在新目录和新数据库试用，先校验候选清单和文件摘要。候选包安装（在候选目录执行）：

```bash
shasum -a 256 -c SHA256SUMS
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python python/operant_agent-0.1.0-py3-none-any.whl
export OPERANT_DB_PATH=/绝对路径/试用目录/core.sqlite3
# 按项目 .env.example 设置模型环境变量，或在当前目录创建未提交的 .env。
.venv/bin/operant model discover
.venv/bin/operant serve --desktop
```

该安装方式需要取得 wheel 声明的依赖，并非完全离线安装器。源码方式：

```bash
uv sync --frozen --extra dev
# 按 .env.example 配置模型；不要把密钥放进数据库、命令行参数或分享给他人。
export OPERANT_DB_PATH=/绝对路径/试用目录/core.sqlite3
uv run operant model discover
uv run operant serve --desktop
```

Core 只监听本机 `127.0.0.1:8000`。启动本批 `Operant.app`，确认界面显示已连接。
保留 `--desktop`，它提供桌面壳所需的 Origin 配置。模型必须使用 discovery 返回的精确 ID。
可选 TUI 在另一终端连接同一 Core（下列 Python 3.11 组合已验证）：

```bash
uv venv --python 3.11 .venv-tui
uv pip install --python .venv-tui/bin/python python/operant_agent-0.1.0-py3-none-any.whl tui/operant_tui-0.1.0-py3-none-any.whl
.venv-tui/bin/operant-tui --core-url http://127.0.0.1:8000
```

## 记忆与日常操作

1. 新建项目并绑定绝对工作区。在插件页显式安装记忆插件，在项目中选择安装实例。
2. 默认插件 `memory-standard` 和笔记插件 `memory-notebook` 都使用 Core 的权限与正式发布头。
   未安装插件时仍可执行普通任务；强依赖记忆的流程会明确拒绝启动。
   `memory-notebook` 接受 `key=value` 或 `key: value`，按区分大小写的精确键查询；例如保存
   `build_tool=uv`，查询 `build_tool`。普通段落应使用默认插件。
3. 在知识页保存事实或提交修改提议。候选需经审阅才进入正式知识；来源、版本、条件和撤销状态仍由 Core 判断。
4. 在经验页从 procedure 形成 Skill 草稿，验证后发布。停用或回退都需要精确版本，Skill 不授予额外工具权限。
5. 关闭记忆会阻断新的召回和后台写入。已发送给模型的内容无法撤回；旧上下文受撤销污染时，需建立干净的新会话。

卸载时，`keep` 保留可定位的数据及必要元数据，`delete` 清理插件专属数据及 Core 托管范围。
二者都不是磁盘安全擦除；独立导出、备份、已生成 Skill 和外部副本需要分别管理。
共享消费者、活动 Run 或保留锁可能使清理处于 `partial` / `blocked`，按界面的原因处理后再继续。
来源删除会显示 tombstone，不代表历史从未存在。

升级后的旧记忆需要显式迁入。界面保留 `legacy_unverified` 和缺少来源等原因，不会把旧库中的
`active` 自动当作新插件的可信发布。先核实内容和来源，再按治理入口提交或确认；不能靠开关绕过。

## 升级与回退

正式用户数据库迁移不在本批授权内。本批只演练合成旧库的隔离副本。
升级实际数据前，停止 Core，保存完整数据库与关联工件/插件数据目录的备份，再制定具体恢复方案。
不要同时运行新旧二进制操作同一数据库，也不要只拷贝正在写入的 SQLite 主文件而遗漏 WAL。

运行固定插件包、配置、权限及知识快照；旧包不可用或权限已撤销时，恢复会明确失败。
关闭新检索或后台维护可作为功能回退，但不能恢复旧关键词自动发布、旧写旁路或失效权限。
数据库只支持既有的受限回退；非空新增表会拒绝降级。恢复旧版本应使用完整的旧备份，
不能让旧程序直接写新 Schema。

## 已知限制

- B2-4 的 Host 性能限制保留；本批对照不能证明所有任务都更快或更准。
- 模型未返回 usage 或价格时保持 unknown，不当作零成本。小样本真实对照只解释本批固定任务。
- Remote 只声明已验的本地协议、控制与适配边界，不宣称生产 HTTPS Target 或公网服务通过验收。
- Docker 条件 skip 不等于容器验收；未知写入结果必须人工核对，不能自动重放。
- GUI 断线、协议或 Cursor 错误会进入显式失败/只读状态。先刷新核对服务端状态，再决定是否重试。
- 保留历史安全扫描待核查事项；候选验收不等于清空所有依赖或 CodeQL 告警。
