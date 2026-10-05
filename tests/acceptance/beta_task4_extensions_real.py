"""Real-model Task 4 extension acceptance; writes only bounded synthetic evidence.

Run with --evidence-dir pointing at a fresh absolute directory. The process
loads the existing local .env, but never writes or prints credential values.
The capability driver result is a proposal; local-control.act is the authority
for any later browser or computer side effect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread, ThreadLegacyRef
from operant.plugins.external_tool import ExternalToolRegistry
from operant.plugins.local_extensions import run_operation
from operant.settings import load_local_env


def definition(name: str, description: str, property_name: str) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {property_name: {"type": "string"}},
            "required": [property_name],
            "additionalProperties": False,
        },
    }


def package(source: Path) -> None:
    source.mkdir(mode=0o700)
    manifest = {
        "plugin_id": "taskfour_ext",
        "version": "1.0.0",
        "host_api_version": "operant-local-extension.v1",
        "tools": [definition("ext_taskfour_ext_count", "Count characters in short text.", "text")],
        "commands": [
            definition("ext_taskfour_ext_status", "Return synthetic command status.", "label")
        ],
        "events": [
            definition("ext_taskfour_ext_event", "Observe a committed event type.", "payload_json")
        ],
        "providers": [
            definition(
                "ext_taskfour_ext_provider", "Select the frozen model endpoint.", "payload_json"
            )
        ],
        "runtimes": [
            definition("ext_taskfour_ext_runtime", "Reduce request output tokens.", "payload_json")
        ],
        "capability_drivers": [
            definition(
                "ext_taskfour_ext_driver", "Propose a bounded observe operation.", "payload_json"
            )
        ],
    }
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "plugin.py").write_text(
        """import json, os, sys
request = json.loads(sys.stdin.buffer.read())
category = request['category']
arguments = request['arguments']
if category == 'provider':
    selected = json.loads(arguments['payload_json'])
    result = {'model_id': selected['model_id'], 'base_url': selected['base_url']}
elif category == 'runtime':
    selected = json.loads(arguments['payload_json'])
    ceiling = selected['max_output_tokens']
    result = {'max_output_tokens': min(96, ceiling) if ceiling else 96}
elif category == 'tool':
    result = {'characters': len(arguments['text'])}
elif category == 'command':
    result = {'label': arguments['label'], 'status': 'ok'}
elif category == 'event':
    observed = json.loads(arguments['payload_json'])
    result = {'event_type': observed['event_type']}
elif category == 'capability_driver':
    result = {'operation': 'observe', 'arguments': {}}
else:
    raise ValueError('unsupported category')
if 'OPERANT_API_KEY' in os.environ:
    raise RuntimeError('Core credential leaked to extension')
