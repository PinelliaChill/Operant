# macOS arm64 自包含 DMG 构建

此脚本从当前干净的 Git 提交构建 Tauri App 和 Core wheel，再把它们、`uv.lock` 锁定的运行依赖及 artifacts 依赖、可迁移 CPython 3.13.14 放入一个 App。DMG 根目录只有 `Operant.app` 和指向 `/Applications` 的快捷入口；App 内置安装说明、来源摘要和许可。构建不安装 App，不读取或迁移用户数据库。

从仓库根目录准备锁定依赖与工具：

```bash
uv sync --frozen --extra dev
npm ci --prefix clients/gui
npm ci --prefix clients/desktop
```

还需 Node、Rust、macOS Xcode Command Line Tools 与 `uv`。打包器要求工作树干净且 HEAD 在构建前后不变，自行执行 `uv build --wheel --offline` 与 `npm run tauri --prefix clients/desktop -- build`，不接受外部 App 或 wheel。它再次核对 App、wheel 与产品版本、arm64 架构和锁文件。

Python 输入是 [python-build-standalone 20260728](https://github.com/astral-sh/python-build-standalone/releases/tag/20260728) 的 `cpython-3.13.14+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz`（SHA-256 `aa2a054f5e04bde63ae199e3bb6bbb634e457423efd294842deeb1299e7e5932`）。打包器先核对归档摘要，再解压到新输出目录；`libpython3.13.dylib` 的 install name 须为 `@rpath/libpython3.13.dylib`。对应的公开第三方许可文本和上游来源清单已存于 [runtime-licenses](runtime-licenses)，脚本核对 `SOURCE.json` 的固定摘要及其逐文件摘要，拒绝额外文件。新 clone 只需另取上述公开 Python 归档，无需旧 Beta 发布包或本机隐藏目录。

正常分发模式保留当前 Tauri 产品 bundle ID、显示名和默认数据目录语义。下例的 `--candidate-id` 是**本轮隔离验收必填**的显式选项；它写入 App 元数据，生成独立 bundle ID 和仅当前用户可访问的 `~/Library/Application Support/Operant Candidates/<候选 bundle ID>/`，不接触现有安装版数据库。将占位路径替换为实际绝对路径，输出目录必须为空：

```bash
uv run --extra dev python scripts/macos_dmg/build.py \
  --python-archive /absolute/path/to/cpython-3.13.14+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz \
  --uv-cache /absolute/path/to/uv-cache \
  --candidate-id current-main-20261014 \
  --output /absolute/path/to/empty-output
```

省略 `--candidate-id` 会生成普通产品身份的 DMG，使用当前桌面壳正式数据路径。两种模式都由当前 Tauri `ManagedCore` 进行端口占用拒绝、匿名 stdin 密钥认证、Core 身份确认和子进程退出回收；Swift 启动器只定位内置 Core、设置 PATH，并在候选模式隔离环境和数据库。Core 凭据只保存在其私有 `.env` 或按正式入口注入，绝不放进 App。

脚本验证锁定依赖、内置 Python 导入、arm64、嵌入 Mach-O 的 ad-hoc 签名和 DMG 摘要，并生成同名 `.sha256` 与 `来源摘要.json`。此签名不等于 Apple Developer ID 签名或公证。正式跨机器安装前，仍须从 DMG 挂载并复制 App 到含空格的隔离路径，在干净 PATH 下检查首次启动、8000 占用拒绝、Core 正式管理请求、退出回收、重开和独立库持久性；不使用真实用户库。
