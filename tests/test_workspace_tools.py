import json
from pathlib import Path

import pytest

from operant.application.factory import AgentFactory
from operant.application.service import _PersistentActionGateway
from operant.domain.messages import ToolCall, ToolDefinition
from operant.domain.models import ModelProfile, RolePreset, ToolPolicy
from operant.domain.security import Capability
from operant.persistence.sqlite import SQLiteStore
from operant.tools.extensions import ToolExtension
from operant.tools.workspace import ToolError, WorkspaceTools


@pytest.mark.asyncio
async def test_read_only_role_cannot_see_or_execute_patch(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(allowed_tools=("read_file", "search_files", "git_diff")),
    )

    assert {tool.name for tool in tools.definitions()} == {
        "read_file",
        "search_files",
        "git_diff",
    }
    with pytest.raises(ToolError, match="not allowed"):
        await tools.execute(
            "apply_patch",
            {"path": "module.py", "old_text": "1", "new_text": "2"},
        )
    assert target.read_text(encoding="utf-8") == "answer = 1\n"


@pytest.mark.asyncio
async def test_coder_can_modify_real_workspace_file(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(
            allowed_tools=("apply_patch",),
            workspace_write=True,
        ),
    )

    await tools.execute(
        "apply_patch",
        {"path": "module.py", "old_text": "answer = 1", "new_text": "answer = 42"},
    )

    assert target.read_text(encoding="utf-8") == "answer = 42\n"


def test_patch_with_identical_text_is_rejected_as_no_progress(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    tools = WorkspaceTools(tmp_path)

    with pytest.raises(ToolError, match="would not change"):
        tools.apply_patch("module.py", "answer = 1", "answer = 1")


@pytest.mark.asyncio
async def test_extension_requires_both_host_registration_and_role_grant(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    async def run(arguments: dict[str, object]) -> dict[str, object]:
        calls.append(arguments)
        return {"job_id": "job-test", "status": "succeeded"}

    extension = ToolExtension(
        plugin_id="operant.chrome.browser",
        plugin_version="phase56.v1",
        host_api_version="operant-tool-extension.v1",
        definition=ToolDefinition(
            name="ext_browser_click",
            description="Click an observed browser element.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        capabilities=(Capability.BROWSER_SUBMIT,),
        side_effecting=True,
        execute=run,
        approval_category="browser",
    )
    denied = WorkspaceTools(tmp_path, extensions={extension.definition.name: extension})
    assert denied.definitions() == ()
    with pytest.raises(ToolError, match="not allowed"):
        await denied.execute(extension.definition.name, {})
    missing = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(allowed_tools=(extension.definition.name,)),
    )
    assert missing.definitions() == ()
    with pytest.raises(ToolError, match="unknown tool"):
        await missing.execute(extension.definition.name, {})
    unregistered_call = missing.public_tool_call(
        ToolCall(
            id="unregistered-call",
            name=extension.definition.name,
            arguments_json='{"value":"private sentence"}',
        )
    )
    assert "private sentence" not in json.dumps(unregistered_call)
    assert "private sentence" not in json.dumps(
        missing.audit_arguments(extension.definition.name, {"value": "private sentence"})
    )
    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(
            allowed_tools=(extension.definition.name,),
            approval_required=("browser",),
        ),
        extensions={extension.definition.name: extension},
    )
    assert [item.name for item in tools.definitions()] == [extension.definition.name]
    assert tools.is_side_effecting(extension.definition.name)
    assert tools.extension_capabilities(extension.definition.name) == (Capability.BROWSER_SUBMIT,)
    assert tools.required_approval_category(extension.definition.name, {}) == "browser"
    audit = tools.audit_arguments(extension.definition.name, {"value": "private"})
    assert audit["plugin_id"] == "operant.chrome.browser"
    assert audit["plugin_version"] == "phase56.v1"
    assert len(audit["input_sha256"]) == 64
    assert "private" not in json.dumps(audit)
    public_call = tools.public_tool_call(
        ToolCall(
            id="call-test",
            name=extension.definition.name,
            arguments_json='{"value":"private sentence"}',
        )
    )
    assert "private sentence" not in json.dumps(public_call)
    assert len(json.loads(public_call["arguments_json"])["input_sha256"]) == 64
    assert "job-test" in await tools.execute(extension.definition.name, {})
    assert calls == [{}]
    digest = tools.action_hash(extension.definition.name, {"selector": "#go"})
    upgraded = WorkspaceTools(
        tmp_path,
        policy=tools.policy,
        extensions={
            extension.definition.name: ToolExtension(
                **{**extension.__dict__, "plugin_version": "phase56.v2"}
            )
        },
    )
    assert upgraded.action_hash(extension.definition.name, {"selector": "#go"}) != digest


def test_extension_gateway_uses_browser_capability_and_hides_input(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "gateway.sqlite3")
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            name="Extension gateway",
            model_id="test-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="Browser writer",
            system_prompt="Use the approved browser.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(
                allowed_tools=("ext_browser_fill",), approval_required=("browser",)
            ),
        )
    )
    session = AgentFactory(store).create_session(role.id)
    agent = store.create_agent(session.id)

    async def unused(_arguments: dict[str, object]) -> dict[str, object]:
        raise AssertionError("approval must happen before extension execution")

    extension = ToolExtension(
        plugin_id="operant.chrome.browser",
        plugin_version="phase56.v1",
        host_api_version="operant-tool-extension.v1",
        definition=ToolDefinition(
            name="ext_browser_fill",
            description="Fill a field.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        capabilities=(Capability.BROWSER_SUBMIT,),
        side_effecting=True,
        execute=unused,
        approval_category="browser",
    )
    tools = WorkspaceTools(
        tmp_path,
        policy=role.tool_policy,
        extensions={extension.definition.name: extension},
    )
    gateway = _PersistentActionGateway(
        store=store, session_id=session.id, agent_id=agent.id, tools=tools
    )
    arguments = {"selector": "#query", "value": "private sentence"}
    claim = gateway.reserve_tool_action(
        tool_call_id="browser-fill-test", name="ext_browser_fill", arguments=arguments
    )
    action, evaluation = gateway._security_claims[claim.receipt_id]
    assert action.requested_capabilities == (Capability.BROWSER_SUBMIT,)
    assert evaluation.decision.value == "ask"
    assert "private sentence" not in json.dumps(action.model_dump(mode="json"))
    category, summary = gateway.approval_requirement(claim) or ("", "")
    assert category == "browser"
    assert "operant.chrome.browser" in summary
    assert "private sentence" not in summary
