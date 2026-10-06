# Operant 桌面客户端

这是基于 Tauri 2 的桌面壳，使用 `clients/gui` 的 React 界面。窗口管理和本机 Core 的连接由桌面壳负责，
模型、工具、审批、数据库和任务恢复仍由 Python Core 处理。

[Beta 2 自包含 DMG](https://github.com/PinelliaChill/Operant/releases/download/v0.1.0-beta.2/Operant-v0.1.0-beta.2-macos-arm64.dmg) 面向 macOS arm64，已包含 Python、匹配版本的 Core 与基础依赖。打开 DMG，将 App 拖入“应用程序”后启动即可；模型服务仍需自行配置。

DMG 启动器使用独立的 `~/Library/Application Support/Operant Beta 2/`，检查 8000 端口冲突，启动 Core 后再打开界面，并在退出时回收自己启动的进程。不会自动迁移旧数据。

源码构建与早期 ZIP 中的 `.app` 仍需要单独配置 Core。桌面尚无 Developer ID 签名、公证或自动更新。
自包含打包入口见 [DMG 构建说明](../../scripts/macos_dmg/README.md)。

## 从源码启动（macOS）

准备 uv、Node.js/npm、Rust/Cargo 和 macOS 构建工具。以下命令从仓库根目录执行。
先在一个终端启动 Core：

```bash
uv sync --frozen --extra dev
uv run operant serve --host 127.0.0.1 --port 8000 --desktop
```

在第二个终端、同一仓库根目录启动桌面开发窗口：

```bash
npm ci --prefix clients/gui
npm ci --prefix clients/desktop
npm run tauri --prefix clients/desktop -- dev
```

Tauri 配置会启动前端开发服务。`--desktop` 让 Core 接受固定的 Tauri 来源；已有 8000 端口服务若未带该参数，
即使网页可访问，桌面窗口也可能连接失败。请先核对服务归属，再决定是否停止旧服务，不要直接覆盖或强杀。

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

- 首次使用先确认是实时连接还是演示模式，演示数据不是实际任务。
- 使用稳定的运行目录并备份旧数据库；升级 App 不代表已迁移用户数据。
- 桌面壳只接受固定的本机 Core 地址，不从网页输入执行路径、命令或工作目录。
- 本机构建、安装验证和正式分发是不同阶段。版本范围见 [根 README](../../README.md)，安全边界见 [SECURITY](../../SECURITY.md)。
