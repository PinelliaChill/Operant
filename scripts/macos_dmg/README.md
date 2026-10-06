# macOS arm64 自包含 DMG 构建

本脚本在 macOS arm64 上，把 Beta 2 已验收的 Tauri App、同版 Core wheel、`uv.lock` 锁定的运行依赖及 artifacts 依赖、官方可迁移 CPython 3.13.14 放入一个 App。它会编译 Swift 启动器、对全部嵌入 Mach-O 做 ad-hoc 签名，并生成 DMG 与同名 `.sha256` 文件。构建不触碰 `/Applications/Operant.app` 或用户数据库。

准备以下输入：

1. 解开的正式 `Operant-v0.1.0-beta.2-macos-arm64.zip` 目录，其中含 `SOURCE_MANIFEST.json`、`SHA256SUMS`、Core wheel 和许可；其内 `desktop/Operant.app.zip` 解出 `Operant.app`。脚本核对 App 二进制和 wheel 的发布摘要。
2. [python-build-standalone 20260728](https://github.com/astral-sh/python-build-standalone/releases/tag/20260728) 的 `cpython-3.13.14+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz`。预期 SHA-256：`aa2a054f5e04bde63ae199e3bb6bbb634e457423efd294842deeb1299e7e5932`。解压后把 `python/` 目录命名为 `cpython-3.13.14-macos-aarch64-none/`，不要用 `uv python install` 后改写过的本机运行时。脚本要求 `libpython3.13.dylib` 的 install name 为 `@rpath/libpython3.13.dylib`。
3. 与同一 Python 构建对应的第三方许可目录，须包含 `SOURCE.json`、`PYTHON.json`、CPython 及所含组件的许可原文。最终目录会原样进入 DMG 的 `licenses/python-runtime/`。

在仓库根目录运行（路径换成实际绝对路径，输出目录须为空）：

```bash
python3 scripts/macos_dmg/build.py \
  --release /absolute/path/to/extracted/Operant-v0.1.0-beta.2-macos-arm64 \
  --app /absolute/path/to/Operant.app \
  --python /absolute/path/to/cpython-3.13.14-macos-aarch64-none \
  --python-archive /absolute/path/to/cpython-3.13.14+20260728-aarch64-apple-darwin-install_only_stripped.tar.gz \
  --runtime-licenses /absolute/path/to/runtime-licenses \
  --uv-cache /absolute/path/to/uv-cache \
  --output /absolute/path/to/empty-output
```

输出为 `Operant-v0.1.0-beta.2-macos-arm64.dmg` 和 `Operant-v0.1.0-beta.2-macos-arm64.dmg.sha256`。脚本会验证锁文件、架构、内置 Python 导入、签名和 DMG 校验。DMG 根目录的 `来源摘要.json` 记录输入、脚本及 App 文件摘要。

发布前还需从 DMG 挂载并把 App 复制到含空格路径，使用干净 `PATH` 双击验证：8000 被占用时拒绝误连，空闲时启动内置 Core 并显示 GUI，退出与重开后 Core 能回收、独立数据库保持可用。不要用现有用户库做测试。本包仅 ad-hoc 签名，未获 Apple Developer ID 签名或公证。
