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
