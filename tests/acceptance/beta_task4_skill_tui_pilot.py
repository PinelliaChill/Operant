"""Standalone headless Textual Pilot through generated HTTP client and Core.

The deterministic provider verifies Session consumption; this is not a real
model acceptance run. Use an absolute, fresh evidence directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any

import uvicorn
from operant_tui.conversation_screen import ConversationScreen
from textual.app import App
from textual.widgets import Select, Static

from operant.api import create_app
from operant.application.security import PolicyEngine
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolDefinition
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.domain.threads import ConversationThread
from operant.providers.base import ModelProvider
from sdk.python_client.phase56_generated import Phase56Client


class DeterministicSkillProvider(ModelProvider):
    def __init__(self) -> None:
        self.messages: list[tuple[Message, ...]] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["skill-pilot-model"]

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


async def _setup(root: Path) -> tuple[Any, Any, Any, str, str, DeterministicSkillProvider]:
    source = root / "source"
    package = source / "explicit-only"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: explicit-only\ndescription: selected by the user\n"
        "disable-model-invocation: true\n---\nSKILL_PILOT_SYNTHETIC_MARKER\n",
        encoding="utf-8",
    )
    policy = PolicyEngine(
        PolicyBundle(
            bundle_id="skill-pilot-synthetic",
            version="test.v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="test-read",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="isolated synthetic test",
                ),
            ),
        )
    )
    app = create_app(
        root / "core.sqlite3", phase45_skill_roots={"test": source}, phase45_policy_engine=policy
    )
    service = app.state.operant_service
    provider = DeterministicSkillProvider()
    service.provider = provider
    manager = service.memory_manager_factory()
    project = await manager.execute(
        ManagementCommand(action="project_create", name="skill-pilot", workspace_path=str(root))
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
            name="synthetic-pilot",
            model_id="skill-pilot-model",
            base_url="https://example.invalid/v1",
            secret_ref="UNUSED_SKILL_PILOT_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="synthetic-pilot-role",
            system_prompt="Use only the explicitly selected Skill.",
            model_profile_id=profile.id,
            memory_scope="read: [project]; write: []",
            budget=Budget(max_turns=2, timeout_seconds=10),
            tool_policy=ToolPolicy(allowed_tools=()),
        )
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(root.resolve())))
    service.create_session(role.id, thread_id=thread.id)
    return app, service, manager, thread.id, skill_id, provider


@contextmanager
def _core(app: Any) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("test Core did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


async def main(evidence_dir: Path) -> None:
    if not evidence_dir.is_absolute():
        raise ValueError("evidence directory must be absolute")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = {
        "status": "failed",
        "entry": "Textual Pilot / generated Phase56 Client / isolated HTTP Core",
        "provider": "deterministic",
    }
    app, service, manager, thread_id, skill_id, provider = await _setup(evidence_dir)
    try:
        with _core(app) as origin:
            client = Phase56Client(origin)
            thread_view = {
                "id": thread_id,
                "status": "active",
                "parent_thread_id": None,
                "workspace_ref": str(evidence_dir.resolve()),
                "legacy_refs": [
                    ref.model_dump(mode="json") for ref in service.get_thread(thread_id).legacy_refs
                ],
            }

            class Controller:
                def catalog(self) -> dict[str, Any]:
                    return {"projects": [], "roles": [], "threads": [thread_view]}

                def commands(self) -> dict[str, Any]:
                    return {"commands": []}

                def extension_commands(self) -> dict[str, Any]:
                    return {"commands": []}

                def skill_commands(self, tid: str) -> dict[str, Any]:
                    return client.list_skill_commands(tid)

                def skill_command(
                    self, tid: str, name: str, arguments: dict[str, Any], *, key: str
                ) -> dict[str, Any]:
                    return client.execute_skill_command(
                        tid,
                        {"command": name, "arguments": arguments, "idempotency_key": key},
                        idempotency_key=key,
                    )

                def history(self, _thread: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "items": [],
                        "session": {"role_snapshot": {"model_id": "skill-test-model"}},
                    }

                def collaboration(self, _tid: str) -> dict[str, Any]:
                    return {"children": [], "messages": []}

                def child_agents(self, _tid: str) -> list[dict[str, Any]]:
                    return []

            screen = ConversationScreen(SimpleNamespace(core_url=origin))
            screen.controller = Controller()  # type: ignore[assignment]
            async with App().run_test(size=(100, 45)) as pilot:
                await pilot.app.push_screen(screen)
                await pilot.pause()
                screen.switch_selection(thread_view)
                await screen.load_selected()
                entry = next(c for c in screen.command_registry if c.get("skill_name"))
                assert entry["canonical_name"] == f"/skill:{skill_id}"
                choice = screen.query_one("#conversation-command-choice", Select)
                choice.value = entry["canonical_name"]
                await pilot.pause()
                invalid = screen.execute_slash(f'/skill:{skill_id} {{"wrong":"value"}}')
                await invalid.wait()
                assert provider.messages == []
                valid = screen.execute_slash(f'/skill:{skill_id} {{"prompt":"Answer now."}}')
                await valid.wait()
                status = screen.query_one("#conversation-status", Static).render().plain
                assert "completed" in status and "Skill command completed" in status
                assert len(provider.messages) == 1
                evidence.update(
                    {
                        "status": "passed",
                        "skill_discovered": True,
                        "completion_option": entry["canonical_name"],
                        "invalid_arguments_rejected": True,
                        "formal_session_feedback": True,
                        "provider_requests": len(provider.messages),
                    }
                )
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
