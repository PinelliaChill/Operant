"""Bounded real-model J3 Skill chain on a new synthetic database and workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-id", default="gpt-5.6-luna")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runroot = args.run_root.resolve()
    runroot.mkdir(parents=True, exist_ok=False)
    workspace = runroot / "workspace"
    workspace.mkdir()
    (workspace / "check.txt").write_text(
        "Synthetic verification procedure: first read the version, then compare checksum.\n"
        "The reusable checkword is B26_MAPLE_91.\n"
    )
    from operant.settings import load_local_env

    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    os.environ["OPERANT_DB_PATH"] = str(runroot / "core.sqlite3")
    result: dict = {
        "phase": "b26_real_skill_joint_acceptance",
        "model_id": args.model_id,
        "workspace": str(workspace),
        "database": os.environ["OPERANT_DB_PATH"],
        "source_hashes": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "src/operant").rglob("*.py"))
        },
        "checks": {},
        "completed": False,
    }
    checks = result["checks"]
    try:
        discovery = subprocess.run(
            ["uv", "run", "--frozen", "operant", "model", "discover"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=50,
        )
        if discovery.returncode or args.model_id not in {
            line.strip() for line in discovery.stdout.splitlines()
        }:
            raise RuntimeError("discovery_failed")
        checks["discovery"] = {"entry": "uv run operant model discover", "selected": args.model_id}
        from fastapi.testclient import TestClient

        from operant.api import create_app
        from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
        from operant.domain.threads import ConversationThread

        app = create_app(runroot / "core.sqlite3")
        service = app.state.operant_service
        profile = service.add_model_profile(
            ModelProfile(
                name="B26 synthetic real model",
                model_id=args.model_id,
                base_url=os.environ["OPERANT_BASE_URL"],
                secret_ref="OPERANT_API_KEY",
                context_window=32768,
            )
        )
        with TestClient(app) as client:
            counter = 0

            def command(version="b2-6", **body):
                nonlocal counter
                counter += 1
                response = client.post(
                    f"/v1/{version}/commands",
                    json=body,
                    headers={"Idempotency-Key": f"b26-live-{counter}"},
                )
                if response.status_code != 200:
                    raise RuntimeError(f"command_{body['action']}_http_{response.status_code}")
                return response.json()

            project = command(
                "b2-3",
                action="project_create",
                name="B26 实际模型合成验收",
                workspace_path=str(workspace),
            )["state"]["projects"][-1]["project_id"]
            install = command(
                "b2-3",
                action="plugin_install",
                plugin_id="memory-standard",
                mode="trusted_in_process",
            )["state"]["installations"][-1]["installation_id"]
            command("b2-3", action="binding_select", project_id=project, installation_id=install)
            role = service.create_role(
                RolePreset(
                    name="B26 source observer",
                    model_profile_id=profile.id,
                    system_prompt=(
                        "Use read_file to inspect requested evidence. "
                        "Answer briefly in Chinese. Never write."
                    ),
                    memory_scope="read: [project]; write: []",
                    tool_policy=ToolPolicy(allowed_tools=("read_file",)),
                    budget=Budget(max_turns=3, max_output_tokens=1200, timeout_seconds=90),
                )
            )
            thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
            initial = service.create_session(role.id, thread_id=thread.id)

            async def run(session, target_thread, message):
                return [
                    e
                    async for e in service.run_session(
                        session.id,
                        user_message=message,
                        workspace=str(workspace),
                        thread_id=target_thread.id,
                    )
                ]

            events = client.portal.call(
                run,
                initial,
                thread,
                "读取 check.txt，把文件里的检查流程和检查口令整理成三条可复用步骤。",
            )
            answer = next(
                (
                    str(e.payload.get("content", ""))
                    for e in events
                    if e.event_type == "agent.completed"
                ),
                "",
            )
            assert "B26_MAPLE_91" in answer and any(
                e.event_type == "tool.completed" for e in events
            )
            checks["experience_formed"] = {
                "session_id": initial.id,
                "entry": "ApplicationService.run_session",
                "tool_policy": ["read_file"],
                "completed": True,
                "tool_called": True,
            }
            history = client.get(
                f"/v1/b2-5/projects/{project}/history", params={"query": "B26_MAPLE_91"}
            ).json()
            # Select the actual model-produced canonical Item, not a fixture source.
            candidates = [
                entry
                for entry in history["items"]
                if entry.get("source") and entry["thread_id"] == thread.id
            ]
            source = next(
                (
                    entry["source"]
                    for entry in reversed(candidates)
                    if entry["kind"] == "agent_message"
                ),
                None,
            )
            if source is None:
                raise RuntimeError("canonical_model_source_missing")
            proposed = command(
                action="procedure_propose", project_id=project, content=answer, sources=[source]
            )
            governance = client.get(f"/v1/b2-5/projects/{project}/governance").json()
            entry = next(
                e
                for e in governance["proposals"]
                if e["proposal"]["proposal_id"] in proposed["affected_ids"]
            )
            assert entry["proposal"]["state"] == "pending"
            p = entry["proposal"]
            command(
                "b2-5",
                action="review",
                project_id=project,
                decision="accept",
                selections=[
                    dict(
                        proposal_id=p["proposal_id"],
                        proposal_revision=p["proposal_revision"],
                        proposed_version=p["proposed_version"],
                        base_head_revision=p["base_head"]["revision"],
                    )
                ],
            )
            draft = command(
                action="skill_draft",
                project_id=project,
                procedure_ref=entry["version"]["ref"],
                name="maple-check",
                description="检查版本和摘要的经验步骤",
            )["state"]["skills"]["skills"][0]

            def exact(skill):
                return dict(
                    project_id=project,
                    skill_id=skill["skill_id"],
                    skill_version=skill["version"]["version"],
                    expected_head_revision=skill["head"]["head_revision"],
                    permission_epoch=skill["head"]["permission_epoch"],
                )

            command(action="skill_validate", **exact(draft))
            published = command(action="skill_publish", **exact(draft))["state"]["skills"][
                "skills"
            ][0]
            assert published["head"]["state"] == "published"
            checks["controlled_publication"] = {
                "skill_id": draft["skill_id"],
                "version": 1,
                "artifact_hash": draft["version"]["artifact"]["content_hash"],
                "proposal_required": True,
                "validation_required": True,
            }
            second = command(
                action="skill_draft",
                project_id=project,
                procedure_ref=entry["version"]["ref"],
                skill_id=published["skill_id"],
                expected_head_revision=published["head"]["head_revision"],
                skill_version=1,
                name="maple-check",
                description="检查版本和摘要的经验步骤：第二版",
            )["state"]["skills"]["skills"][0]
            assert second["version"]["version"] == 2
            command(action="skill_validate", **exact(second))
            second = command(action="skill_publish", **exact(second))["state"]["skills"]["skills"][
                0
            ]
            disabled = command(action="skill_disable", **exact(second))["state"]["skills"][
                "skills"
            ][0]
            assert disabled["head"]["state"] == "disabled"
            restored = command(action="skill_rollback", rollback_to_version=1, **exact(disabled))[
                "state"
            ]["skills"]["skills"][0]
            assert restored["head"]["published_version"] == 1
            assert restored["head"]["state"] == "published"
            checks["disable_and_rollback"] = {
                "published_versions": [1, 2],
                "disabled_version": 2,
                "restored_version": 1,
                "head_revision": restored["head"]["head_revision"],
                "entry": "/v1/b2-6/commands",
            }
            reader = service.create_role(
                RolePreset(
                    name="B26 skill reader",
                    model_profile_id=profile.id,
                    system_prompt="依据已授权的技能资料回答，不使用工具。",
                    memory_scope="read: [project]; write: []",
                    tool_policy=ToolPolicy(allowed_tools=()),
                    budget=Budget(max_turns=1, max_output_tokens=512, timeout_seconds=60),
                )
            )
            later_thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
            later = service.create_session(reader.id, thread_id=later_thread.id)
            events = client.portal.call(
                run, later, later_thread, "按当前已发布的检查技能，告诉我操作顺序和检查口令。"
            )
            answer2 = next(
                (
                    str(e.payload.get("content", ""))
                    for e in events
                    if e.event_type == "agent.completed"
                ),
                "",
            )
            revisions = service.store.list_context_revisions(later.id)
            used = any(
                "经验技能" in (m.content or "") and "maple-check" in (m.content or "")
                for revision in revisions
                for m in revision.messages
            )
            assert "B26_MAPLE_91" in answer2 and used
            checks["later_task_reuse"] = {
                "session_id": later.id,
                "entry": "ApplicationService.run_session",
                "tool_policy": [],
                "completed": True,
                "skill_in_actual_context": used,
                "answer_contains_checkword": True,
            }
            command("b2-5", action="source_revoke", project_id=project, source=source)
            calls_before = len(service.store.list_context_revisions(later.id))
            try:
                revoked_events = client.portal.call(run, later, later_thread, "再执行一次。")
                blocked = not any(e.event_type == "model.completed" for e in revoked_events)
            except PermissionError:
                blocked = True
            assert blocked and len(service.store.list_context_revisions(later.id)) == calls_before
            checks["source_revocation_blocks_next_send"] = {
                "blocked": True,
                "new_contexts": 0,
                "historical_context_preserved": bool(revisions),
            }
            result["completed"] = True
    except Exception as exc:
        result["error_class"] = type(exc).__name__
        # Code-owned phase labels only: never persist provider URLs or raw errors.
        if isinstance(exc, RuntimeError) and str(exc).startswith(
            ("command_", "discovery_", "canonical_")
        ):
            result["failure_code"] = str(exc)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "completed": result["completed"],
                    "checks": list(checks),
                    "error_class": result.get("error_class"),
                    "failure_code": result.get("failure_code"),
                },
                ensure_ascii=False,
            )
        )
    if not result["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
