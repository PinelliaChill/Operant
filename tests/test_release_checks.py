from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "release_checks.py"


def test_release_evidence_generation_and_verification(tmp_path: Path) -> None:
    wheel = tmp_path / "operant_agent-0.1.0-py3-none-any.whl"
    metadata = b"Name: operant-agent\nVersion: 0.1.0\nRequires-Dist: fastapi<1,>=0.115\n\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("operant_agent-0.1.0.dist-info/METADATA", metadata)
    source = tmp_path / "operant_agent-0.1.0.tar.gz"
    source.write_bytes(b"source-distribution")

    subprocess.run([sys.executable, str(SCRIPT), "generate", "--dist", str(tmp_path)], check=True)
    subprocess.run([sys.executable, str(SCRIPT), "verify", "--dist", str(tmp_path)], check=True)

    manifest = json.loads((tmp_path / "release-manifest.json").read_text())
    assert manifest["signing"] == "not_performed"
    assert manifest["artifacts"][wheel.name] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    sbom = json.loads((tmp_path / "operant-agent.spdx.json").read_text())
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert len(sbom["packages"]) > 1
    assert any(item["relationshipType"] == "DEPENDS_ON" for item in sbom["relationships"])
    assert manifest["distribution_trust"] == "unsigned_candidate_not_for_release"
    assert len(manifest["source_revision"]) == 40


def test_release_verification_rejects_tampering(tmp_path: Path) -> None:
    wheel = tmp_path / "operant_agent-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "operant_agent-0.1.0.dist-info/METADATA",
            b"Name: operant-agent\nVersion: 0.1.0\n\n",
        )
    (tmp_path / "operant_agent-0.1.0.tar.gz").write_bytes(b"source")
    subprocess.run([sys.executable, str(SCRIPT), "generate", "--dist", str(tmp_path)], check=True)
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--dist", str(tmp_path)], check=False
    )
    assert completed.returncode != 0


def test_release_verification_rejects_untracked_distribution_artifact(tmp_path: Path) -> None:
    wheel = tmp_path / "operant_agent-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "operant_agent-0.1.0.dist-info/METADATA",
            b"Name: operant-agent\nVersion: 0.1.0\n\n",
        )
    (tmp_path / "operant_agent-0.1.0.tar.gz").write_bytes(b"source")
    subprocess.run([sys.executable, str(SCRIPT), "generate", "--dist", str(tmp_path)], check=True)
    (tmp_path / "unexpected-0.1.0.tar.gz").write_bytes(b"untracked")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--dist", str(tmp_path)], check=False
    )
    assert completed.returncode != 0


def test_release_verification_rejects_sbom_that_does_not_match_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "operant_agent-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "operant_agent-0.1.0.dist-info/METADATA",
            b"Name: operant-agent\nVersion: 0.1.0\n\n",
        )
    (tmp_path / "operant_agent-0.1.0.tar.gz").write_bytes(b"source")
    subprocess.run([sys.executable, str(SCRIPT), "generate", "--dist", str(tmp_path)], check=True)
    sbom_path = tmp_path / "operant-agent.spdx.json"
    sbom = json.loads(sbom_path.read_text())
    sbom["packages"][0]["versionInfo"] = "9.9.9"
    sbom_path.write_text(json.dumps(sbom))
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--dist", str(tmp_path)], check=False
    )
    assert completed.returncode != 0
