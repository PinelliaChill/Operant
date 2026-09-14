# 参与 Operant

欢迎修复问题、补充文档和改进使用体验。较大的功能或公共接口改动，请先开 Issue 说明要解决的问题，
避免花时间实现不适合当前方向的方案。

## 报告问题

请提供所用提交或版本、操作系统、复现步骤、预期结果和实际结果。
日志只保留复现所需部分，删除密钥、个人路径、对话和私有代码；不要上传 `.env`、数据库或整个 `.operant/`。
安全问题的处理方式见 [SECURITY.md](SECURITY.md)。

## 本地开发

从自己的 Fork 新建工作分支，在仓库根目录安装锁定依赖：

```bash
uv sync --frozen --extra dev
```

Python 服务、协议、数据或运行行为有变化时，执行：

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv lock --check --offline
git diff --check
```

修改 React 界面时，使用 Node.js 22.6+ 并执行：

```bash
npm ci --prefix clients/gui
npm run test --prefix clients/gui
npm run typecheck --prefix clients/gui
npm run build --prefix clients/gui
```

同时检查涉及的页面、窄屏、键盘焦点、错误与断线表现。修改 TUI 或桌面壳时，
请查看对应目录说明，并验证实际客户端。协议与 SDK 的修改应从公共 Schema 重新生成，不能只手改生成文件。

纯文档改动只需回读内容、核对命令与链接、检查敏感信息，并运行 `git diff --check`。
不用为文档改字重复运行整个产品测试。

## 提交 Pull Request

- 一次改动集中解决一个问题，避免夹带无关格式化或依赖升级。
- 写清楚解决了什么问题、用户会看到什么变化，以及实际做了哪些验证。
- 改变功能或安全边界时，同步更新 [当前实现说明](docs/PROJECT_ARCHITECTURE.md)。
- 没有验证的环境直接写明。Mock 测试、跳过的 Docker 测试和真实模型调用要分开报告。
- 提交前确认没有运行数据、个人资料和凭据；保留所引用第三方代码的许可与来源说明。

提交贡献时，请确认你有权提供相关内容，并愿意按项目的 [Apache-2.0 许可证](LICENSE) 提供贡献。
