# B2-2 开工环境检查

记录身份：Codex；适用对象：本批执行者、Reviewer。2026-09-10。
实施基线 `8851a237cc961afa8649cffe57d407472838887f`，隔离树 `/private/tmp/operant-b2-2`。
本文件只证明开工可用性，不替代最终集成验收。

| 环境 | 结果与限制 |
| --- | --- |
| Python | 3.13.3；两处 uv 离线缓存缺包。复制 B2-1 已安装依赖至独立 .venv，修正脚本与 editable 路径后 `operant.__file__` 指向本树。后续 `UV_CACHE_DIR=/private/tmp/operant-uv-cache uv run --no-sync`，锁文件未改 |
| GUI | Node v26.8.1；复用现有 node_modules，package/lock 未变。Vite 在 127.0.0.1:3000，通过 OPERANT_CORE_URL 代理隔离 Core |
| Core | 正式 `build_server_config(desktop=True)`，127.0.0.1:18000，临时 SQLite；healthz 200。已有用户 8000 服务保留 |
| 桌面预检 | Tauri Origin + X-Operant-Client-Version 的 OPTIONS 200，未知 Origin OPTIONS 400。初次直接 create_app 未启用 desktop CORS 返回405，改正式启动器后通过；没有放宽已有 CORS |
| Tauri | 桌面代码与 B2-1 构建输入无差异，复用 `/private/tmp/operant-b2-1-a/clients/desktop/src-tauri/target/debug/operant-desktop`。独立临时 `Operant B2-2 Verification.app`，不替换用户安装。CUA 实际观察 WebView `127.0.0.1:3000/#/projects`，Live/Core 已连接、空隔离库无项目；正式打包/signing 不属本次验收 |
| Provider | 正式 `uv run --no-sync operant model discover` 成功，67个精确ID，包含 `gpt-5.6-luna`。已有 .env 仅加载至目标进程，不复制/打印值；没有据此宣称模型调用成功 |
| 模型任务准备 | 通过正式 /v1/models、/v1/roles、/v1/commands/workspace/init 在临时库登记 profile/只读角色及绝对 workspace。模型 gpt-5.6-luna/low；预算4轮/90秒/600输出tokens/2工具调用；无安装插件，无workspace写权限；仅受控本机只读工具允许待审批 |
| 隔离执行 | macOS sandbox-exec 宿主可启动；执行沙箱内部 probe 被系统拒绝，实际边界需宿主级验证。Docker daemon不可用；不能把条件skip当作Docker验收 |
| Reviewer | 指定 gpt-5.6-luna/max；协作工具未提供 Fast 开关，未启用/未宣称 Fast |

原生工具第一次 getApp 耗时约415秒后返回真实窗口；此后 AX读取小于1秒。后续集中验收新GUI流程，避免重复启动。
