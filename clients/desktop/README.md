# Operant 桌面客户端

这是基于 Tauri 2 的桌面壳，使用 `clients/gui` 的 React 界面。窗口管理和本机 Core 的连接由桌面壳负责，
模型、工具、审批、数据库和任务恢复仍由 Python Core 处理。

桌面壳需要匹配版本的 Core，不能只复制 `.app` 就假定已经完成安装。
当前构建目标是 macOS `.app`；尚无正式 Developer ID 签名、公证、DMG 或自动更新。

## 从源码启动（macOS）

准备 uv、Node.js/npm、Rust/Cargo 和 macOS 构建工具。以下命令从仓库根目录执行。
先在一个终端启动 Core：

```bash
uv sync --frozen --extra dev
OPERANT_SETUP_ALLOWED_ORIGINS_JSON='["http://127.0.0.1:3000"]' uv run operant serve --host 127.0.0.1 --port 8000 --desktop
```

在第二个终端、同一仓库根目录启动桌面开发窗口：

```bash
npm ci --prefix clients/gui
npm ci --prefix clients/desktop
npm run tauri --prefix clients/desktop -- dev
```

Tauri 配置会启动前端开发服务。`--desktop` 让 Core 接受固定的 Tauri 来源；已有 8000 端口服务若未带该参数，
即使网页可访问，桌面窗口也可能连接失败。请先核对服务归属，再决定是否停止旧服务，不要直接覆盖或强杀。

开发窗口通过 Vite 同源代理访问 Core，模型连接页只接受显式登记的本机来源；更换开发端口时同步修改
`OPERANT_SETUP_ALLOWED_ORIGINS_JSON`。发布壳仍使用固定 Tauri 来源，不需要登记开发端口。

## 构建候选

安装好上述依赖后，在仓库根目录执行：

```bash
cargo test --locked --manifest-path clients/desktop/src-tauri/Cargo.toml
npm run tauri --prefix clients/desktop -- build
```

配置中的构建钩子会先编译 GUI，产物位于 `clients/desktop/src-tauri/target/release/bundle/macos/`。
双击候选 App 前仍需启动匹配的 Core；若让桌面壳自行启动 Core，启动环境的 `PATH` 必须能找到 `operant`。
从终端能运行 `uv run operant`，不代表 Finder 启动的 App 也拥有同样的环境。

## 使用边界

- 首次使用在“连接模型”中选择服务商和模型；系统准备通用助手及个人工作区。演示切换位于开发设置。
- 本机能力启用后可选择“在新对话中使用”，先核对应用或完整网站范围。新对话使用专用控制会话；原手动操控会话须先结束。响应丢失时按原请求核对，勿重复开启。
- 使用稳定的运行目录并备份旧数据库；升级 App 不代表已迁移用户数据。
- 桌面壳只接受固定的本机 Core 地址，不从网页输入执行路径、命令或工作目录。
- 模型登录通过受限的 `open_model_oauth` 命令打开官方 OpenAI/Google 页面，回调须匹配当前 Core；不提供任意链接打开权限。真实账号授权结果见 [本轮验收](../../docs/design/onboarding-ux/acceptance.md)。
- 本机构建、安装验证和正式分发是不同阶段。版本范围见 [根 README](../../README.md)，安全边界见 [SECURITY](../../SECURITY.md)。
