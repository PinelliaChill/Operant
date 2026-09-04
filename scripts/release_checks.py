#!/usr/bin/env python3
"""Generate and verify release evidence without claiming unperformed signing."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import zipfile
from email.parser import BytesParser
from pathlib import Path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifacts(dist: Path) -> list[Path]:
    artifacts = sorted((*dist.glob("*.whl"), *dist.glob("*.tar.gz")))
    if not artifacts:
        raise SystemExit(f"no wheel or source distribution found in {dist}")
    return artifacts


def _wheel_metadata(wheel: Path) -> tuple[str, str, list[str]]:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise SystemExit(f"expected one METADATA entry in {wheel.name}")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    name = metadata["Name"]
    version = metadata["Version"]
    if not name or not version:
        raise SystemExit(f"wheel metadata is missing Name or Version: {wheel.name}")
    requirements = sorted(metadata.get_all("Requires-Dist", []))
    return name, version, requirements


def generate(dist: Path) -> None:
    artifacts = _artifacts(dist)
    checksums = {path.name: _digest(path) for path in artifacts}
    (dist / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in checksums.items()),
        encoding="utf-8",
    )
    wheel = next((path for path in artifacts if path.suffix == ".whl"), None)
    if wheel is None:
        raise SystemExit("a wheel is required to generate package metadata")
    name, version, requirements = _wheel_metadata(wheel)
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{name}-{version}",
        "documentNamespace": f"https://operant.invalid/spdx/{name}/{version}/{checksums[wheel.name]}",
        "creationInfo": {
            "created": "1970-01-01T00:00:00Z",
            "creators": ["Tool: scripts/release_checks.py"],
            "comment": (
                "Deterministic package SBOM; resolved versions remain authoritative in uv.lock."
            ),
        },
        "packages": [
            {
                "name": name,
                "SPDXID": "SPDXRef-Package",
                "versionInfo": version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "Apache-2.0",
                "licenseDeclared": "Apache-2.0",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{name}@{version}",
                    }
                ],
                "checksums": [{"algorithm": "SHA256", "checksumValue": checksums[wheel.name]}],
                "comment": "Requires-Dist: " + "; ".join(requirements),
            }
        ],
    }
    (dist / "operant-agent.spdx.json").write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "artifacts": checksums,
        "sbom": "operant-agent.spdx.json",
        "signing": "not_performed",
        "notarization": "not_applicable_to_python_wheel_and_sdist",
        "scope": "Python wheel and source distribution only",
    }
    (dist / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def verify(dist: Path) -> None:
    manifest = json.loads((dist / "release-manifest.json").read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise SystemExit("release manifest has no artifacts")
    actual_artifact_names = {path.name for path in _artifacts(dist)}
    if set(artifacts) != actual_artifact_names:
        raise SystemExit("release manifest does not cover exactly the distribution artifacts")
    for name, expected in artifacts.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise SystemExit("release manifest contains an invalid artifact name")
        path = dist / name
        if not path.is_file() or _digest(path) != expected:
            raise SystemExit(f"checksum verification failed: {name}")
    sbom_name = manifest.get("sbom")
    if not isinstance(sbom_name, str) or Path(sbom_name).name != sbom_name:
        raise SystemExit("release manifest contains an invalid SBOM name")
    sbom = json.loads((dist / sbom_name).read_text(encoding="utf-8"))
    packages = sbom.get("packages")
    if sbom.get("spdxVersion") != "SPDX-2.3" or not isinstance(packages, list):
        raise SystemExit("SPDX SBOM validation failed")
    wheels = sorted(path for path in _artifacts(dist) if path.suffix == ".whl")
    if len(wheels) != 1 or len(packages) != 1 or not isinstance(packages[0], dict):
        raise SystemExit("release evidence requires exactly one wheel and one SPDX package")
    wheel = wheels[0]
    wheel_name, wheel_version, requirements = _wheel_metadata(wheel)
    package = packages[0]
    package_checksums = package.get("checksums")
    if (
        package.get("name") != wheel_name
        or package.get("versionInfo") != wheel_version
        or package.get("comment") != "Requires-Dist: " + "; ".join(requirements)
        or package_checksums != [{"algorithm": "SHA256", "checksumValue": artifacts[wheel.name]}]
    ):
        raise SystemExit("SPDX package does not match the built wheel")
    expected_lines = [f"{digest}  {name}" for name, digest in artifacts.items()]
    actual_lines = (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    if actual_lines != expected_lines:
        raise SystemExit("SHA256SUMS does not match release manifest")


def verify_signature(
    artifact: Path, signature: Path, certificate: Path, identity: str, oidc_issuer: str
) -> None:
    cosign = shutil.which("cosign")
    if cosign is None:
        raise SystemExit("cosign is required for signature verification")
    subprocess.run(
        [
            cosign,
            "verify-blob",
            "--signature",
            str(signature),
            "--certificate",
            str(certificate),
            "--certificate-identity",
            identity,
            "--certificate-oidc-issuer",
            oidc_issuer,
            str(artifact),
        ],
        check=True,
    )


def verify_macos(bundle: Path) -> None:
    if platform.system() != "Darwin":
        raise SystemExit("macOS signature/notarization verification requires a macOS host")
    if not bundle.exists():
        raise SystemExit(f"bundle does not exist: {bundle}")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    subprocess.run(["spctl", "--assess", "--type", "exec", "--verbose=2", str(bundle)], check=True)
    subprocess.run(["xcrun", "stapler", "validate", str(bundle)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("generate", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--dist", type=Path, required=True)
    signature = subparsers.add_parser("verify-signature")
    signature.add_argument("--artifact", type=Path, required=True)
    signature.add_argument("--signature", type=Path, required=True)
    signature.add_argument("--certificate", type=Path, required=True)
    signature.add_argument("--identity", required=True)
    signature.add_argument("--oidc-issuer", required=True)
    macos = subparsers.add_parser("verify-macos")
    macos.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.dist)
    elif args.command == "verify":
        verify(args.dist)
    elif args.command == "verify-signature":
        verify_signature(
            args.artifact,
            args.signature,
            args.certificate,
            args.identity,
            args.oidc_issuer,
        )
    else:
        verify_macos(args.bundle)


if __name__ == "__main__":
    main()
