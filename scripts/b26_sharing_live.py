"""Bounded real-model sharing and revocation acceptance on synthetic projects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-id", default="gpt-5.6-luna")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runroot = args.run_root.resolve()
    runroot.mkdir(exist_ok=False, parents=True)
    from operant.settings import load_local_env

    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    os.environ["OPERANT_DB_PATH"] = str(runroot / "core.sqlite3")
    evidence: dict = {
        "model_id": args.model_id,
        "database": os.environ["OPERANT_DB_PATH"],
        "source_hashes": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "src/operant").rglob("*.py"))
        },
        "checks": {},
        "completed": False,
    }
    checks = evidence["checks"]
    try:
        discovery = subprocess.run(
            ["uv", "run", "--frozen", "operant", "model", "discover"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=50,
        )
        if discovery.returncode or args.model_id not in discovery.stdout.splitlines():
            raise RuntimeError("discovery_failed")
        checks["discovery"] = True
        from fastapi.testclient import TestClient

        from operant.api import create_app
        from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
        from operant.domain.threads import ConversationThread
        from operant.memory_plugins.experience_runtime import begin_experience_run

        app = create_app(runroot / "core.sqlite3")
        service = app.state.operant_service
        profile = service.add_model_profile(
            ModelProfile(
                name="B26 sharing real model",
                model_id=args.model_id,
                base_url=os.environ["OPERANT_BASE_URL"],
                secret_ref="OPERANT_API_KEY",
                context_window=32768,
            )
        )
        with TestClient(app) as client:
            serial = 0

            def command(version="b2-6", **body):
                nonlocal serial
                serial += 1
                response = client.post(
                    f"/v1/{version}/commands",
                    json=body,
                    headers={"Idempotency-Key": f"sharing-live-{serial}"},
                )
                if response.status_code != 200:
                    raise RuntimeError(f"command_{body['action']}_http_{response.status_code}")
                return response.json()

            def project(label):
                workspace = runroot / label
                workspace.mkdir()
                for gitargs in (
                    ("init", "-b", "main"),
                    ("config", "user.name", "B26"),
                    ("config", "user.email", "b26@example.invalid"),
                ):
                    subprocess.run(
                        ["git", *gitargs], cwd=workspace, check=True, capture_output=True
                    )
                (workspace / "README.md").write_text(f"Synthetic {label} workspace.\n")
                subprocess.run(
                    ["git", "add", "README.md"], cwd=workspace, check=True, capture_output=True
                )
                subprocess.run(
                    ["git", "commit", "-m", "Synthetic baseline"],
                    cwd=workspace,
                    check=True,
                    capture_output=True,
                )
                p = command(
                    "b2-3",
                    action="project_create",
                    name=f"B26 {label}",
                    workspace_path=str(workspace),
                )["state"]["projects"][-1]
                installation = command(
                    "b2-3",
                    action="plugin_install",
                    plugin_id="memory-standard",
                    mode="trusted_in_process",
                )["state"]["installations"][-1]
                command(
                    "b2-3",
                    action="binding_select",
                    project_id=p["project_id"],
                    installation_id=installation["installation_id"],
                )
                command(
                    action="worktree_register",
                    project_id=p["project_id"],
                    workspace_id=p["workspace_id"],
                )
                return p, workspace

            source, source_workspace = project("source")
            target, target_workspace = project("target")
            command(
                "b2-3",
                action="memory_save",
                project_id=source["project_id"],
                content="合成共享偏好：使用简洁中文回答，检查口令为 B26_CEDAR_72。",
                confirmed=True,
            )
            knowledge = client.get(f"/v1/b2-5/projects/{source['project_id']}/governance").json()[
                "records"
            ][0]["version"]
            source_state = client.get(f"/v1/b2-6/projects/{source['project_id']}/experience").json()
            source_ref = dict(
                source_type="memory_version",
                source_id=knowledge["ref"]["record_id"],
                revision=knowledge["ref"]["version"],
                content_digest=knowledge["ref"]["content_digest"],
                scope=knowledge["scope"],
                permission_epoch=source_state["remote"]["permission_epoch"],
                availability="available",
            )
            personal_result = command(
                action="personal_preference_create",
                project_id=source["project_id"],
                content="合成个人偏好：使用简洁中文回答，检查口令为 B26_CEDAR_72。",
                source_refs=[source_ref],
            )
            manager = service.memory_manager
            assert manager is not None
            # Read the formal immutable row referenced by the command result,
            # without inventing a PersonalScope or bypassing its command.
            personal = next(
                v
                for v in manager.ledger.query(knowledge["ref"]["dataset_id"], None, limit=100)
                if v.scope.kind == "personal"
            )
            checks["explicit_personal_scope"] = {
                "kind": personal.scope.kind,
                "ref": personal.ref.model_dump(mode="json"),
                "affected_ids": personal_result["affected_ids"],
            }

            def role(name):
                return service.create_role(
                    RolePreset(
                        name=name,
                        model_profile_id=profile.id,
                        system_prompt="仅依据当前已授权资料回答。无资料时明确说未知。不要使用工具。",
                        memory_scope="read: [project]; write: []",
                        tool_policy=ToolPolicy(allowed_tools=()),
                        budget=Budget(max_turns=1, max_output_tokens=512, timeout_seconds=60),
                    )
                )

            allowed_role, denied_role = role("authorized reader"), role("ungranted reader")
            granted = command(
                action="grant_create",
                project_id=source["project_id"],
                grant=dict(
                    project_id=source["project_id"],
                    source_dataset_id=personal.ref.dataset_id,
                    source_scope=personal.scope.model_dump(mode="json"),
                    target_scope=dict(
                        kind="workspace",
                        project_id=target["project_id"],
                        workspace_id=target["workspace_id"],
                    ),
                    subject_id=allowed_role.id,
                    grantor_id=personal.owner.principal_id,
                    purpose="recall",
                    memory_refs=[personal.ref.model_dump(mode="json")],
                    permission_epoch=source_state["remote"]["permission_epoch"],
                    expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                ),
            )
            grant = next(
                g
                for g in granted["state"]["sharing"]["grants"]
                if g["subject_id"] == allowed_role.id
            )
            thread = service.create_thread(ConversationThread(workspace_ref=str(target_workspace)))
            denied = service.create_session(denied_role.id, thread_id=thread.id)
            denied_agent = service.factory.create_agent(denied.id)
            denied_run = begin_experience_run(
                manager,
                workspace=str(target_workspace),
                session_id=denied.id,
                agent_id=denied_agent.id,
                snapshot=denied.role_snapshot,
                enabled=True,
            )
            assert denied_run is not None and "B26_CEDAR_72" not in denied_run.content()
            checks["ungranted_role_excluded"] = True
            thread = service.create_thread(ConversationThread(workspace_ref=str(target_workspace)))
            allowed = service.create_session(allowed_role.id, thread_id=thread.id)

            async def run(message):
                return [
                    e
                    async for e in service.run_session(
                        allowed.id,
                        user_message=message,
                        workspace=str(target_workspace),
                        thread_id=thread.id,
                    )
                ]

            events = client.portal.call(run, "当前授权共享给我的个人偏好和检查口令是什么？")
            answer = next(
                (
                    str(e.payload.get("content", ""))
                    for e in events
                    if e.event_type == "agent.completed"
                ),
                "",
            )
            revisions = service.store.list_context_revisions(allowed.id)
            assert "B26_CEDAR_72" in answer
            assert any(
                "授权共享知识" in (m.content or "") and "B26_CEDAR_72" in (m.content or "")
                for r in revisions
                for m in r.messages
            )
            checks["real_cross_project_read"] = {
                "session_id": allowed.id,
                "context_revisions": len(revisions),
                "entry": "ApplicationService.run_session",
                "answer_contains_checkword": True,
            }
            command(
                action="grant_revoke",
                project_id=source["project_id"],
                grant_id=grant["grant_id"],
                expected_revision=grant["revision"],
                reason="合成验收撤销",
            )
            events = client.portal.call(run, "再次重复当前共享偏好。")
            assert not any(e.event_type == "agent.completed" for e in events)
            assert len(service.store.list_context_revisions(allowed.id)) == len(revisions)
            checks["revocation_blocks_next_send"] = True
            evidence["completed"] = True
    except Exception as exc:
        evidence["error_class"] = type(exc).__name__
        if isinstance(exc, RuntimeError) and str(exc).startswith(("command_", "discovery_")):
            evidence["failure_code"] = str(exc)
    finally:
        args.output.parent.mkdir(exist_ok=True, parents=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "completed": evidence["completed"],
                    "checks": list(checks),
                    "error_class": evidence.get("error_class"),
                    "failure_code": evidence.get("failure_code"),
                }
            )
        )
    if not evidence["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
