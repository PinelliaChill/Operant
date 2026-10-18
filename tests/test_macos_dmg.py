"""Packaging boundaries that must fail before writing a distributable DMG."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.macos_dmg import build


def _wheel(path: Path, version: str, metadata_version: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"operant_agent-{version}.dist-info/METADATA",
            f"Name: operant-agent\nVersion: {metadata_version}\n",
        )
        archive.writestr("operant/cli.py", "app = object()\n")


def test_candidate_identity_requires_explicit_valid_namespace() -> None:
    base = build.product_identifier()
    assert build.bundle_identifier(base, None) == base
    assert build.bundle_identifier(base, "current-main-20261014") == (
        f"{base}.candidate.current-main-20261014"
    )
    for invalid in ("", "../existing", "HasUppercase", "-leading", "trailing-", "a" * 49):
        with pytest.raises(ValueError):
            build.bundle_identifier(base, invalid)


def test_wheel_version_and_cli_must_match_product(tmp_path: Path) -> None:
    version = build.project_version()
    wheel = tmp_path / f"operant_agent-{version}-py3-none-any.whl"
    _wheel(wheel, version, "9.9.9")
    with pytest.raises(ValueError, match="metadata"):
        build.verify_wheel(wheel, version)
    _wheel(wheel, version, version)
    build.verify_wheel(wheel, version)


def test_bundle_rejects_symlink_escaping_app(tmp_path: Path) -> None:
    bundle = tmp_path / "Operant.app"
    bundle.mkdir()
    (bundle / "safe").symlink_to("nested", target_is_directory=True)
    (bundle / "nested").mkdir()
    build.check_symlinks(bundle)
    (bundle / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="external symlink"):
        build.check_symlinks(bundle)


def test_runtime_license_input_cannot_add_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    license_file = tmp_path / "LICENSE.cpython.txt"
    license_file.write_text("license text")
    checksum = hashlib.sha256(license_file.read_bytes()).hexdigest()
    (tmp_path / "PYTHON.json").write_text("{}")
    python_checksum = hashlib.sha256((tmp_path / "PYTHON.json").read_bytes()).hexdigest()
    source = tmp_path / "SOURCE.json"
    source.write_text(
        json.dumps(
            {
                "python_version": build.PYTHON_VERSION,
                "files_sha256": {
                    "LICENSE.cpython.txt": checksum,
                    "PYTHON.json": python_checksum,
                },
            }
        )
    )
    monkeypatch.setattr(build, "RUNTIME_LICENSE_MANIFEST_SHA256", build.digest(source))
    assert {path.name for path in build.verified_runtime_licenses(tmp_path)} == {
        "SOURCE.json",
        "LICENSE.cpython.txt",
        "PYTHON.json",
    }
    (tmp_path / ".env").write_text("DO_NOT_COPY=1")
    with pytest.raises(ValueError, match="unexpected"):
        build.verified_runtime_licenses(tmp_path)


def test_vendored_runtime_licenses_match_verified_source() -> None:
    files = build.verified_runtime_licenses(build.SCRIPT_DIR / "runtime-licenses")
    assert any(path.name == "PYTHON.json" for path in files)
    assert any(path.name == "LICENSE.cpython.txt" for path in files)


def test_release_identity_is_explicit_and_bounded() -> None:
    assert build.release_label(None) is None
    assert build.release_label("2.0.0-beta.5") == "2.0.0-beta.5"
    for invalid in ("", "a" * 81, "../beta5", "beta\nsecret", " beta"):
        with pytest.raises(ValueError, match="release label"):
            build.release_label(invalid)


def test_built_product_must_match_requested_normal_or_candidate_identity() -> None:
    normal = build.product_identifier()
    candidate = build.bundle_identifier(normal, "usage-polish")
    for identifier in (normal, candidate):
        product = {
            "CFBundleShortVersionString": build.project_version(),
            "CFBundleIdentifier": identifier,
        }
        build.verify_tauri_product(product, build.project_version(), identifier)
        other = candidate if identifier == normal else normal
        with pytest.raises(ValueError, match="identifier"):
            build.verify_tauri_product(product, build.project_version(), other)
        with pytest.raises(ValueError, match="version"):
            build.verify_tauri_product(product, "9.9.9", identifier)
