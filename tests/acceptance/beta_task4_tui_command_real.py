"""Mounted Textual + generated-client + local Core extension command acceptance.

Run with clients/tui/.venv/bin/python; no model credential or desktop focus is used.
The six-category package is actually enabled through the macOS sandbox probe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from textual.app import App
from textual.widgets import Select, Static

from operant.api import create_app
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread, ThreadLegacyRef
from sdk.python_client.phase56_generated import Phase56Client
from tests.acceptance.beta_task4_extensions_real import package
from tests.acceptance.beta_task4_local_control_real import running_core


async def main(evidence_dir: Path) -> None:
    if not evidence_dir.is_absolute():
        raise ValueError("evidence directory must be absolute")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    workspace = evidence_dir / "workspace"
    workspace.mkdir()
    app = create_app(
        evidence_dir / "core.sqlite3",
        artifact_root=evidence_dir / "artifacts",
        phase56_local_authorizer=lambda _request: True,
    )
    service = app.state.operant_service
    evidence: dict[str, object] = {"status": "failed", "entry": "Textual Pilot / HTTP Core"}
    try:
        source = evidence_dir / "source"
        package(source)
        registry = service.extension_registry
        _manifest, digest = registry.inspect(source)
        record = registry.install(source, expected_digest=digest)
        registry.set_enabled(record.manifest.plugin_id, True, granted_categories=("command",))
        name = record.granted_name("ext_taskfour_ext_status")
        profile = service.add_model_profile(
            ModelProfile(
                name="tui-command-no-model-call",
                model_id="unused-no-model-call",
                base_url="http://127.0.0.1:1/v1",
                secret_ref="UNUSED_TUI_COMMAND_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
            )
        )
        role = service.create_role(
            RolePreset(
                name="tui-command-no-model-call",
                system_prompt="No model call is made in this acceptance.",
                model_profile_id=profile.id,
                effort=Effort.LOW,
                tool_policy=ToolPolicy(allowed_tools=()),
                budget=Budget(max_turns=1, timeout_seconds=30),
            )
        )
        session = service.create_session(role.id, workspace_ref=str(workspace))
        thread = service.create_thread(
            ConversationThread(
                workspace_ref=str(workspace),
                legacy_refs=(ThreadLegacyRef(source_type="session", source_id=session.id),),
            )
        )
        thread_projection = {
            "id": thread.id,
            "status": "active",
            "parent_thread_id": None,
            "workspace_ref": str(workspace),
            "legacy_refs": [{"source_type": "session", "source_id": session.id}],
        }
        with running_core(app) as origin:
            client = Phase56Client(origin)

            class Controller:
                def catalog(self) -> dict[str, object]:
                    return {"projects": [], "roles": [], "threads": [thread_projection]}

                def commands(self) -> dict[str, object]:
                    return {"commands": []}

                def extension_commands(self) -> dict[str, object]:
                    return client.list_extension_commands()

                def extension_command(
                    self, tid: str, command: str, arguments: dict[str, object], *, key: str
                ) -> dict[str, object]:
                    return client.execute_extension_command(
                        tid,
                        {"command": command, "arguments": arguments, "idempotency_key": key},
                        idempotency_key=key,
                    )

            from operant_tui.conversation_screen import ConversationScreen

            screen = ConversationScreen(SimpleNamespace(core_url=origin))
            screen.controller = Controller()  # type: ignore[assignment]
            async with App().run_test(size=(100, 45)) as pilot:
                await pilot.app.push_screen(screen)
                await pilot.pause()
                entry = next(
                    command
                    for command in screen.command_registry
                    if command.get("extension_name") == name
                )
                assert entry["canonical_name"] == f"/{name}"
                choices = screen.query_one("#conversation-command-choice", Select)
                choices.value = entry["canonical_name"]
                await pilot.pause()
                screen.switch_selection(screen.threads[thread.id])
                invalid = screen.execute_slash(f'/{name} {{"wrong":"value"}}')
                await invalid.wait()
                invalid_status = screen.query_one("#conversation-status", Static).render().plain
                assert "http_400" in invalid_status and "schema" in invalid_status
                valid = screen.execute_slash(f'/{name} {{"label":"synthetic"}}')
                await valid.wait()
                status = screen.query_one("#conversation-status", Static).render().plain
                assert "completed" in status and "synthetic" in status
                evidence.update(
                    {
                        "status": "passed",
                        "discovered_command": name,
                        "completion_option": entry["canonical_name"],
                        "invalid_arguments_rejected": True,
                        "feedback_contains_completed_result": True,
                    }
                )
    finally:
        (evidence_dir / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    asyncio.run(main(parser.parse_args().evidence_dir))
