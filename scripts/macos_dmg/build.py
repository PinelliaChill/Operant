"""Build a relocatable, self-contained macOS arm64 Beta 2 DMG.

Inputs are the already accepted Beta 2 App and Core wheel, plus the exact
uv.lock and the verified official standalone CPython 3.13.14 arm64 distribution. No project
source or user database is installed into the application bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = "Operant-v0.1.0-beta.2-macos-arm64"
PYTHON_VERSION = "3.13.14"
PYTHON_DISTRIBUTION = f"cpython-{PYTHON_VERSION}-macos-aarch64-none"
PYTHON_ARCHIVE_SHA256 = "aa2a054f5e04bde63ae199e3bb6bbb634e457423efd294842deeb1299e7e5932"


def run(*args: str | Path, env: dict[str, str] | None = None) -> None:
    command = [str(arg) for arg in args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, env=env)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def require_arm64(path: Path) -> None:
    output = subprocess.check_output(["lipo", "-archs", str(path)], text=True).split()
    if "arm64" not in output or "x86_64" in output:
        raise ValueError(f"not arm64-only: {path}: {output}")


def verify_python_distribution(directory: Path, archive: Path) -> None:
    if digest(archive) != PYTHON_ARCHIVE_SHA256:
        raise ValueError("standalone CPython archive differs from the verified upstream asset")
    libpython = directory / "lib/libpython3.13.dylib"
    install_name = subprocess.check_output(["otool", "-D", str(libpython)], text=True).splitlines()[
        -1
    ]
    if install_name != "@rpath/libpython3.13.dylib":
        raise ValueError(f"Python libpython is not relocatable: {install_name}")
    sysconfig = directory / "lib/python3.13/_sysconfigdata__darwin_darwin.py"
    if "/Users/" in sysconfig.read_text() or "/private/tmp/" in sysconfig.read_text():
        raise ValueError("Python sysconfig contains a local absolute path")


def verify_release_wheel(release: Path, wheel: Path) -> None:
    manifest = release / "SHA256SUMS"
    expected = {}
    for line in manifest.read_text().splitlines():
        checksum, relative = line.split(maxsplit=1)
        expected[relative.lstrip("* ")] = checksum
    relative = wheel.relative_to(release).as_posix()
    if expected.get(relative) != digest(wheel):
        raise ValueError("Core wheel differs from the published Beta 2 manifest")
    source = json.loads((release / "SOURCE_MANIFEST.json").read_text())
    core = json.loads((release / "core/release-manifest.json").read_text())
    if source["source_tree"] != "900db4bc9e901819d1c8c1cf12b303d662e0fdf4":
        raise ValueError("unexpected Beta 2 source tree")
    if source["files_sha256"][relative] != digest(wheel):
        raise ValueError("Core wheel differs from source manifest")
    if core["artifacts"][wheel.name] != digest(wheel):
        raise ValueError("Core wheel differs from build manifest")
    if core["uv_lock_sha256"] != digest(ROOT / "uv.lock"):
        raise ValueError("uv.lock differs from accepted Beta 2 source")


def check_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        destination = (path.parent / os.readlink(path)).resolve()
        if not destination.is_relative_to(root.resolve()):
            raise ValueError(f"bundle contains external symlink: {path} -> {destination}")


def bundle_checksums(app: Path) -> dict[str, str]:
    result = {}
    for path in sorted(app.rglob("*")):
        relative = path.relative_to(app).as_posix()
        if path.is_symlink():
            result[relative] = "symlink:" + os.readlink(path)
        elif path.is_file():
            result[relative] = digest(path)
    return result


def sign_app(app: Path) -> None:
    for path in app.rglob("*"):
        if path.is_file() and not path.is_symlink():
            marker = subprocess.run(
                ["file", "-b", str(path)], capture_output=True, text=True, check=True
            ).stdout
            if "Mach-O" in marker:
                run("codesign", "--force", "--sign", "-", str(path))
    run("codesign", "--force", "--deep", "--sign", "-", str(app))
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release", type=Path, required=True, help="extracted published beta.2 macOS ZIP"
    )
    parser.add_argument(
        "--app", type=Path, required=True, help="accepted, already signed Beta 2 Operant.app"
    )
    parser.add_argument(
        "--python",
        type=Path,
        required=True,
        help="pristine official CPython 3.13.14 arm64 install_only_stripped directory",
    )
    parser.add_argument(
        "--python-archive",
        type=Path,
        required=True,
        help="verified official install_only_stripped archive",
    )
    parser.add_argument("--output", type=Path, required=True, help="new empty staging directory")
    parser.add_argument("--uv-cache", type=Path, required=True)
    parser.add_argument(
        "--runtime-licenses",
        type=Path,
        required=True,
        help="verified python-build-standalone 20260728 third-party license directory",
    )
    args = parser.parse_args()
    release = args.release.resolve(strict=True)
    app_source = args.app.resolve(strict=True)
    python_source = args.python.resolve(strict=True)
    python_archive = args.python_archive.resolve(strict=True)
    runtime_licenses = args.runtime_licenses.resolve(strict=True)
    output = args.output.resolve()
    wheel = release / "core/operant_agent-0.1.0-py3-none-any.whl"
    if python_source.name != PYTHON_DISTRIBUTION:
        raise ValueError(f"expected {PYTHON_DISTRIBUTION}")
    verify_python_distribution(python_source, python_archive)
    if (
        app_source.name != "Operant.app"
        or not (app_source / "Contents/MacOS/operant-desktop").is_file()
    ):
        raise ValueError("expected accepted Operant.app with operant-desktop")
    if not wheel.is_file():
        raise ValueError("published Core wheel is missing")
    verify_release_wheel(release, wheel)
    source_manifest = json.loads((release / "SOURCE_MANIFEST.json").read_text())
    if (
        digest(app_source / "Contents/MacOS/operant-desktop")
        != source_manifest["app"]["distribution_binary_sha256"]
    ):
        raise ValueError("Tauri binary differs from accepted Beta 2 distribution")
    require_arm64(app_source / "Contents/MacOS/operant-desktop")
    require_arm64(python_source / "bin/python3.13")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    args.uv_cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, UV_CACHE_DIR=str(args.uv_cache.resolve()))

    volume = output / "volume"
    volume.mkdir()
    app = volume / "Operant.app"
    run("ditto", app_source, app)
    resources = app / "Contents/Resources"
    python = resources / "python"
    run("ditto", python_source, python)
    packages = python / "lib/python3.13/site-packages"
    requirements = output / "locked-requirements.txt"
    with requirements.open("w") as destination:
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--offline",
                "--no-dev",
                "--extra",
                "artifacts",
                "--no-editable",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--python",
                "3.13",
            ],
            cwd=ROOT,
            env=env,
            stdout=destination,
            check=True,
        )
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python / "bin/python3.13",
        "--target",
        packages,
        "--no-deps",
        "--require-hashes",
        "--only-binary",
        ":all:",
        "-r",
        requirements,
        env=env,
    )
    run(
        "uv",
        "pip",
        "install",
        "--python",
        python / "bin/python3.13",
        "--target",
        packages,
        "--no-deps",
        wheel,
        env=env,
    )
    verify_python_distribution(python, python_archive)
    # Exact packaged imports and the standalone interpreter must work after relocation.
    subprocess.run(
        [
            str(python / "bin/python3.13"),
            "-I",
            "-c",
            "import operant, fastapi, uvicorn, cryptography; print(operant.__file__)",
        ],
        cwd=output,
        env=env,
        check=True,
    )

    launcher = app / "Contents/MacOS/operant-launcher"
    swift_env = dict(env, CLANG_MODULE_CACHE_PATH=str(output / "clang-cache"))
    run(
        "swiftc",
        "-module-cache-path",
        output / "swift-cache",
        "-O",
        "-target",
        "arm64-apple-macos11.0",
        "-framework",
        "AppKit",
        ROOT / "scripts/macos_dmg/Launcher.swift",
        "-o",
        launcher,
        env=swift_env,
    )
    require_arm64(launcher)
    wrapper = app / "Contents/Resources/bin/operant"
    wrapper.parent.mkdir()
    shutil.copy2(ROOT / "scripts/macos_dmg/operant-wrapper", wrapper)
    wrapper.chmod(0o755)
    info = app / "Contents/Info.plist"
    with info.open("rb") as source:
        plist = plistlib.load(source)
    plist["CFBundleExecutable"] = "operant-launcher"
    plist["CFBundleDisplayName"] = "Operant Beta 2"
    plist["LSMinimumSystemVersion"] = "11.0"
    with info.open("wb") as destination:
        plistlib.dump(plist, destination)

    for name in ("LICENSE", "COMMERCIAL.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(release / name, volume / name)
    shutil.copytree(release / "licenses", volume / "licenses")
    shutil.copy2(python / "lib/python3.13/LICENSE.txt", volume / "licenses/CPython-3.13.txt")
    shutil.copytree(runtime_licenses, volume / "licenses/python-runtime")
    with (volume / "THIRD_PARTY_NOTICES.md").open("a", encoding="utf-8") as notices:
        notices.write(
            "\n## 本 DMG 的内置 Python 运行时\n\n"
            "上文末尾关于 Python 依赖不随包提供的说明仅适用于原 Beta 2 ZIP。"
            "本 DMG 包含 CPython 3.13.14、由 `uv.lock` 锁定的 Core 运行依赖及 artifacts 可选依赖。"
            "CPython 许可见 `licenses/CPython-3.13.txt`；Python 构建所含第三方组件的许可"
            "见 `licenses/python-runtime/`；Python 包许可文件随各包保存在"
            " `Operant.app/Contents/Resources/python/lib/python3.13/site-packages/` 的"
            " `*.dist-info/licenses/` 等元数据内。\n"
        )
    installed_licenses = resources / "Licenses"
    installed_licenses.mkdir()
    for name in ("LICENSE", "COMMERCIAL.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(volume / name, installed_licenses / name)
    shutil.copytree(volume / "licenses", installed_licenses / "licenses")
    (volume / "Applications").symlink_to("/Applications", target_is_directory=True)
    (volume / "安装说明.txt").write_text(
        "Operant Beta 2（macOS arm64）\n\n"
        "将 Operant.app 拖到 Applications，或复制到任意可写位置后双击。"
        "首次打开此 ad-hoc 签名且未公证的 App 时，macOS 可能要求在系统设置中允许打开。\n\n"
        "App 自带 Core 和 Python；无需另装 Python 或 uv。首次启动会在"
        " ~/Library/Application Support/Operant Beta 2/ 创建独立数据库。"
        "它不会自动打开或迁移旧版数据库。退出桌面窗口后，内置 Core 随之停止。\n\n"
        "模型使用：如需 API Key，在上述私有数据目录中新建 .env，写入"
        " OPERANT_API_KEY=你的密钥（不要把密钥放入 App 包内）。重启 App 后，在界面里"
        "填写模型服务的 Base URL、模型 ID 和 Secret Ref=OPERANT_API_KEY，创建 Model Profile。"
        "模型是否可用取决于所选服务；包内不含密钥。\n\n"
        "如 8000 端口已由旧版 Operant 或其他服务占用，先退出该服务再打开本版。"
        "报错日志位于上述目录的 core.log。\n\n"
        "本包仅为 macOS arm64，未获 Apple Developer ID 签名或公证。"
        "项目许可见 LICENSE、COMMERCIAL.md、THIRD_PARTY_NOTICES.md 与 licenses/。\n",
        encoding="utf-8",
    )
    manifest = {
        "name": NAME,
        "source_tree": "900db4bc9e901819d1c8c1cf12b303d662e0fdf4",
        "published_core_wheel_sha256": digest(wheel),
        "accepted_tauri_binary_sha256": digest(app_source / "Contents/MacOS/operant-desktop"),
        "uv_lock_sha256": digest(ROOT / "uv.lock"),
        "python_distribution": PYTHON_DISTRIBUTION,
        "python_archive_sha256": digest(python_archive),
        "python_archive_source": (
            "https://github.com/astral-sh/python-build-standalone/releases/download/20260728/"
            "cpython-3.13.14%2B20260728-aarch64-apple-darwin-install_only_stripped.tar.gz"
        ),
        "python_binary_sha256": digest(python_source / "bin/python3.13"),
        "requirements_sha256": digest(requirements),
        "packaging_git_head": subprocess.check_output(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "packaging_scripts_sha256": {
            "scripts/macos_dmg/build.py": digest(ROOT / "scripts/macos_dmg/build.py"),
            "scripts/macos_dmg/Launcher.swift": digest(ROOT / "scripts/macos_dmg/Launcher.swift"),
            "scripts/macos_dmg/operant-wrapper": digest(ROOT / "scripts/macos_dmg/operant-wrapper"),
        },
        "signing": "ad-hoc only; not Developer ID signed or notarized",
    }
    check_symlinks(app)
    sign_app(app)
    manifest["launcher_sha256"] = digest(launcher)
    manifest["bundle_files_sha256"] = bundle_checksums(app)
    (volume / "来源摘要.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    dmg = output / f"{NAME}.dmg"
    run(
        "hdiutil",
        "create",
        "-volname",
        "Operant Beta 2",
        "-fs",
        "HFS+",
        "-srcfolder",
        volume,
        "-format",
        "UDZO",
        "-ov",
        dmg,
    )
    run("hdiutil", "verify", dmg)
    (output / f"{dmg.name}.sha256").write_text(f"{digest(dmg)}  {dmg.name}\n", encoding="ascii")
    print(f"DMG: {dmg}\nSHA256: {digest(dmg)}")


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("macOS is required")
    main()
