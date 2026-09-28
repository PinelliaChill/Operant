"""Opt-in real-model acceptance for an isolated third-party Agent Tool."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from operant.application.service import ApplicationService
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.external_tool import ExternalToolRegistry
from operant.providers.openai_compatible import OpenAICompatibleProvider
from operant.remote.tool_extensions import local_capability_tool_extensions
from operant.settings import load_local_env


async def main() -> None:
    if os.environ.get("OPERANT_PHASE4_REAL_MODEL") != "1":
        raise RuntimeError("set OPERANT_PHASE4_REAL_MODEL=1 for real-model acceptance")
    load_local_env(os.environ["OPERANT_ACCEPTANCE_ENV_FILE"])
    model_id = os.environ["OPERANT_ACCEPTANCE_MODEL_ID"]
    with tempfile.TemporaryDirectory(prefix="operant-phase4-external-agent-") as temporary:
        root = Path(temporary).resolve()
        source = root / "source"
        source.mkdir()
        (source / "manifest.json").write_text(
            json.dumps(
                {
                    "plugin_id": "word_count",
                    "version": "1.0.0",
                    "host_api_version": "operant-tool-extension.v1",
                    "tools": [
                        {
                            "name": "ext_word_count_count",
                            "description": "Count characters in the given short text.",
                            "parameters": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                        }
                    ],
                }
            )
        )
        (source / "plugin.py").write_text(
            "import json,sys\n"
            "request=json.loads(sys.stdin.buffer.read())\n"
            "print(json.dumps({'result':{'characters':len(request['arguments']['text'])}}))\n"
        )
        registry = ExternalToolRegistry(root / "external-tools")
        manifest, digest = registry.inspect(source)
        installed = registry.install(source, expected_digest=digest)
        assert manifest == installed.manifest
        registry.set_enabled(installed.manifest.plugin_id, True)
        granted_name = installed.granted_name("ext_word_count_count")

        store = SQLiteStore(root / "core.sqlite3")
        service = ApplicationService(
            store,
            OpenAICompatibleProvider(),
            tool_extension_factory=local_capability_tool_extensions,
        )
        service.initialize()
        try:
            profile = service.add_model_profile(
                ModelProfile(
                    name="Phase4 external Tool acceptance",
                    model_id=model_id,
                    base_url=os.environ["OPERANT_BASE_URL"],
                    secret_ref="OPERANT_API_KEY",
                    supported_efforts=(Effort.LOW,),
                    default_effort=Effort.LOW,
                    effort_parameter=None,
                )
            )
            role = service.create_role(
                RolePreset(
                    name="External Tool acceptance",
                    system_prompt=(
                        f"Call {granted_name} exactly once with the text 'hello'. "
                        "Use its result to answer with the number of characters."
                    ),
                    model_profile_id=profile.id,
                    effort=Effort.LOW,
                    tool_policy=ToolPolicy(allowed_tools=(granted_name,)),
                    budget=Budget(max_turns=3, timeout_seconds=90),
                )
            )
            session = service.create_session(role.id, workspace_ref=str(root))
            events = [
                event
                async for event in service.run_session(
                    session.id,
                    user_message="Use the provided Tool to count characters in hello.",
                    workspace=root,
                    memory_enabled=False,
                )
            ]
            names = [event.event_type for event in events]
            model_events = [event for event in events if event.event_type == "model.completed"]
            redacted = all(
                "input_sha256" in call.get("arguments_json", "")
                and "hello" not in call.get("arguments_json", "")
                for event in model_events
                for call in event.payload.get("tool_calls", [])
                if call.get("name") == granted_name
            )
            result_marked_untrusted = any(
                "untrusted" in json.dumps(event.payload)
                for event in events
                if event.event_type == "tool.completed"
            )
            print("model_id", model_id)
            print("entry", "ApplicationService.run_session")
            print("tool_started", "tool.started" in names)
            print("tool_completed", "tool.completed" in names)
            print("agent_completed", "agent.completed" in names)
            print("tool_arguments_redacted", redacted)
            print("result_marked_untrusted", result_marked_untrusted)
            if not {"tool.started", "tool.completed", "agent.completed"}.issubset(names):
                raise RuntimeError(f"external Tool Agent flow failed: {names}")
            if not redacted or not result_marked_untrusted:
                raise RuntimeError("external Tool audit or trust marker is missing")
        finally:
            service.close()


asyncio.run(main())
