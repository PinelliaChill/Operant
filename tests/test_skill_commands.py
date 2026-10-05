"""Explicit Skill commands use installed bytes and the ordinary Session kernel."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from operant.api import create_app
from operant.api_workbench_context import thread_session
from operant.application.security import PolicyEngine
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolDefinition
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.domain.threads import ConversationThread
from operant.memory_plugins.manager import MemoryManager
from operant.providers.base import ModelProvider


class SkillProvider(ModelProvider):
    def __init__(self) -> None:
        self.messages: list[tuple[Message, ...]] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["skill-test-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        self.messages.append(tuple(messages))
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="Skill command completed.", finish_reason="stop"),
        )


def _allow() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="skill-command-test",
            version="test.v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="test-read",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="test only",
                ),
            ),
        )
    )


def _ask() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="skill-command-ask-test",
            version="test.v1",
            default_decision=PolicyDecision.ASK,
            rules=(),
        )
    )


async def _setup(
    tmp_path: Path, *, policy: PolicyEngine | None = None
) -> tuple[Any, Any, Any, str, str, str, SkillProvider]:
    source = tmp_path / "source"
    package = source / "explicit-only"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: explicit-only\ndescription: selected by the user\n"
        "disable-model-invocation: true\n---\nSKILL_EXPLICIT_MARKER_42\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path / "core.sqlite3",
        phase45_skill_roots={"test": source},
        phase45_policy_engine=policy or _allow(),
    )
    service = app.state.operant_service
    provider = SkillProvider()
    service.provider = provider
    manager = service.memory_manager_factory()
    project = await manager.execute(
        ManagementCommand(action="project_create", name="skill-test", workspace_path=str(tmp_path))
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
            name="Skill command test",
            model_id="skill-test-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_SKILL_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="Skill command role",
            system_prompt="Follow the selected Skill and answer concisely.",
            model_profile_id=profile.id,
            memory_scope="read: [project]; write: []",
            budget=Budget(max_turns=2, timeout_seconds=10),
            tool_policy=ToolPolicy(allowed_tools=()),
        )
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    service.create_session(role.id, thread_id=thread.id)
    return app, service, manager, thread.id, skill_id, project_id, provider


@pytest.mark.asyncio
async def test_skill_command_explicit_only_run_replay_and_history(tmp_path: Path) -> None:
    app, service, manager, thread_id, skill_id, project_id, provider = await _setup(tmp_path)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = f"/v1/workbench/threads/{thread_id}/skill-commands"
            listing = await client.get(url)
            assert listing.status_code == 200, listing.text
            assert listing.json()["commands"][0]["command"] == f"skill:{skill_id}"
            body = {
                "command": f"skill:{skill_id}",
                "arguments": {"prompt": "Use the selected Skill to answer."},
                "idempotency_key": "skill-once",
            }
            invalid = await client.post(
                url, json={**body, "arguments": {"prompt": "x", "tool": "exec"}}
            )
            assert invalid.status_code == 422
            assert provider.messages == []
            first = await client.post(url, json=body)
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "completed"
            assert first.json()["result"] == "Skill command completed."
            joined = "\n".join(m.model_dump_json() for m in provider.messages[0])
            assert "SKILL_EXPLICIT_MARKER_42" in joined
            assert len(provider.messages) == 1
            repeated = await client.post(url, json=body)
            assert repeated.json() == first.json()
            assert len(provider.messages) == 1
            conflict = await client.post(url, json={**body, "arguments": {"prompt": "different"}})
            assert conflict.status_code == 409
            events = service.list_events(first.json()["resource_id"])
            assert any(event.event_type == "agent.completed" for event in events)
            await manager.execute(
                ManagementCommand(action="skill_disable", skill_id=skill_id, project_id=project_id)
            )
            assert (await client.get(url)).json()["commands"] == []
            disabled = await client.post(url, json={**body, "idempotency_key": "after-disable"})
            assert disabled.status_code == 403
            assert len(provider.messages) == 1
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_skill_ask_waits_for_run_admission_and_releases_failed_admission(
    tmp_path: Path,
) -> None:
    app, service, manager, thread_id, skill_id, _, provider = await _setup(tmp_path, policy=_ask())
    session = thread_session(service, thread_id)
    body = {
        "command": f"skill:{skill_id}",
        "arguments": {"prompt": "Run the approved Skill."},
        "idempotency_key": "skill-approved-once",
    }
    url = f"/v1/workbench/threads/{thread_id}/skill-commands"
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert service.admit_session_run(session.id)
            try:
                busy = await client.post(url, json=body)
                assert busy.status_code == 409
                assert busy.json()["detail"]["code"] == "session_run_conflict"
                with service.store._connect() as connection:
                    assert (
                        connection.execute(
                            "SELECT COUNT(*) FROM phase45_approval_requests"
                        ).fetchone()[0]
                        == 0
                    )
            finally:
                service.release_session_run(session.id)

            asked = await client.post(url, json=body)
            assert asked.status_code == 409, asked.text
            detail = asked.json()["detail"]
            assert detail["code"] == "approval_required"
            assert service.admitted_session_run_lease(session.id) is None
            approved = await client.post(
                f"/v1/security/approvals/{detail['approval_id']}",
                json={"approved": True, "reason_code": "user-confirmed"},
            )
            assert approved.status_code == 200, approved.text

            assert service.admit_session_run(session.id)
            try:
                still_busy = await client.post(url, json=body)
                assert still_busy.status_code == 409
                assert still_busy.json()["detail"]["code"] == "session_run_conflict"
                approval = await client.get(f"/v1/security/approvals/{detail['approval_id']}")
                assert approval.json()["status"] == "approved"
            finally:
                service.release_session_run(session.id)

            completed = await client.post(url, json=body)
            assert completed.status_code == 200, completed.text
            assert completed.json()["status"] == "completed"
            approval = await client.get(f"/v1/security/approvals/{detail['approval_id']}")
            assert approval.json()["status"] == "consumed"
            assert len(provider.messages) == 1

            gateway = app.state.phase45_action_gateway
            original_guard = gateway.guard

            def fail_guard(**_kwargs: Any) -> Any:
                raise RuntimeError("synthetic pre-journal failure")

            gateway.guard = fail_guard
            try:
                errored = await client.post(
                    url, json={**body, "idempotency_key": "skill-guard-error"}
                )
                assert errored.status_code == 409
                assert errored.json()["detail"]["code"] == "command_outcome_unknown"
                assert service.admitted_session_run_lease(session.id) is None
            finally:
                gateway.guard = original_guard
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_skill_command_changed_bytes_and_unknown_are_not_replayed(tmp_path: Path) -> None:
    app, service, manager, thread_id, skill_id, _, provider = await _setup(tmp_path)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = f"/v1/workbench/threads/{thread_id}/skill-commands"
            body = {
                "command": f"skill:{skill_id}",
                "arguments": {"prompt": "Run the Skill."},
                "idempotency_key": "unknown-once",
            }

            async def fail_run(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
                raise RuntimeError("lost after admission")
                yield

            original = service.run_session
            service.run_session = fail_run
            failed = await client.post(url, json=body)
            assert failed.status_code == 409
            assert failed.json()["detail"]["code"] == "command_outcome_unknown"
            service.run_session = original
            replay = await client.post(url, json=body)
            assert replay.status_code == 409
            assert replay.json()["detail"]["code"] == "command_outcome_unknown"
            assert provider.messages == []
            (manager.root / "skills" / skill_id / "SKILL.md").write_text("changed")
            assert (await client.get(url)).json()["commands"] == []
            changed = await client.post(url, json={**body, "idempotency_key": "changed"})
            assert changed.status_code == 403
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["bytes", "authorization"])
async def test_skill_command_rechecks_approved_snapshot_before_model(
    tmp_path: Path, change: str
) -> None:
    app, service, manager, thread_id, skill_id, _, provider = await _setup(tmp_path)
    original_admit = service.admit_session_run

    def changed_after_approval(session_id: str, **kwargs: Any) -> bool:
        if change == "bytes":
            (manager.root / "skills" / skill_id / "SKILL.md").write_text(
                "---\nname: changed\ndescription: changed\n---\nMUTATED\n",
                encoding="utf-8",
            )
        else:
            manager._state["skills"][0]["state"] = "disabled"
            manager._save()
        return original_admit(session_id, **kwargs)

    service.admit_session_run = changed_after_approval
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = f"/v1/workbench/threads/{thread_id}/skill-commands"
            body = {
                "command": f"skill:{skill_id}",
                "arguments": {"prompt": "Run only approved Skill bytes."},
                "idempotency_key": f"changed-{change}",
            }
            first = await client.post(url, json=body)
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "failed"
            assert provider.messages == []
            events = service.list_events(first.json()["resource_id"])
            assert any(event.event_type == "skill.command.started" for event in events)
            assert any(event.event_type == "skill.command.failed" for event in events)
            assert (await client.post(url, json=body)).json() == first.json()
            assert provider.messages == []
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_skill_command_reads_other_manager_revocation_from_store(tmp_path: Path) -> None:
    app, service, manager, thread_id, skill_id, project_id, provider = await _setup(tmp_path)
    external = MemoryManager(service, host=manager.host)
    try:
        await external.execute(
            ManagementCommand(action="skill_disable", skill_id=skill_id, project_id=project_id)
        )
        assert manager._state["skills"][0]["state"] == "installed"
        assert project_id in manager._state["skills"][0]["project_ids"]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = f"/v1/workbench/threads/{thread_id}/skill-commands"
            listing = await client.get(url)
            assert listing.status_code == 200
            assert listing.json()["commands"] == []
            denied = await client.post(
                url,
                json={
                    "command": f"skill:{skill_id}",
                    "arguments": {"prompt": "Must not run after remote disable."},
                    "idempotency_key": "external-revocation",
                },
            )
            assert denied.status_code == 403
            assert provider.messages == []
    finally:
        await external.close()
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_skill_command_respects_frozen_empty_run_skill_binding(tmp_path: Path) -> None:
    app, service, manager, original_thread, skill_id, _, provider = await _setup(tmp_path)
    try:
        role_id = thread_session(service, original_thread).role_snapshot.role_id
        thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
        service.create_session(role_id, thread_id=thread.id, config_overrides={"skill_ids": []})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = f"/v1/workbench/threads/{thread.id}/skill-commands"
            assert (await client.get(url)).json()["commands"] == []
            rejected = await client.post(
                url,
                json={
                    "command": f"skill:{skill_id}",
                    "arguments": {"prompt": "Cannot expand frozen role scope."},
                    "idempotency_key": "frozen-empty",
                },
            )
            assert rejected.status_code == 403
            assert provider.messages == []
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_new_project_without_installed_skills_lists_empty_commands(tmp_path: Path) -> None:
    app = create_app(tmp_path / "empty.sqlite3", phase45_policy_engine=_allow())
    service = app.state.operant_service
    manager = service.memory_manager_factory()
    try:
        await manager.execute(
            ManagementCommand(
                action="project_create", name="empty-project", workspace_path=str(tmp_path)
            )
        )
        profile = service.add_model_profile(
            ModelProfile(
                name="empty-skill-profile",
                model_id="skill-test-model",
                base_url="https://example.invalid/v1",
                secret_ref="OPERANT_SKILL_TEST_KEY",
            )
        )
        role = service.create_role(
            RolePreset(
                name="empty-skill-role",
                system_prompt="Answer concisely.",
                model_profile_id=profile.id,
                tool_policy=ToolPolicy(allowed_tools=()),
            )
        )
        thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
        service.create_session(role.id, thread_id=thread.id)
        assert not (manager.root / "skills").exists()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/v1/workbench/threads/{thread.id}/skill-commands")
        assert response.status_code == 200, response.text
        assert response.json() == {"commands": []}
        assert not (manager.root / "skills").exists()
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_missing_installed_skill_root_is_not_discoverable(tmp_path: Path) -> None:
    app, service, manager, thread_id, skill_id, _, provider = await _setup(tmp_path)
    try:
        shutil.rmtree(manager.root / "skills")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = f"/v1/workbench/threads/{thread_id}/skill-commands"
            response = await client.get(url)
            assert response.status_code == 200, response.text
            assert response.json() == {"commands": []}
            denied = await client.post(
                url,
                json={
                    "command": f"skill:{skill_id}",
                    "arguments": {"prompt": "Do not run a missing Skill."},
                    "idempotency_key": "missing-root",
                },
            )
            assert denied.status_code == 403
            assert provider.messages == []
    finally:
        await manager.close()
        service.close()
