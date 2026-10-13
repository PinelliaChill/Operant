# Operant 桌面客户端

这是基于 Tauri 2 的桌面壳，使用 `clients/gui` 的 React 界面。窗口管理和本机 Core 的连接由桌面壳负责，
模型、工具、审批、数据库和任务恢复仍由 Python Core 处理。

桌面壳需要匹配版本的 Core，不能只复制 `.app` 就假定已经完成安装。
上述源码构建输出为 macOS `.app`；预发布安装包见根 README。尚无正式 Developer ID 签名、公证或自动更新。

## 从源码启动（macOS）

准备 uv、Node.js/npm、Rust/Cargo 和 macOS 构建工具。以下命令从仓库根目录执行。
先安装锁定依赖：

```bash
uv sync --frozen --extra dev
npm ci --prefix clients/gui
npm ci --prefix clients/desktop
```

在同一仓库根目录启动桌面开发窗口：

```bash
PATH="$PWD/.venv/bin:$PATH" npm run tauri --prefix clients/desktop -- dev
```

Tauri 启动前端服务，并自行创建匹配版本的 Core 子进程。随机连接密钥只经匿名 stdin 管道传给
该子进程；管理操作经原生桥接验证 Core 身份。默认端口 8000 必须空闲；已有服务会阻止启动，
不会被接管或停止。请先核对归属，勿直接覆盖或强杀。

管理操作在开发窗口和发布壳中都走原生桥接；其他查询和事件流在开发窗口中使用 Vite 同源代理。
仅调试构建支持 `OPERANT_CORE_URL=http://127.0.0.1:<port>`、绝对路径的
`OPERANT_CORE_EXECUTABLE` 和 `OPERANT_CORE_DATA_DIR`；它们由启动环境提供，网页不能指定。
默认 Core 数据位于应用数据目录的 `core/`，调试验收可另选空目录。安装启动环境显式提供的
绝对 `OPERANT_DB_PATH` 仍会复用；调试隔离目录优先于该值。升级前先备份数据库，本批未迁移真实用户库。

## 构建候选

安装好上述依赖后，在仓库根目录执行：

```bash
cargo test --locked --manifest-path clients/desktop/src-tauri/Cargo.toml
npm run tauri --prefix clients/desktop -- build
```

配置中的构建钩子会先编译 GUI，产物位于 `clients/desktop/src-tauri/target/release/bundle/macos/`。
候选 App 会自行启动 Core，启动环境的 `PATH` 必须能找到匹配的 `operant`。
从终端能运行 `uv run operant`，不代表 Finder 启动的 App 也拥有同样的环境。

已有窗口使用 `clients/gui/dist` 时，隔离验收须直接给 Vite 指定另一输出目录，避免覆盖该窗口的文件。
`npm run build -- --outDir ...` 不会把目录参数传给脚本中的 Vite。先在 `clients/gui` 执行类型检查，
再构建并检查同一目录：

```bash
npm run typecheck
npx vite build --outDir ../../.operant/gui-verification-dist
node scripts/check-bundle.mjs ../../.operant/gui-verification-dist
```

该目录由隔离的预览服务使用；原窗口的服务、数据与静态目录保持独立。

## Gemini OAuth 应用配置

以下由 Operant 应用维护者准备，普通用户在连接页登录并选择自己的 Cloud 项目。
按 [Google 官方流程](https://ai.google.dev/gemini-api/docs/oauth)，在自有项目启用 Generative Language API，
配置 Google Auth platform 的授权页面和测试用户，再在 Clients 中创建 Desktop app 客户端。
账号条款和真实授权由账号所有者完成；隔离验收不发布应用或改变组织权限。

将该客户端的 `OPERANT_GEMINI_OAUTH_CLIENT_ID` 和 `OPERANT_GEMINI_OAUTH_CLIENT_SECRET`
配置在目标 Core 的受保护、未提交 `.env`，或只注入该进程。不要写入源码、SQLite、日志或聊天。
首次连接还需用户的 Google Cloud 项目 ID，调用通过 `x-goog-user-project` 使用该项目；
后续登录复用同一客户端和项目。客户端配置、账号登录与模型可用分别核对，不能互相代替。
缺配置时保留现有对话并给出连接入口；真实登录、目录、流式工具调用、续期和撤销均通过后才算验收。

## 使用边界

- 首次使用在“连接模型”中选择服务商和模型；系统准备通用助手及个人工作区。演示切换位于开发设置。
- 创建响应丢失时，退出重开仍可按原请求核对并打开已创建的对话，不会自动重复创建或发送消息。输入草稿当前仅在窗口运行期间保留。
- 本机能力启用后可选择“在新对话中使用”，先核对应用或完整网站范围。新对话使用专用控制会话；原手动操控会话须先结束。响应丢失时按原请求核对，勿重复开启。
- 明确批准后点“继续原请求”直接进入新对话；实际工具审批在对话中处理，批准只适用于对应目标和操作。接管、撤销或未知结果不会自动重试。
- 使用稳定的运行目录并备份旧数据库；升级 App 不代表已迁移用户数据。
- 桌面壳只接受固定的本机 Core 地址，不从网页输入执行路径、命令或工作目录。
- 默认启动器已接入私有管道与固定操作桥接，密钥和签名接口不交给网页。Core 的
  `--desktop-auth-stdio` 需要原生父进程，不应手动给普通终端启动命令添加该参数。
  已验证实际 Tauri 的管理查询、选择器与退出回收；独立客户端的技能目录配对增量已接入，仍待真实验收。其他 HTTP 入口不能据此视为已认证。
- 模型登录通过受限的 `open_model_oauth` 命令打开官方 OpenAI/Google 页面，回调须匹配当前 Core；不提供任意链接打开权限。真实账号授权结果见 [本轮验收](../../docs/design/onboarding-ux/acceptance.md)。
- 本机构建、安装验证和正式分发是不同阶段。版本范围见 [根 README](../../README.md)，安全边界见 [SECURITY](../../SECURITY.md)。
