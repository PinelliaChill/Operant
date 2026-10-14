"""Build a self-contained macOS arm64 DMG from this checkout's App and Core wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10 build hosts
    import tomli as tomllib  # type: ignore[import-not-found]

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON_VERSION = "3.13.14"
PYTHON_DISTRIBUTION = f"cpython-{PYTHON_VERSION}-macos-aarch64-none"
PYTHON_ARCHIVE_SHA256 = "aa2a054f5e04bde63ae199e3bb6bbb634e457423efd294842deeb1299e7e5932"
RUNTIME_LICENSE_MANIFEST_SHA256 = "e9ec960dba08bec071817e546ad9b8ba5d93be21c9771d7519f2d9db666469ee"


def run(*args: str | Path, env: dict[str, str] | None = None) -> None:
    command = [str(arg) for arg in args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, env=env, cwd=ROOT)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def require_arm64(path: Path) -> None:
    archs = subprocess.check_output(["lipo", "-archs", str(path)], text=True).split()
    if archs != ["arm64"]:
        raise ValueError(f"not arm64-only: {path}: {archs}")


def verify_python_distribution(directory: Path) -> None:
    libpython = directory / "lib/libpython3.13.dylib"
    install_name = subprocess.check_output(["otool", "-D", str(libpython)], text=True).splitlines()[
        -1
    ]
    if install_name != "@rpath/libpython3.13.dylib":
        raise ValueError(f"Python libpython is not relocatable: {install_name}")
    sysconfig = directory / "lib/python3.13/_sysconfigdata__darwin_darwin.py"
    if "/Users/" in sysconfig.read_text() or "/private/tmp/" in sysconfig.read_text():
        raise ValueError("Python sysconfig contains a local absolute path")
    require_arm64(directory / "bin/python3.13")


def check_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink() and not (path.parent / os.readlink(path)).resolve().is_relative_to(
            root.resolve()
        ):
            raise ValueError(f"bundle contains external symlink: {path}")


def verified_runtime_licenses(directory: Path) -> list[Path]:
    source_file = directory / "SOURCE.json"
    if not source_file.is_file() or source_file.is_symlink():
        raise ValueError("runtime license source manifest is missing")
    if digest(source_file) != RUNTIME_LICENSE_MANIFEST_SHA256:
        raise ValueError("runtime license source manifest differs from verified input")
    source = json.loads(source_file.read_text())
    if source.get("python_version") != PYTHON_VERSION:
        raise ValueError("runtime licenses differ from bundled Python version")
    checksums = source.get("files_sha256")
    if not isinstance(checksums, dict) or "PYTHON.json" not in checksums:
        raise ValueError("runtime license manifest is incomplete")
    if any(
        not isinstance(name, str)
        or not re.fullmatch(r"(?:LICENSE\.[a-z0-9.-]+\.txt|PYTHON\.json)", name)
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        for name, checksum in checksums.items()
    ):
        raise ValueError("runtime license manifest has invalid file entries")
    expected = {"SOURCE.json", *checksums}
    actual = {path.name for path in directory.iterdir()}
    if actual - expected - {"README.txt"} or expected - actual:
        raise ValueError("runtime license directory contains unexpected or missing files")
    for name, checksum in checksums.items():
        path = directory / name
        if not path.is_file() or path.is_symlink() or digest(path) != checksum:
            raise ValueError(f"runtime license differs from manifest: {name}")
    return [directory / name for name in sorted(expected)]


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


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    desktop = json.loads((ROOT / "clients/desktop/src-tauri/tauri.conf.json").read_text())
    version = project["project"]["version"]
    if not isinstance(version, str):
        raise ValueError("invalid Core product version")
    if desktop["version"] != version:
        raise ValueError("Core and desktop product versions disagree")
    return version


def product_identifier() -> str:
    desktop = json.loads((ROOT / "clients/desktop/src-tauri/tauri.conf.json").read_text())
    return str(desktop["identifier"])


def bundle_identifier(base: str, candidate_id: str | None) -> str:
    if candidate_id is None:
        return base
    if len(candidate_id) > 48 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", candidate_id):
        raise ValueError("candidate ID must be 1-48 lowercase letters, digits or inner hyphens")
    return f"{base}.candidate.{candidate_id}"


def verify_wheel(wheel: Path, version: str) -> None:
    if wheel.name != f"operant_agent-{version}-py3-none-any.whl":
        raise ValueError("Core wheel name differs from product version")
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"operant_agent-{version}.dist-info/METADATA").decode()
        if "Name: operant-agent\n" not in metadata or f"Version: {version}\n" not in metadata:
            raise ValueError("Core wheel metadata differs from product version")
        if "operant/cli.py" not in archive.namelist():
            raise ValueError("Core wheel is missing the formal CLI")


def source_identity() -> tuple[str, str]:
    status = subprocess.check_output(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ],
        cwd=ROOT,
        text=True,
    )
    if status:
        raise ValueError("source checkout must be clean before packaging")
    head = subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True
    ).strip()
    return head, tree


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-archive", type=Path, required=True)
    parser.add_argument("--runtime-licenses", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new empty output directory")
    parser.add_argument("--uv-cache", type=Path, required=True)
    parser.add_argument(
        "--candidate-id",
        help="explicit isolated candidate namespace; omit for normal product identity",
    )
    args = parser.parse_args()
    python_archive = args.python_archive.resolve(strict=True)
    runtime_licenses = args.runtime_licenses.resolve(strict=True)
    output = args.output.resolve()
    source_head, source_tree = source_identity()
    version = project_version()
    target_identifier = bundle_identifier(product_identifier(), args.candidate_id)
    if digest(python_archive) != PYTHON_ARCHIVE_SHA256:
        raise ValueError("standalone CPython archive differs from the verified upstream asset")
    license_files = verified_runtime_licenses(runtime_licenses)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    args.uv_cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, UV_CACHE_DIR=str(args.uv_cache.resolve()))
    run("uv", "build", "--wheel", "--offline", "--out-dir", output / "wheels", env=env)
    wheel = output / "wheels" / f"operant_agent-{version}-py3-none-any.whl"
    verify_wheel(wheel, version)
    run("npm", "run", "tauri", "--prefix", "clients/desktop", "--", "build", env=env)
    app_source = ROOT / "clients/desktop/src-tauri/target/release/bundle/macos/Operant.app"
    executable = app_source / "Contents/MacOS/operant-desktop"
    require_arm64(executable)
    with (app_source / "Contents/Info.plist").open("rb") as source:
        product = plistlib.load(source)
    if product["CFBundleShortVersionString"] != version:
        raise ValueError("Tauri App version differs from product version")
    if product["CFBundleIdentifier"] != product_identifier():
        raise ValueError("Tauri App identifier differs from product configuration")
    if source_identity() != (source_head, source_tree):
        raise ValueError("source checkout changed during build")
    volume = output / "volume"
    volume.mkdir()
    app = volume / "Operant.app"
    run("ditto", app_source, app)
    resources = app / "Contents/Resources"
    python = resources / "python"
    run("tar", "-xzf", python_archive, "-C", output)
    extracted_python = output / "python"
    verify_python_distribution(extracted_python)
    shutil.move(str(extracted_python), str(python))
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
    verify_python_distribution(python)
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
        SCRIPT_DIR / "Launcher.swift",
        "-o",
        launcher,
        env=swift_env,
    )
    require_arm64(launcher)
    wrapper = resources / "bin/operant"
    wrapper.parent.mkdir()
    shutil.copy2(SCRIPT_DIR / "operant-wrapper", wrapper)
    wrapper.chmod(0o755)
    info = app / "Contents/Info.plist"
    with info.open("rb") as source:
        plist = plistlib.load(source)
    plist["CFBundleExecutable"] = "operant-launcher"
    plist["OperantProductBundleIdentifier"] = product_identifier()
    if args.candidate_id:
        plist["CFBundleDisplayName"] = f"Operant {version} Candidate"
        plist["CFBundleIdentifier"] = target_identifier
        plist["OperantCandidateID"] = args.candidate_id
    plist["LSMinimumSystemVersion"] = "11.0"
    with info.open("wb") as destination:
        plistlib.dump(plist, destination)
    installed_licenses = resources / "Licenses"
    installed_licenses.mkdir()
    for name in ("LICENSE", "COMMERCIAL.md"):
        shutil.copy2(ROOT / name, installed_licenses / name)
    shutil.copytree(ROOT / "licenses", installed_licenses / "licenses")
    shutil.copy2(
        python / "lib/python3.13/LICENSE.txt", installed_licenses / "licenses/CPython-3.13.txt"
    )
    target_licenses = installed_licenses / "licenses/python-runtime"
    target_licenses.mkdir()
    for license_file in license_files:
        shutil.copy2(license_file, target_licenses / license_file.name)
    (installed_licenses / "THIRD_PARTY_NOTICES.md").write_text(
        "本候选包内置 CPython 3.13.14 和 uv.lock 锁定的 Core/artifacts 依赖。"
        "各 Python 包的许可保存在内置 site-packages 的 dist-info 元数据中；"
        "CPython 和构建运行时许可见本目录 licenses/。\n",
        encoding="utf-8",
    )
    (resources / "安装说明.txt").write_text(
        f"Operant {version} macOS arm64\n\n"
        "将 Operant.app 从 DMG 复制到可写位置后打开。App 内置 Core 和 Python，无需另装。"
        + (
            "此候选构建使用独立的数据目录，不读取或迁移现有安装版数据库。"
            if args.candidate_id
            else "普通构建沿用当前产品的数据目录；安装前请备份已有数据库。"
        )
        + "退出桌面窗口后，内置 Core 随之停止。\n\n"
        "模型凭据由连接页写入受保护的 .env，或按现有正式入口注入；请勿写进 App 包。"
        "本包仅经 ad-hoc 签名，未经 Apple Developer ID 签名或公证。"
        "许可见 Contents/Resources/Licenses/。\n",
        encoding="utf-8",
    )
    (volume / "Applications").symlink_to("/Applications", target_is_directory=True)
    manifest = {
        "name": f"Operant-v{version}-macos-arm64"
        + (f"-candidate-{args.candidate_id}" if args.candidate_id else ""),
        "product_version": version,
        "candidate_id": args.candidate_id,
        "bundle_identifier": plist["CFBundleIdentifier"],
        "source_head": source_head,
        "source_tree": source_tree,
        "build_commands": [
            "uv build --wheel --offline --out-dir <output>/wheels",
            "npm run tauri --prefix clients/desktop -- build",
        ],
        "core_wheel_sha256": digest(wheel),
        "tauri_binary_sha256": digest(executable),
        "uv_lock_sha256": digest(ROOT / "uv.lock"),
        "python_distribution": PYTHON_DISTRIBUTION,
        "python_archive_sha256": digest(python_archive),
        "python_binary_sha256": digest(python / "bin/python3.13"),
        "requirements_sha256": digest(requirements),
        "packaging_scripts_sha256": {
            name: digest(SCRIPT_DIR / name)
            for name in ("build.py", "Launcher.swift", "operant-wrapper")
        },
        "signing": "ad-hoc only; not Developer ID signed or notarized",
    }
    (resources / "来源摘要.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    check_symlinks(app)
    sign_app(app)
    manifest["bundle_files_sha256"] = bundle_checksums(app)
    (output / "来源摘要.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    dmg = output / f"{manifest['name']}.dmg"
    volname = f"Operant {version}" + (" Candidate" if args.candidate_id else "")
    run(
        "hdiutil",
        "create",
        "-volname",
        volname,
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
