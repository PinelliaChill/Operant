# Operant Textual TUI

The TUI is an optional client. Its wheel depends on the exact matching
`operant-agent` release, which ships the generated Python clients from
`sdk/python_client`; it does not read SQLite, logs, or the Core event bus.

```bash
python -m pip install -e clients/tui
operant-tui --core-url http://127.0.0.1:8000
```

`Textual` and `Rich` are optional UI dependencies of the main Operant Core and
are installed only with this standalone client package. The URL is not allowed
to contain credentials. Runtime secrets remain Core-owned `secret_ref` values.

启动后默认进入会话工作台。展开“新建会话”选择已注册工作区和角色；历史树按父子会话排列，
Enter 打开会话，Space 折叠，Ctrl+L 聚焦输入，Ctrl+R 刷新。Esc 返回 Graph 监控，Alt+5 返回工作台。

输入 `/` 可查看 Core 的正式命令；上下文详情、压缩、清理和文件/会话引用位于折叠面板内。
清理需再次执行确认，保留历史；运行中的上下文不能改动。引用先附加来源与有界快照，正文由模型按需读取。
子 Agent 默认继承当前模型、工具权限、预算和工作区；定向消息需选择收件人，取消作用于当前会话及后代。
断线或未知写入结果会保留最近投影与幂等键，必须刷新核对，不自动重放。
