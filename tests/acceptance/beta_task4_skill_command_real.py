"""Real gpt-6-luna acceptance for explicit-only Skill through formal Session API.

Uses an absolute synthetic workspace and the original local environment only in
this process. Evidence contains status flags and hashes, never credentials or
model prompt/response text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from operant.api import create_app
from operant.application.security import PolicyEngine
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.domain.threads import ConversationThread
from operant.protocol import canonical_action_hash
from operant.settings import load_local_env

MARKER = "BLUEPRINT_42"
MODEL = "gpt-6-luna"


def _allow() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="task4-explicit-skill-acceptance",
            version="test.v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="isolated-read",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="synthetic acceptance workspace",
                ),
            ),
        )
    )


async def main(evidence_dir: Path) -> None:
    if not evidence_dir.is_absolute():
        raise ValueError("evidence directory must be absolute")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    workspace = evidence_dir / "workspace"
    workspace.mkdir()
    source = evidence_dir / "source"
    package = source / "explicit-only"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: explicit-only\ndescription: synthetic explicit command acceptance\n"
        "disable-model-invocation: true\n---\n"
        f"When explicitly selected, answer with exactly {MARKER}. Do not use tools.\n",
        encoding="utf-8",
    )
    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    base_url = os.environ["OPERANT_BASE_URL"]
    if not os.environ.get("OPERANT_API_KEY"):
        raise RuntimeError("OPERANT_API_KEY is unavailable")
    evidence: dict[str, object] = {
        "status": "failed",
        "model_id": MODEL,
        "entry": "Phase56 executeSkillCommand -> ApplicationService.run_session",
        "workspace_absolute": str(workspace.resolve()).startswith("/"),
        "synthetic_only": True,
    }
    app = create_app(
        evidence_dir / "core.sqlite3",
        artifact_root=evidence_dir / "artifacts",
        phase45_skill_roots={"synthetic": source},
        phase45_policy_engine=_allow(),
        phase56_local_authorizer=lambda _request: True,
    )
    service = app.state.operant_service
    manager = service.memory_manager_factory()
    try:
        discovered = await service.discover_models(base_url=base_url, secret_ref="OPERANT_API_KEY")
        if MODEL not in discovered:
            raise RuntimeError("formal Discovery did not return exact gpt-6-luna")
        evidence["discovery_exact_match"] = True
        actual_provider = service.provider.delegate
        marker_seen: list[bool] = []

        class ObservedProvider:
            async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
                return await actual_provider.list_models(base_url=base_url, secret_ref=secret_ref)

            async def stream(self, *, snapshot: object, messages: object, tools: object):
                marker_seen.append(MARKER in "\n".join(m.model_dump_json() for m in messages))
                async for event in actual_provider.stream(
                    snapshot=snapshot, messages=messages, tools=tools
                ):
                    yield event

        service.provider.delegate = ObservedProvider()
        project = await manager.execute(
            ManagementCommand(
                action="project_create", name="synthetic-skill", workspace_path=str(workspace)
            )
        )
        project_id = project.state.projects[-1].project_id
        await manager.execute(ManagementCommand(action="skill_discover"))
        package_ref = manager.projection().skill_catalog[0].package_ref
        installed = await manager.execute(
            ManagementCommand(action="skill_install", package_ref=package_ref)
        )
        skill_id = installed.state.skills[0].skill_id
        await manager.execute(
            ManagementCommand(action="skill_enable", skill_id=skill_id, project_id=project_id)
        )
        profile = service.add_model_profile(
            ModelProfile(
                name="task4-explicit-skill-real",
                model_id=MODEL,
                base_url=base_url,
                secret_ref="OPERANT_API_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
            )
        )
        role = service.create_role(
            RolePreset(
                name="task4-explicit-skill-real",
                system_prompt="Answer the current user request concisely. Do not use tools.",
                model_profile_id=profile.id,
                effort=Effort.LOW,
                tool_policy=ToolPolicy(allowed_tools=()),
                budget=Budget(max_turns=2, timeout_seconds=120, max_output_tokens=128),
                memory_scope="none",
            )
        )
        ordinary_thread = service.create_thread(
            ConversationThread(workspace_ref=str(workspace.resolve()))
        )
        ordinary_session = service.create_session(role.id, thread_id=ordinary_thread.id)
        ordinary_events = [
            event
            async for event in service.run_session(
                ordinary_session.id,
                user_message="Say ready.",
                workspace=workspace,
                thread_id=ordinary_thread.id,
            )
        ]
        if not any(event.event_type == "agent.completed" for event in ordinary_events):
            raise RuntimeError("ordinary formal Run did not complete")
        ordinary_request_count = len(marker_seen)
        if ordinary_request_count == 0 or any(marker_seen):
            raise AssertionError("disable-model-invocation did not exclude the ordinary Run")
        thread = service.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))
        session = service.create_session(role.id, thread_id=thread.id)
        prompt = "Invoke the selected Skill and give its exact answer."
        body = {
            "command": f"skill:{skill_id}",
            "arguments": {"prompt": prompt},
            "idempotency_key": "real-skill-command-once",
        }
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=180
        ) as client:
            listed = await client.get(f"/v1/workbench/threads/{thread.id}/skill-commands")
            if listed.status_code != 200 or not any(
                item["command"] == body["command"] for item in listed.json()["commands"]
            ):
                raise AssertionError("explicit Skill was not discovered")
            response = await client.post(
                f"/v1/workbench/threads/{thread.id}/skill-commands",
                json=body,
                headers={"Idempotency-Key": body["idempotency_key"]},
            )
            replay = await client.post(
                f"/v1/workbench/threads/{thread.id}/skill-commands",
                json=body,
                headers={"Idempotency-Key": body["idempotency_key"]},
            )
        response_body = response.json()
        evidence.update(
            {
                "skill_id": skill_id,
                "prompt_sha256": canonical_action_hash({"prompt": prompt}),
                "http_status": response.status_code,
                "command_status": response_body.get("status"),
                "answer_marker_present": MARKER in str(response_body.get("result", "")),
                "ordinary_context_excluded": all(
                    not value for value in marker_seen[:ordinary_request_count]
                ),
                "explicit_context_included": len(marker_seen) > ordinary_request_count
                and all(marker_seen[ordinary_request_count:]),
                "model_requests": len(marker_seen),
                "replay_same": response_body == replay.json(),
                "history_started": any(
                    e.event_type == "skill.command.started" for e in service.list_events(session.id)
                ),
                "history_completed": any(
                    e.event_type == "skill.command.completed"
                    for e in service.list_events(session.id)
                ),
                "session_completed": any(
                    e.event_type == "agent.completed" for e in service.list_events(session.id)
                ),
            }
        )
        if not (
            response.status_code == 200
            and response_body.get("status") == "completed"
            and evidence["answer_marker_present"]
            and evidence["explicit_context_included"]
            and evidence["replay_same"]
            and evidence["history_started"]
            and evidence["history_completed"]
            and evidence["session_completed"]
        ):
            raise AssertionError("formal explicit Skill command acceptance failed")
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        raise
    finally:
        (evidence_dir / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await manager.close()
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    asyncio.run(main(parser.parse_args().evidence_dir))
