from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_onboarding import install_onboarding_routes
from operant.api_skill_commands import install_skill_command_routes
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine, balanced_policy_bundle
from operant.application.service import ApplicationService
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolDefinition
from operant.domain.models import ModelProfile, RoleSnapshot
from operant.domain.security import (
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
)
from operant.domain.threads import ConversationThread
from operant.persistence.onboarding import UXRepository
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore
from operant.providers.base import ModelProvider


class _AnswerProvider(ModelProvider):
    def __init__(self) -> None:
        self.messages: list[tuple[Message, ...]] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["test-model"]

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
            response=ModelResponse(content="已完成。", finish_reason="stop"),
        )


def test_new_conversation_and_rename_are_persistent_and_idempotent(tmp_path: Path) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), object())  # type: ignore[arg-type]
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="ready",
            model_id="test-model",
            base_url="https://example.test/v1",
            secret_ref="TEST_MODEL_KEY",
            effort_parameter=None,
        )
    )
    app = FastAPI()
    app.state.phase45_action_gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(service.store),
        SQLitePhase45Repository(service.store),
        PolicyEngine(balanced_policy_bundle()),
    )
    repo = UXRepository(service.store)
    refreshed_sources: list[str] = []
    app.state.refresh_skill_sources = refreshed_sources.append
    install_onboarding_routes(app, service, repo)
    install_skill_command_routes(
        app,
        service,
        action_gateway=app.state.phase45_action_gateway,
        local_authorizer=lambda _request: True,
    )
    client = TestClient(app)
    try:
        bootstrapped = client.post("/v1/setup/bootstrap", json={"model_profile_id": profile.id})
        assert bootstrapped.status_code == 200, bootstrapped.text
        assert bootstrapped.json()["ready"] is True
        default_workspace = service.store.get_workspace_initialization_by_id(
            bootstrapped.json()["default_workspace_id"]
        )
        assert refreshed_sources == [default_workspace.workspace_ref]

        key = "conversation-test-1"
        created = client.post("/v1/setup/conversations", json={}, headers={"Idempotency-Key": key})
        assert created.status_code == 200, created.text
        assert created.headers["Idempotency-Key"] == key
        assert created.json()["title"] == "新对话"
        replayed = client.post("/v1/setup/conversations", json={}, headers={"Idempotency-Key": key})
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == created.json()
        assert len(service.list_threads()) == 1
        resolved = client.get("/v1/setup/conversations/metadata", params={"request_id": key})
        assert [item["thread_id"] for item in resolved.json()["items"]] == [
            created.json()["thread_id"]
        ]
        assert client.get(
            "/v1/setup/conversations/metadata", params={"request_id": "not-admitted"}
        ).json() == {"items": []}
        repo.save_command("another-operation", "f" * 64, {"connection_id": "example"})
        assert client.get(
            "/v1/setup/conversations/metadata", params={"request_id": "another-operation"}
        ).json() == {"items": []}
        assert len(service.list_threads()) == 1
        session = service.get_session(created.json()["session_id"])
        assert len(session.role_snapshot.skill_ids) == 6
        provider = _AnswerProvider()
        service.provider = provider
        command_url = f"/v1/workbench/threads/{created.json()['thread_id']}/skill-commands"
        commands = client.get(command_url)
        assert commands.status_code == 200, commands.text
        assert len(commands.json()["commands"]) == 6
        grill = next(
            item
            for item in commands.json()["commands"]
            if item["description"].startswith("Clarify a plan")
        )
        invoked = client.post(
            command_url,
            json={
                "command": grill["command"],
                "arguments": {"prompt": "请帮我明确目标"},
                "idempotency_key": "first-bundled-skill",
            },
        )
        assert invoked.status_code == 200, invoked.text
        assert invoked.json()["status"] == "completed"
        assert len(provider.messages) == 1
        assert "When explicitly invoked" in "\n".join(
            message.content for message in provider.messages[0]
        )
        auto_title = repo.get_metadata(created.json()["thread_id"])
        assert auto_title.title_source == "auto"
        changed_payload = client.post(
            "/v1/setup/conversations",
            json={"title": "另一个名称"},
            headers={"Idempotency-Key": key},
        )
        assert changed_payload.status_code == 409
        assert len(service.list_threads()) == 1

        bad_role = client.post(
            "/v1/setup/conversations",
            json={"role_id": "missing-role"},
            headers={"Idempotency-Key": "bad-role"},
        )
        assert bad_role.status_code == 404
        assert len(service.list_threads()) == 1

        thread_id = created.json()["thread_id"]
        renamed = client.patch(
            f"/v1/setup/conversations/{thread_id}/metadata",
            json={"title": "需求讨论", "expected_revision": auto_title.revision},
            headers={"Idempotency-Key": "rename-test-1"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title_source"] == "manual"
        again = client.patch(
            f"/v1/setup/conversations/{thread_id}/metadata",
            json={"title": "需求讨论", "expected_revision": auto_title.revision},
            headers={"Idempotency-Key": "rename-test-1"},
        )
        assert again.status_code == 200, again.text
        assert again.json() == renamed.json()
        listed = client.get("/v1/setup/conversations/metadata")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["title"] == "需求讨论"

        other_path = tmp_path / "other-workspace"
        other_path.mkdir()
        other, _ = service.initialize_workspace(other_path)
        other_created = client.post(
            "/v1/setup/conversations",
            json={"workspace_id": other.id},
            headers={"Idempotency-Key": "other-workspace-conversation"},
        )
        assert other_created.status_code == 200, other_created.text
        assert refreshed_sources[-1] == str(other_path)
        other_snapshot = service.get_session(other_created.json()["session_id"]).role_snapshot
        assert other_snapshot.config_project_id == other.id
        assert len(other_snapshot.skill_ids) == 6
        other_command_url = (
            f"/v1/workbench/threads/{other_created.json()['thread_id']}/skill-commands"
        )
        other_commands = client.get(other_command_url)
        assert other_commands.status_code == 200, other_commands.text
        assert len(other_commands.json()["commands"]) == 6
        other_grill = next(
            item
            for item in other_commands.json()["commands"]
            if item["description"].startswith("Clarify a plan")
        )
        other_invocation = client.post(
            other_command_url,
            json={
                "command": other_grill["command"],
                "arguments": {"prompt": "请检查另一工作区"},
                "idempotency_key": "other-bundled-skill",
            },
        )
        assert other_invocation.status_code == 200, other_invocation.text
        assert len(provider.messages) == 2
        assert "When explicitly invoked" in "\n".join(
            message.content for message in provider.messages[1]
        )

        app.state.phase45_action_gateway.engine = PolicyEngine(
            PolicyBundle(
                bundle_id="deny-onboarding",
                version="deny-onboarding.v1",
                rules=(
                    PolicyRule(
                        rule_id="deny.setup.write",
                        layer=PolicyLayer.SYSTEM,
                        decision=PolicyDecision.DENY,
                        capabilities=(Capability.WORKSPACE_WRITE,),
                        tools=("onboarding",),
                        reason="setup writes are disabled",
                        hard=True,
                    ),
                ),
            )
        )
        denied = client.post(
            "/v1/setup/conversations", json={}, headers={"Idempotency-Key": "denied-new"}
        )
        assert denied.status_code == 403
        assert len(service.list_threads()) == 2
    finally:
        if service.memory_manager is not None:
            import asyncio

            asyncio.run(service.memory_manager.close())
        service.close()


def test_child_names_persist_and_direct_reads_are_independent_of_page_limit(tmp_path: Path) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "child-names.sqlite3"), object())  # type: ignore[arg-type]
    service.initialize()
    parent = service.create_thread(ConversationThread())
    child = service.create_thread(ConversationThread(parent_thread_id=parent.id))
    repo = UXRepository(service.store)
    named = repo.rename(child.id, "子任务名称")
    app = FastAPI()
    install_onboarding_routes(app, service, UXRepository(SQLiteStore(service.store.path)))
    try:
        with TestClient(app) as client:
            listed = client.get("/v1/setup/conversations/metadata").json()
            assert named.model_dump(mode="json") in listed["items"]
            bounded = client.get("/v1/setup/conversations/metadata", params={"limit": 1}).json()
            assert child.id not in [item["thread_id"] for item in bounded["items"]]
            direct = client.get(f"/v1/setup/conversations/{child.id}/metadata")
            assert direct.json() == named.model_dump(mode="json")
            assert (
                client.get(
                    f"/v1/setup/conversations/{child.id}/metadata",
                    headers={"Origin": "https://untrusted.example"},
                ).status_code
                == 403
            )
    finally:
        service.close()
