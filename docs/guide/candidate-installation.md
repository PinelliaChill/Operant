# Operant Beta 候选安装、升级与回退

本页保留手工候选的安装流程；已发布的自包含安装包见[根 README](../../README.md)。
本轮首次引导会为默认工作区接入六项内置技能；已有角色、快照和旧 CLI 手工配置保持原值。
模型 OAuth 的应用配置见[桌面说明](../../clients/desktop/README.md#gemini-oauth-应用配置)，
真实可用状态以[本轮验收](../design/onboarding-ux/acceptance.md)为准。

本说明适用于同一源码提交构建的 Python Core、TUI 和 macOS arm64 桌面候选。桌面 App 不内置 Core：启动时从 `PATH` 查找 `operant`，固定连接 `127.0.0.1:8000`。当前候选没有 Developer ID 签名、公证、DMG 或自动更新；Ubuntu 只安装 Core/TUI 和任务 5 已验收的私有远程命令行，不安装 macOS App。

所有操作使用独立的绝对路径。已有 Core 必须先正常退出，且要核对 8000 端口归属。不要对正在使用的数据库做升级或回退。备份包含运行态文件，可能含敏感信息；只保存在私有目录，传输时用可信通道，不提交仓库。

## 构建并核对候选

在冻结的源码提交上运行。构建机需要 uv、Python 3.12、Node/npm、Rust 和 macOS 构建工具。以下示例中的 `CANDIDATE_ROOT` 是空的新目录。

```bash
CANDIDATE_ROOT="/private/tmp/operant-candidate-$(git rev-parse --short HEAD)"
mkdir -m 700 "$CANDIDATE_ROOT"
python scripts/distribution.py record-inputs --output "$CANDIDATE_ROOT/SOURCE_INPUTS.json"
uv lock --check --offline
uv build --no-sources --out-dir "$CANDIDATE_ROOT/core"
python scripts/release_checks.py generate --dist "$CANDIDATE_ROOT/core"
python scripts/release_checks.py verify --dist "$CANDIDATE_ROOT/core"
uv build --no-sources clients/tui --out-dir "$CANDIDATE_ROOT/tui"
npm ci --prefix clients/gui
npm ci --prefix clients/desktop
npm run tauri --prefix clients/desktop -- build
mkdir "$CANDIDATE_ROOT/desktop"
ditto -c -k --sequesterRsrc --keepParent \
  clients/desktop/src-tauri/target/release/bundle/macos/Operant.app \
  "$CANDIDATE_ROOT/desktop/Operant.app.zip"
git rev-parse HEAD > "$CANDIDATE_ROOT/SOURCE_REVISION"
(cd "$CANDIDATE_ROOT" && shasum -a 256 tui/*.whl > TUI_SHA256SUMS)
(cd "$CANDIDATE_ROOT" && shasum -a 256 desktop/Operant.app.zip > DESKTOP_SHA256SUMS)
python scripts/distribution.py verify-inputs --manifest "$CANDIDATE_ROOT/SOURCE_INPUTS.json"
```

Core 的 `release-manifest.json` 和 `SHA256SUMS` 覆盖 Core wheel/sdist，并标明未签名候选；`TUI_SHA256SUMS` 和 `DESKTOP_SHA256SUMS` 分别覆盖 TUI 与 App zip。复制后在候选根目录运行 `shasum -a 256 -c TUI_SHA256SUMS` 和 `shasum -a 256 -c DESKTOP_SHA256SUMS`，在 `core/` 运行 `shasum -a 256 -c SHA256SUMS`。`SOURCE_INPUTS.json` 记录逐文件摘要、HEAD 和工作树是否有改动。候选仍是未提交改动时，不能把 `SOURCE_REVISION` 单独当成构建身份；提交后运行 `python scripts/distribution.py verify-inputs --manifest "$CANDIDATE_ROOT/SOURCE_INPUTS.json" --content-only`，逐文件核对冻结源码，允许 HEAD 和 dirty 标记随提交改变。Python 包内的六项默认 Skill 仍需通过产品入口显式安装、按项目启用；`[artifacts]` extra 提供 docx/pptx/pdf 所需 Python 库，宿主工具和模型凭据需在目标机单独配置。

## 新目录安装与启动

在 macOS 上解压候选 App 到私有测试目录。`INSTALL_ROOT` 和 `DATA_ROOT` 都必须是新目录，不能指向 `/Applications/Operant.app` 或正在使用的数据。首次安装需要可访问 Python 依赖源，或者事先准备完整 wheelhouse。

```bash
INSTALL_ROOT=/private/tmp/operant-beta-install
DATA_ROOT="$INSTALL_ROOT/data"
mkdir -m 700 "$INSTALL_ROOT"
ditto -x -k "$CANDIDATE_ROOT/desktop/Operant.app.zip" "$INSTALL_ROOT"
uv venv --python 3.12 "$INSTALL_ROOT/venv"
CORE_WHEEL=$(find "$CANDIDATE_ROOT/core" -maxdepth 1 -name '*.whl' -print -quit)
TUI_WHEEL=$(find "$CANDIDATE_ROOT/tui" -maxdepth 1 -name '*.whl' -print -quit)
uv pip install --python "$INSTALL_ROOT/venv/bin/python" \
  "operant-agent[artifacts] @ file://$CORE_WHEEL" "$TUI_WHEEL"
mkdir -m 700 "$DATA_ROOT"
OPERANT_DB_PATH="$DATA_ROOT/operant.sqlite3" "$INSTALL_ROOT/venv/bin/operant" init
OPERANT_DB_PATH="$DATA_ROOT/operant.sqlite3" "$INSTALL_ROOT/venv/bin/operant" --help
"$INSTALL_ROOT/venv/bin/operant-tui" --help
python scripts/distribution.py launch-desktop \
  --app "$INSTALL_ROOT/Operant.app" --venv "$INSTALL_ROOT/venv" --data "$DATA_ROOT"
```

桌面启动命令要求 8000 端口空闲，并为 App 注入候选 venv、固定绝对数据库路径和运行目录。请在真实窗口确认 Core 健康、Live 连接和重启后的数据；命令行 `--help` 只证明安装入口可执行。模型服务配置仅向目标进程注入环境变量，运行目录内不要复制真实 `.env`。Core 网页/TUI 可在独立终端用同一 `OPERANT_DB_PATH` 启动，避免同时写同一 SQLite。

## 从旧版本升级并保留数据

先正常停止旧 Core 和旧 App，确认没有进程仍写旧数据目录。示例要求旧数据库位于旧数据目录的 `operant.sqlite3`。`backup` 使用 SQLite backup API 生成一致的数据库副本，并复制该目录其他文件；快照目录必须是新目录。运行中的其他文件写入无法由 SQLite backup 保证，因此停机是前提。

```bash
OLD_DATA=/absolute/private/old-data
SNAPSHOT=/absolute/private/backup-before-upgrade
NEW_DATA=/absolute/private/new-version-data
python scripts/distribution.py backup --source "$OLD_DATA" --destination "$SNAPSHOT"
python scripts/distribution.py verify --snapshot "$SNAPSHOT"
python scripts/distribution.py restore --source "$SNAPSHOT" --destination "$NEW_DATA"
OPERANT_DB_PATH="$NEW_DATA/operant.sqlite3" "$INSTALL_ROOT/venv/bin/operant" init
```

新版本 `init` 只作用于 `NEW_DATA`，旧目录与快照保持原样。升级后通过正式 Core 入口检查旧 Session、记录、配置和必要的运行态文件，并核对新 Schema、`PRAGMA quick_check`。旧版 Core 不可打开升级后的库。新增版本产生的数据不会自动回填旧版。

## 回退

停止新版 Core/App。把**升级前**快照恢复到另一处空目录，并使用旧版 Core 的独立 venv/App 启动；不要用新版库做通用 SQLite downgrade。

```bash
ROLLBACK_DATA=/absolute/private/rollback-data
python scripts/distribution.py verify --snapshot "$SNAPSHOT"
python scripts/distribution.py restore --source "$SNAPSHOT" --destination "$ROLLBACK_DATA"
OPERANT_DB_PATH="$ROLLBACK_DATA/operant.sqlite3" /absolute/old-venv/bin/operant init
```

确认旧版正式入口可以读取升级前的数据，再决定是否切换日常入口。验证失败时保留三个目录供排查，不覆盖任何一个版本的数据。

## Ubuntu 跨机器安装

把同一候选的 `core/`、`tui/`、`TUI_SHA256SUMS` 和 `SOURCE_REVISION` 经可信通道送到 Ubuntu 的新私有目录。先在 Ubuntu 核对 wheel/hash 与来源记录，再装进新 venv；不要共享 Mac 的 venv 或 App。Ubuntu 的 Python 版本至少 3.10，建议与候选验证用的 3.12 一致。

```bash
REMOTE_ROOT=/absolute/private/operant-beta
(cd "$REMOTE_ROOT/core" && sha256sum -c SHA256SUMS)
(cd "$REMOTE_ROOT" && sha256sum -c TUI_SHA256SUMS)
python3 -m venv "$REMOTE_ROOT/venv"
CORE_WHEEL=$(find "$REMOTE_ROOT/core" -maxdepth 1 -name '*.whl' -print -quit)
TUI_WHEEL=$(find "$REMOTE_ROOT/tui" -maxdepth 1 -name '*.whl' -print -quit)
"$REMOTE_ROOT/venv/bin/pip" install \
  "operant-agent[artifacts] @ file://$CORE_WHEEL" "$TUI_WHEEL"
"$REMOTE_ROOT/venv/bin/operant" --help
"$REMOTE_ROOT/venv/bin/operant-tui" --help
```

若在 Ubuntu 只用任务 5 的 Remote Device/Target，可按需要省略 `[artifacts]`。跨设备功能仍按[私有跨设备说明](../design/beta-remote/usage.md)设置受限 edge、TLS/CA、Scope 与临时路径；安装成功不等于跨设备任务通过。Linux 上没有已验收的本地扩展隔离器时，不能启用相关第三方扩展。

## 本轮隔离验收记录（2026-10-05）

本轮候选在 `/private/tmp/operant-task6-distribution/candidate-bundle-01`，Core wheel SHA-256 为 `ac7e9cc205983820d9e84e4de172499e23735d08ef04e03faad47aeab95da928`。`SOURCE_INPUTS.json` 标明工作树尚有未提交改动，记录了构建输入逐文件摘要；构建后 `verify-inputs` 复验通过。Core 的 `release_checks.py verify`、TUI/App zip 的 SHA 校验通过，187 个 Python 源文件和六个默认 Skill 文件与 wheel 内内容逐字节相同。冻结提交完成后仍应按清单回读对应文件，不能只用旧 HEAD 认定候选身份。

- macOS 独立 venv 安装同一 Core/TUI wheel 与 `[artifacts]` extra，六项默认 Skill 均来自 `site-packages`；新库初始化为 v23，正式 Core/TUI 命令可运行。已安装 helper 实际生成一页 PDF，并用 pypdf 重开读回标题。真实 Tauri 候选 App 经 zip 解压，二进制 SHA 与原构建一致；隔离新库窗口连接、配置/Plan/清单保存及重开、退出时仅回收自启 Core、外部 Core 不误杀，见 `/private/tmp/operant-task6-distribution/native-final-result.json`。未更新 `/Applications/Operant.app`。
- 旧版 `08e65ea` Core 创建 v20 合成业务库，任务 4 Core 创建 v22 业务库；两版都通过正式管理入口安装 Memory 插件、建立项目绑定及保存检索记录。停机快照包含数据库、`memory-plugins/registry.json` 与包目录、外部 artifact。候选 Core 在新目录升级到 v23，四张业务表原行、registry 和 artifact 哈希相等；正式 Session、管理和记忆检索可读。另从升级前快照恢复后，各自旧版 Core 重新启动并读回同一内容。结果见 `/private/tmp/operant-task6-distribution/v20-to-v23-complete-result.json` 与 `v22-to-v23-enabled-result.json`。升级恢复时已安装 Memory 插件按生命周期处于 disabled，检索前需通过正式入口显式启用；早期直接检索的 409 作为历史失败保留。
- 另一台 Ubuntu 24.04 主机通过任务 5 已用的 SSH 身份连接，仅在 `/tmp/operant-beta-task6-distribution.bk55ll` 建立私有目录；从 Mac 用 `scp` 传同一 Core/TUI 候选，目标机重新校验 SHA。Python 3.12.3 新 venv 安装后，Core/TUI CLI、v23 新库、回环正式 Core `/healthz`、六项包来源和 `[artifacts]` 导入均通过；Core 已停止。已安装 helper 也实际生成 PDF 并重开确认一页及标题。远端机器 ID 仅记录哈希前 12 位，不保存凭据。脱敏结果在 `/private/tmp/operant-task6-distribution/ubuntu-remote-result.json` 与 `ubuntu-identity-helper.json`。
