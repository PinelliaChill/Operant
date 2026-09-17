#!/usr/bin/env python3
"""Run the bounded B2-7 migration and real-isolation drill.

The drill creates a fresh synthetic v14 database, copies it to a separate
upgrade target, and records the v14 -> v18 migration readback.  It also starts
one tiny plugin through the real macOS ``sandbox-exec`` probe when available.
No configured database, environment file, provider credential, or user path is
opened.  If the platform cannot provide an actual sandbox, the isolation
result is recorded as ``blocked`` and the script never falls back to a plain
subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from operant.contracts.b2_1 import (  # noqa: E402
    Certification,
    PluginManifest,
    RunScope,
)
from operant.domain.commands import WorkspaceInitialization  # noqa: E402
from operant.domain.memory import Memory, MemoryKind, MemoryStatus  # noqa: E402
from operant.domain.models import utc_now  # noqa: E402
from operant.domain.threads import (  # noqa: E402
    ConversationThread,
    Item,
    Turn,
    UserMessagePayload,
)
from operant.persistence.sqlite import MigrationError, SQLiteStore  # noqa: E402
from operant.plugins import (  # noqa: E402
    HostBudget,
    PluginError,
    PluginHost,
    PluginRegistry,
    SandboxProbe,
    compute_package_digest,
)


def _workspace(path: Path) -> WorkspaceInitialization:
    path.mkdir(parents=True, exist_ok=True)
    reference = str(path.resolve())
    return WorkspaceInitialization(
        workspace_ref=reference,
        workspace_hash=hashlib.sha256(reference.encode("utf-8")).hexdigest(),
        readable=True,
        writable=True,
    )


def _seed_legacy_database(path: Path, run_root: Path) -> dict[str, str]:
    """Create a v14-only database with two synthetic legacy records."""

    store = SQLiteStore(path)
    if store.migrate(target_version=14) != 14:
        raise RuntimeError("synthetic source database did not reach schema v14")
    project_workspace = run_root / "legacy-project-workspace"
    other_workspace = run_root / "legacy-other-workspace"
    project = store.register_workspace(_workspace(project_workspace))[0]
    store.register_workspace(_workspace(other_workspace))
    thread = store.create_thread(ConversationThread(workspace_ref=project.workspace_ref))
    turn = store.create_turn(Turn(thread_id=thread.id))
    store.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="B2-7 legacy history marker", author_ref="synthetic"),
        )
    )
    memory = store.create_memory(
        Memory(
            id="b27-legacy-memory",
            kind=MemoryKind.PROJECT,
            content="B2-7 legacy project fact",
            project_scope=project.workspace_ref,
            source_task="B2-7 synthetic migration drill",
            confidence=0.9,
            status=MemoryStatus.ACTIVE,
        )
    )
    store.update_memory(memory.id, content="B2-7 legacy project fact (revision 2)")
    return {"workspace_ref": project.workspace_ref, "memory_id": memory.id}


def _table_snapshot(path: Path, tables: tuple[str, ...]) -> tuple[dict[str, int], str]:
    """Return counts and a hash of selected canonical rows for migration QA."""

    snapshots: dict[str, list[list[Any]]] = {}
    counts: dict[str, int] = {}
    with sqlite3.connect(path) as connection:
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            snapshots[table] = [list(row) for row in rows]
            counts[table] = len(rows)
            if not columns:
                raise RuntimeError(f"expected migration table is missing: {table}")
    encoded = json.dumps(snapshots, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return counts, hashlib.sha256(encoded).hexdigest()


def _migration_drill(run_root: Path) -> dict[str, Any]:
    source = run_root / "legacy-v14.sqlite3"
    upgraded = run_root / "upgrade-v18.sqlite3"
    seed = _seed_legacy_database(source, run_root)
    shutil.copy2(source, upgraded)
    selected_tables = (
        "workspace_initializations",
        "threads",
        "items",
        "memories",
        "memory_versions",
    )
    before_counts, before_hash = _table_snapshot(source, selected_tables)

    upgrade_store = SQLiteStore(upgraded)
    upgrade_store.initialize()
    target_version = upgrade_store.schema_version()
    after_counts, after_hash = _table_snapshot(upgraded, selected_tables)
    applied = upgrade_store.list_applied_migrations()
    if target_version != 18 or [row["version"] for row in applied] != list(range(1, 19)):
        raise RuntimeError("synthetic upgrade did not reach the v18 migration history")
    if before_counts != after_counts or before_hash != after_hash:
        raise RuntimeError("legacy canonical rows changed during the v14 -> v18 upgrade")

    rollback_empty = run_root / "rollback-empty-v18.sqlite3"
    rollback_store = SQLiteStore(rollback_empty)
    rollback_store.initialize()
    rollback_empty_version = rollback_store.rollback(17, isolated=True)

    rollback_populated = run_root / "rollback-populated-v18.sqlite3"
    populated_store = SQLiteStore(rollback_populated)
    populated_store.initialize()
    with populated_store._connect() as connection:  # noqa: SLF001 - isolated drill fixture
        connection.execute(
            "INSERT INTO b26_commands(command_id, project_id, request_digest, state, result) "
            "VALUES (?, ?, ?, ?, ?)",
            ("b27-drill-command", "b27-drill-project", "a" * 64, "completed", "{}"),
        )
    try:
        populated_store.rollback(17, isolated=True)
    except MigrationError as error:
        rollback_populated_status = {
            "status": "blocked",
            "reason_code": "experience_evidence_present",
            "error_type": type(error).__name__,
            "schema_version_after_attempt": populated_store.schema_version(),
        }
    else:
        raise RuntimeError("populated v18 rollback unexpectedly succeeded")

    return {
        "status": "passed",
        "source_database": str(source),
        "source_schema_version": 14,
        "source_created_by": "SQLiteStore.migrate(target_version=14)",
        "upgrade_database": str(upgraded),
        "upgrade_target_schema_version": target_version,
        "canonical_tables": list(selected_tables),
        "seed": seed,
        "pre_migration_counts": before_counts,
        "post_migration_counts": after_counts,
        "pre_migration_canonical_hash": before_hash,
        "post_migration_canonical_hash": after_hash,
        "applied_versions": [row["version"] for row in applied],
        "empty_rollback": {
            "database": str(rollback_empty),
            "target_version": 17,
            "status": "passed" if rollback_empty_version == 17 else "failed",
            "isolated_flag": True,
        },
        "populated_rollback": {
            "database": str(rollback_populated),
            **rollback_populated_status,
        },
    }


def _write_isolated_package(package: Path) -> tuple[PluginManifest, Certification, Path]:
    package.mkdir(parents=True, exist_ok=False)
    (package / "dependencies.json").write_text('{"stdlib_only":true}\n', encoding="utf-8")
    (package / "permissions.json").write_text('{"capabilities":[]}\n', encoding="utf-8")
    worker = package / "worker.py"
    worker.write_text(
        "import json, pathlib, sys\n"
        "marker = pathlib.Path(sys.argv[1])\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    marker.write_text('real-isolated-worker-ran', encoding='utf-8')\n"
        "    context = request['params']['context']\n"
        "    result = {'request_id': context['request_id'],\n"
        "              'sdk_version': 'operant-memory-sdk.v1',\n"
        "              'state': 'ready', 'checkpoint_ref': None}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'],\n"
        "                      'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    dependencies = (package / "dependencies.json").read_bytes()
    permissions = (package / "permissions.json").read_bytes()
    manifest = PluginManifest(
        plugin_id="b27.drill.plugin",
        plugin_version="1.0.0",
        sdk_version="operant-memory-sdk.v1",
        host_api_versions=("operant-memory-sdk.v1",),
        package_digest=compute_package_digest(package),
        dependencies_digest=hashlib.sha256(dependencies).hexdigest(),
        permissions_digest=hashlib.sha256(permissions).hexdigest(),
        entrypoint="worker.py",
        config_schema_ref="b27.drill.config.v1",
        config_schema_digest="a" * 64,
        state_schema_version="b27.drill.state.v1",
        capabilities=("extract",),
        memory_mb=64,
        max_rpc_bytes=128_000,
        max_concurrency=1,
        export_supported=True,
        import_supported=True,
        recoverable=True,
    )
    now = utc_now()
    certification = Certification(
        certification_id="b27.drill.cert.v1",
        issuer_id="issuer.local",
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.plugin_version,
        package_digest=manifest.package_digest,
        dependencies_digest=manifest.dependencies_digest,
        permissions_digest=manifest.permissions_digest,
        lifecycle_evidence_digest="b" * 64,
        allowed_modes=("isolated",),
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        revocation_epoch=0,
        state="valid",
        signature_ref="b27.drill.signature",
    )
    return manifest, certification, worker


async def _real_isolation_drill(run_root: Path) -> dict[str, Any]:
    managed = run_root / "plugin-managed"
    package = run_root / "plugin-source" / "package"
    manifest, certification, _worker = _write_isolated_package(package)
    registry = PluginRegistry(managed, trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=certification)
    installed_worker = registry.package_path(installation.installation_id) / "worker.py"
    marker = registry.state_path_for(installation.installation_id) / "worker-ran"
    host = PluginHost(
        registry,
        stdio_commands={
            installation.installation_id: (
                sys.executable,
                "-I",
                "-S",
                str(installed_worker),
                str(marker),
            )
        },
        budget=HostBudget(timeout_seconds=5, max_idle_seconds=5),
        sandbox_probe=SandboxProbe(),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    result: dict[str, Any] = {
        "platform": platform.platform(),
        "runner": "/usr/bin/sandbox-exec",
        "package": str(registry.package_path(installation.installation_id)),
        "status": "blocked",
        "isolation_claim": "real_sandbox_exec_only",
    }
    try:
        admission = await host.start(installation.installation_id, mode="isolated")
        lease = host.start_run(
            binding.binding_id,
            run_id="b27-isolated-run",
            scope=RunScope(
                kind="run",
                project_id="b27-project",
                workspace_id="b27-workspace",
                run_id="b27-isolated-run",
                writer_id=None,
            ),
        )
        lifecycle = await host.lifecycle(lease, operation="health")
        process = host._engines[installation.installation_id].engine.process  # noqa: SLF001
        result.update(
            {
                "status": "passed",
                "mode": admission.mode,
                "evidence_ref": admission.isolation_evidence_ref,
                "process_pid": process.pid if process is not None else None,
                "lifecycle_state": lifecycle.state,
                "managed_write_observed": marker.read_text(encoding="utf-8")
                if marker.is_file()
                else None,
            }
        )
    except PluginError as error:
        result.update(
            {
                "status": "blocked" if error.code == "isolation_unavailable" else "failed",
                "reason_code": error.code,
                "error_type": type(error).__name__,
            }
        )
    finally:
        try:
            await host.close()
        except PluginError as error:
            result.update(
                {
                    "status": "failed",
                    "close_reason_code": error.code,
                    "close_error_type": type(error).__name__,
                }
            )
    return result


def _git_head() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value if len(value) == 40 else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    output = args.output.resolve()
    if not run_root.is_absolute() or not output.is_absolute():
        raise SystemExit("run-root and output must be absolute paths")
    run_root.mkdir(parents=True, exist_ok=False)
    migration = _migration_drill(run_root)
    isolation = asyncio.run(_real_isolation_drill(run_root))
    evidence = {
        "evidence_head": _git_head(),
        "source_root": str(REPOSITORY_ROOT),
        "run_root": str(run_root),
        "database_scope": "synthetic_only",
        "credentials_scope": "none_loaded",
        "migration": migration,
        "real_isolation": isolation,
        "limitations": [
            "This drill does not prove a real provider/model or desktop GUI flow.",
            "PluginRegistry exposes explicit immutable installations rather than one atomic "
            "package-upgrade command; same-dataset replacement requires a retained dataset "
            "and no active old Run.",
            "A blocked real sandbox result is not an isolation acceptance.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
