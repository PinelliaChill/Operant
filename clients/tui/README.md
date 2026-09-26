# Operant 终端客户端

TUI 是可选的 Textual 界面，通过生成的 Python Client 访问 Core，不直接读写数据库或运行工具。
当前主线提供会话审批、Graph 运行状态与事件订阅、恢复/取消，以及经验与授权页面。

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

- `Tab` / `Shift+Tab`：移动焦点。
- `Alt+1/2/3`：切换会话、运行和检查器区域。
- `Alt+4`：打开经验与授权页面。
- `?`：查看帮助；`Esc`：关闭当前操作；`q`：退出。

按页面输入对应 Session 或 Graph Run ID，读取审批、刷新状态或订阅事件。取消需要再次确认；
网络、协议或权限错误会显示出来，不回退到演示数据。运行与授权结果以 Core 返回的状态为准。

## 定向测试

```bash
uv run --project clients/tui --no-sync python -m unittest discover -s clients/tui/test
```

当前说明对应根 README 标注的主线范围，不包含尚未合并的会话式工作台操作。
安装、版本和安全说明见 [根 README](../../README.md) 与 [SECURITY](../../SECURITY.md)。
