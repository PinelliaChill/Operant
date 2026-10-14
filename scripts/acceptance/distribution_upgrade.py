"""Replay a stopped old Core snapshot through candidate upgrade and rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
from distribution import DATABASE, restore, verify  # noqa: E402

BUSINESS_TABLES = ("model_profiles", "role_heads", "role_versions", "sessions")
MEMORY_READBACK = r"""
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from operant.api import create_app
data = Path(sys.argv[1])
project = sys.argv[2]
app = create_app(data / 'operant.sqlite3')
with TestClient(app) as client:
    management = client.get('/v1/b2-3/management')
    assert management.status_code == 200, management.status_code
    body = management.json()
    assert project in json.dumps(body)
    project_state = next(item for item in body['projects'] if item['project_id'] == project)
    enable = client.post('/v1/b2-3/commands',
        json={'action':'plugin_enable','installation_id':project_state['installation_id']},
        headers={'Idempotency-Key':'distribution-enable-' + sys.argv[3]})
    assert enable.status_code == 200, (enable.status_code, enable.json())
    found = client.post('/v1/b2-3/commands',
        json={'action':'memory_search','project_id':project,'query':'DISTRIBUTION_MEMORY_KEY'},
        headers={'Idempotency-Key':'distribution-readback-' + sys.argv[3]})
    assert found.status_code == 200, (found.status_code, found.json())
    assert 'ORCHID_64' in json.dumps(found.json())
print(json.dumps({'management': True, 'memory_found': True}))
"""


def _business_rows(database: Path) -> dict[str, list[list[object]]]:
    with sqlite3.connect(database) as connection:
        return {
            table: [list(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in BUSINESS_TABLES
        }


def _schema_version(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        return int(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])


def _run(binary: Path, data: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["OPERANT_DB_PATH"] = str(data / DATABASE)
    return subprocess.run(
        [str(binary), *arguments],
        cwd=data,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _memory_readback(venv: Path, data: Path, project_id: str, phase: str) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["OPERANT_DB_PATH"] = str(data / DATABASE)
    result = subprocess.run(
        [str(venv / "bin" / "python"), "-I", "-c", MEMORY_READBACK, str(data), project_id, phase],
        cwd=data,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"memory readback {phase} failed: {result.stderr[-2200:]}")
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "management": True,
        "memory_found": True,
    }


def run(
    backup: Path,
    old_venv: Path,
    candidate_venv: Path,
    session_id: str,
    project_id: str,
    root: Path,
) -> dict[str, object]:
    if not all(path.is_absolute() for path in (backup, old_venv, candidate_venv, root)):
        raise ValueError("all paths must be absolute")
    old_version = verify(backup)["operant_schema_version"]
    assert old_version in {20, 22}
    original_rows = _business_rows(backup / DATABASE)
    assert all(original_rows.values()), "the old snapshot needs real business rows"
    original_artifact = backup / "artifacts" / "migration-sample.txt"
    assert original_artifact.is_file(), "the old snapshot needs an external artifact"
    registry = backup / "memory-plugins" / "registry.json"
    assert registry.is_file(), "the old snapshot needs the installed memory registry"
    snapshot_hash = _sha(backup / DATABASE)

    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    upgraded = root / "upgraded"
    rollback = root / "rollback"
    restore(backup, upgraded)
    _run(candidate_venv / "bin" / "operant", upgraded, "init")
    assert _schema_version(upgraded / DATABASE) == 23
    assert _business_rows(upgraded / DATABASE) == original_rows
    assert _sha(upgraded / "artifacts" / original_artifact.name) == _sha(original_artifact)
    assert _sha(upgraded / "memory-plugins" / "registry.json") == _sha(registry)
    candidate_session = json.loads(
        _run(candidate_venv / "bin" / "operant", upgraded, "session", "show", session_id).stdout
    )
    assert candidate_session["id"] == session_id
    _memory_readback(candidate_venv, upgraded, project_id, "upgraded")

    restore(backup, rollback)
    _run(old_venv / "bin" / "operant", rollback, "init")
    assert _schema_version(rollback / DATABASE) == old_version
    assert _business_rows(rollback / DATABASE) == original_rows
    assert _sha(rollback / "artifacts" / original_artifact.name) == _sha(original_artifact)
    assert _sha(rollback / "memory-plugins" / "registry.json") == _sha(registry)
    old_session = json.loads(
        _run(old_venv / "bin" / "operant", rollback, "session", "show", session_id).stdout
    )
    assert old_session["id"] == session_id
    _memory_readback(old_venv, rollback, project_id, "rollback")
    assert _sha(backup / DATABASE) == snapshot_hash
    return {
        "old_schema": old_version,
        "upgraded_schema": 23,
        "restored_schema": old_version,
        "business_row_counts": {table: len(rows) for table, rows in original_rows.items()},
        "business_rows_equal": True,
        "artifact_sha256": _sha(original_artifact),
        "artifact_equal": True,
        "memory_registry_equal": True,
        "memory_management_and_search": True,
        "old_session_readback": old_session["id"] == session_id,
        "candidate_session_readback": candidate_session["id"] == session_id,
        "backup_unchanged": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--old-venv", type=Path, required=True)
    parser.add_argument("--candidate-venv", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(
        args.backup,
        args.old_venv,
        args.candidate_venv,
        args.session_id,
        args.project_id,
        args.run_root,
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