print(json.dumps({'result': result}))
""",
        encoding="utf-8",
    )


async def main(evidence_dir: Path) -> None:
    if not evidence_dir.is_absolute():
        raise ValueError("evidence directory must be absolute")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    (evidence_dir / "workspace").mkdir()
    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    base_url = os.environ["OPERANT_BASE_URL"]
    if not os.environ.get("OPERANT_API_KEY"):
        raise RuntimeError("OPERANT_API_KEY is required")
    model_id = "gpt-6-luna"
    evidence: dict[str, object] = {
        "status": "failed",
        "model_id": model_id,
        "entry": "ApplicationService.run_session; extension command HTTP route",
    }
    app = create_app(
        evidence_dir / "core.sqlite3",
        artifact_root=evidence_dir / "artifacts",
        phase56_local_authorizer=lambda _request: True,
    )
    service = app.state.operant_service
    try:
        discovered = await service.discover_models(base_url=base_url, secret_ref="OPERANT_API_KEY")
        if model_id not in discovered:
            raise RuntimeError("gpt-6-luna was not returned by formal Discovery")
        evidence["discovery_exact_match"] = True
        actual_provider = service.provider.delegate
        request_caps: list[int | None] = []

        class ObservedProvider:
            async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
                return await actual_provider.list_models(base_url=base_url, secret_ref=secret_ref)

            async def stream(self, *, snapshot: object, messages: object, tools: object):
                request_caps.append(snapshot.budget.max_output_tokens)
                async for event in actual_provider.stream(
                    snapshot=snapshot, messages=messages, tools=tools
                ):
                    yield event

        service.provider.delegate = ObservedProvider()
        source = evidence_dir / "source"
        package(source)
        registry: ExternalToolRegistry = service.extension_registry
        manifest, digest = registry.inspect(source)
        record = registry.install(source, expected_digest=digest)
        if manifest != record.manifest:
            raise AssertionError("installed manifest mismatch")
        registry.set_enabled(
            record.manifest.plugin_id,
            True,
            granted_categories=(
                "tool",
                "command",
                "event",
                "provider",
                "runtime",
                "capability_driver",
            ),
        )
        tool_name = record.granted_name("ext_taskfour_ext_count")
        provider_name = record.granted_name("ext_taskfour_ext_provider")
        profile = service.add_model_profile(
            ModelProfile(
                name="beta-task4-six-category-real",
                provider=provider_name,
                model_id=model_id,
                base_url=base_url,
                secret_ref="OPERANT_API_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
            )
        )
        role = service.create_role(
            RolePreset(
                name="beta-task4-six-category-real",
                system_prompt=(
                    f"Call {tool_name} exactly once with text 'hello'. "
                    "Use its result to answer with the number of characters."
                ),
                model_profile_id=profile.id,
                effort=Effort.LOW,
                tool_policy=ToolPolicy(allowed_tools=(tool_name,)),
                budget=Budget(max_turns=3, timeout_seconds=120, max_output_tokens=256),
                memory_scope="none",
            )
        )
        workspace = (evidence_dir / "workspace").resolve()
        session = service.create_session(role.id, workspace_ref=str(workspace))
        thread = service.create_thread(
            ConversationThread(
                workspace_ref=str(workspace),
                legacy_refs=(ThreadLegacyRef(source_type="session", source_id=session.id),),
            )
        )
        events = [
            event
            async for event in service.run_session(
                session.id,
                user_message="Use the provided Tool to count characters in hello.",
                workspace=workspace,
                memory_enabled=False,
            )
        ]
        types = [event.event_type for event in events]
        evidence["model_flow"] = {
            "agent_completed": "agent.completed" in types,
            "tool_started": "tool.started" in types,
            "tool_completed": "tool.completed" in types,
            "extension_event_dispatched": "extension.runtime_dispatched" in types,
            "runtime_request_caps": request_caps,
            "session_id": session.id,
        }
        if not request_caps or any(cap is None or cap > 96 for cap in request_caps):
            raise AssertionError("runtime extension did not narrow real Provider requests")
        if not {"agent.completed", "tool.started", "tool.completed"}.issubset(types):
            raise AssertionError("real Agent Tool flow did not complete")
        if "extension.runtime_dispatched" not in types:
            raise AssertionError("event extension did not receive a committed event")
        driver_name = record.granted_name("ext_taskfour_ext_driver")
        proposal = run_operation(registry, driver_name, "capability_driver", {"payload_json": "{}"})
        if proposal != {"operation": "observe", "arguments": {}}:
            raise AssertionError("driver proposal shape is invalid")
        evidence["capability_driver_proposal_only"] = True
        command_name = record.granted_name("ext_taskfour_ext_status")
        body = {
            "command": command_name,
            "arguments": {"label": "synthetic"},
            "idempotency_key": "beta-task4-command-1",
        }
        with TestClient(app) as client:
            response = client.post(
                f"/v1/workbench/threads/{thread.id}/extension-commands",
                json=body,
                headers={"Idempotency-Key": body["idempotency_key"]},
            )
            replay = client.post(
                f"/v1/workbench/threads/{thread.id}/extension-commands",
                json=body,
                headers={"Idempotency-Key": body["idempotency_key"]},
            )
        evidence["command_status"] = response.status_code
        evidence["command_replay_same"] = response.json() == replay.json()
        if response.status_code != 200 or not evidence["command_replay_same"]:
            raise AssertionError("explicit extension command did not complete and replay")
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        raise
    finally:
        (evidence_dir / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    asyncio.run(main(parser.parse_args().evidence_dir))
