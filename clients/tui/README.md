# Operant 终端客户端

TUI 是可选的 Textual 界面，通过生成的 Python Client 访问 Core，不直接读写数据库或运行工具。
当前主线默认进入会话工作台，提供新建会话、父子历史、输入、子任务、定向消息和上下文操作；
也保留 Graph 监控、审批、恢复/取消与经验授权页面。

## 从源码运行

在仓库根目录、一个终端中启动 Core：

```bash
uv sync --frozen --extra dev
uv run operant serve --host 127.0.0.1 --port 8000
```

再在第二个终端、同一仓库根目录运行：

```bash
uv sync --project clients/tui --frozen
uv run --project clients/tui --no-sync operant-tui --core-url http://127.0.0.1:8000
```

`clients/tui/pyproject.toml` 会使用当前源码树的 Core 依赖，避免误装不匹配的远端版本。
单独安装候选 wheel 时，也必须提供匹配版本的 `operant-agent`。连接 URL 不得包含用户名、密码或密钥。

## 常用操作

启动后展开“新建会话”，选择已注册工作区和角色。会话工作台中：

- `Enter` 打开会话，`Space` 折叠历史树。
- `Ctrl+L` 聚焦输入，`Ctrl+R` 刷新；`Esc` 返回 Graph 监控。
- 输入 `/` 查看 Core 提供的正式命令。引用与上下文操作在折叠面板中；清理需要确认且保留历史，活动运行中不能改上下文基线。
- 子 Agent 默认继承模型、权限、预算和工作区；定向消息需选择收件人。断线或未知写结果需刷新核对，不自动重放。
- “文件与代码预览”读取注册工作区内的有界 UTF-8 正文和 Git Diff；正文预览会预填文件引用，仍须显式点击“预览并附加引用”。超长内容显示截断提示。工件引用须先从当前会话的授权列表选择，列表显示来源、摘要与哈希，附加时 Core 再复核权限和版本。
- “配置继承”显示有效值、逐字段来源和修订号；选择全局、项目、工作区或角色覆盖层后编辑 JSON，保存或恢复继承。变更只作用于新会话，活动会话仍用冻结快照。
- “Goal / Plan”可创建、编辑目标与计划，跟踪清单状态。完成 Goal 需结果引用，完成清单项需证据引用；状态与修订由 Core 校验。
- “交互终端”在当前会话的可信工作区启动 Host shell，继承本机用户权限。Action Gateway 只检查创建；若要求人工审批，可在 TUI 查看目标并显式批准或拒绝，再用同一请求键重试。后续输入不逐条审批；`Ctrl+Q` 关闭。连接令牌通过 WebSocket 子协议发送，不进入 URL。断线后 Core 尝试回收进程，输入不会自动重放；若清理未确认，TUI 明示终端 ID 并要求人工核对。

Graph 监控与其他页面的快捷键：

- `Tab` / `Shift+Tab`：移动焦点。
- `Alt+1/2/3`：切换会话、运行和检查器区域。
- `Alt+4`：打开经验与授权页面；`Alt+5`：返回会话工作台。
- `?`：查看帮助；`Esc`：关闭当前操作；`q`：退出。

按页面输入对应 Session 或 Graph Run ID，读取审批、刷新状态或订阅事件。取消需要再次确认；
网络、协议或权限错误会显示出来，不回退到演示数据。运行与授权结果以 Core 返回的状态为准。

## 定向测试

```bash
uv run --project clients/tui --no-sync python -m unittest discover -s clients/tui/test
```

当前说明对应已合并会话工作台的主线；旧 Beta 标签不包含这些新增操作。
安装、版本和安全说明见 [根 README](../../README.md) 与 [SECURITY](../../SECURITY.md)。
