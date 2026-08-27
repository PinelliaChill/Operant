from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from operant.application.service import ApplicationService, _PersistentActionGateway
from operant.domain.actions import ApprovalRequest, ApprovalStatus, ToolActionReceipt
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolCall, ToolDefinition
from operant.domain.models import (
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    ToolPolicy,
)
from operant.persistence.sqlite import SQLiteStore
from operant.protocol import (
    MAX_PUBLIC_SERIALIZED_BYTES,
    canonical_action_hash,
    redact_public_data,
    redact_public_text,
)
from operant.runtime.loop import AgentLoop, ToolActionClaim
from operant.tools.execution import CommandResult
from operant.tools.workspace import ToolError, WorkspaceTools

SECRET = "unit-test-secret-value"


class SecretRunner:
    async def run(self, **_kwargs: Any) -> CommandResult:
        return CommandResult(
            argv=("python", "safe-test.py"),
            exit_code=1,
            stdout=f'API_KEY={SECRET}\n{{"api_key":"{SECRET}"}}',
            stderr=(
                "Bearer abc123\n"
                "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
                f"{SECRET}\n"
                "-----END ENCRYPTED PRIVATE KEY-----"
            ),
            stdout_truncated=False,
            stderr_truncated=False,
        )


class FailingWorkspaceTools(WorkspaceTools):
    def __init__(self, *args: Any, failure_type: type[Exception], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failure_type = failure_type

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        approved_categories: frozenset[str] = frozenset(),
    ) -> str:
        del name, arguments, approved_categories
        if self.failure_type is ToolError:
            raise ToolError(
                f"DATABASE_PASSWORD={SECRET}; Bearer abc123; "
                f"-----BEGIN DSA PRIVATE KEY-----\n{SECRET}"
            )
        if self.failure_type is ValueError:
            raise ValueError(f"OPENAI_API_KEY={SECRET}")
        raise json.JSONDecodeError(f"client_secret={SECRET}", "", 0)


class ToolThenStopProvider:
    def __init__(self) -> None:
        self.turn = 0
        self.tool_result = ""

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, tools
        self.turn += 1
        if self.turn == 1:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="redaction-call",
                            name="run_command",
                            arguments_json='{"argv":["python","safe-test.py"]}',
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
            return
        self.tool_result = messages[-1].content or ""
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="done", finish_reason="stop"),
        )


class WaitingProvider:
    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        await asyncio.sleep(3600)
        if False:
            yield ProviderEvent(event_type="unreachable")


class DuplicatePatchProvider:
    def __init__(self) -> None:
        self.turn = 0

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.turn += 1
        if self.turn <= 2:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="same-patch-call",
                            name="apply_patch",
                            arguments_json=(
                                '{"path":"target.py","old_text":"before\\n","new_text":"after\\n"}'
                            ),
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
            return
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="done", finish_reason="stop"),
        )


class GitAddProvider:
    def __init__(self) -> None:
        self.turn = 0

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.turn += 1
        if self.turn == 1:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="race-git-add",
                            name="run_command",
                            arguments_json='{"argv":["git","add","change.txt"]}',
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
            return
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="approved", finish_reason="stop"),
        )


class RecordingGateway:
    def __init__(self) -> None:
        self.receipt_result = ""

    def reserve_tool_action(self, **_kwargs: Any) -> ToolActionClaim:
        return ToolActionClaim(receipt_id="receipt", action_hash="a" * 64)

    def complete_tool_action(self, _claim: ToolActionClaim, result: str) -> None:
        self.receipt_result = result

    def fail_tool_action(
        self,
        _claim: ToolActionClaim,
        *,
        error_code: str,
        result: str,
    ) -> None:
        del error_code
        self.receipt_result = result

    def request_approval(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def verify_approval(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _snapshot(policy: ToolPolicy) -> RoleSnapshot:
    profile = ModelProfile(
        name="protocol-model",
        model_id="protocol-model",
        base_url="https://example.invalid/v1",
        secret_ref="OPERANT_TEST_KEY",
    )
    return RoleSnapshot(
        role_id="role_protocol",
        role_version=1,
        role_name="Protocol",
        system_prompt="Test bounded public output.",
        model_profile_id=profile.id,
        model_profile_name=profile.name,
        provider=profile.provider,
        model_id=profile.model_id,
        base_url=profile.base_url,
        secret_ref=profile.secret_ref,
        effort=profile.default_effort,
        provider_effort_parameter=profile.effort_parameter,
        provider_effort_value=profile.provider_effort_value(profile.default_effort),
        tool_policy=policy,
        budget=RolePreset(
            name="Protocol",
            system_prompt="Test bounded public output.",
            model_profile_id=profile.id,
        ).budget,
        memory_scope="session",
    )


def _service_with_role(
    tmp_path: Path,
    provider: Any,
    *,
    policy: ToolPolicy,
) -> tuple[ApplicationService, str]:
    store = SQLiteStore(tmp_path / "service.sqlite3")
    service = ApplicationService(store, provider)
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            id="model_service_protocol",
            name="service-protocol",
            model_id="service-protocol",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            id="role_service_protocol",
            name="Service Protocol",
            system_prompt="Exercise the persistent gateway.",
            model_profile_id=profile.id,
            tool_policy=policy,
        )
    )
    return service, role.id


def test_redactor_covers_assignments_json_partial_keys_and_bounds() -> None:
    samples = (
        (f"API_KEY={SECRET}", SECRET),
        (f"DATABASE_PASSWORD={SECRET}", SECRET),
        (f"OPENAI_API_KEY={SECRET}", SECRET),
        (f'{{"api_key":"{SECRET}"}}', SECRET),
        (f'{{"client_secret":"{SECRET}"}}', SECRET),
        ("Authorization: Basic dXNlcjpzZWNyZXQ=", "dXNlcjpzZWNyZXQ="),
        ("https://db-user:db-password@example.invalid/path", "db-user:db-password"),
        (f"-----BEGIN PRIVATE KEY-----\n{SECRET}", SECRET),
        (
            f"-----BEGIN ENCRYPTED PRIVATE KEY-----\n{SECRET}\n-----END ENCRYPTED PRIVATE KEY-----",
            SECRET,
        ),
        (
            f"-----BEGIN DSA PRIVATE KEY-----\n{SECRET}\n-----END RSA PRIVATE KEY-----",
            SECRET,
        ),
        (f"-----BEGIN FOO-BAR PRIVATE KEY-----\n{SECRET}", SECRET),
        ("Bearer abc123", "abc123"),
        ("Bearer z", "z"),
        ("Bearer x$?", "x$?"),
        ("sk-abcdefghijklmnop1234", "sk-abcdefghijklmnop1234"),
    )
    for sample, secret_value in samples:
        assert secret_value not in redact_public_text(sample)
    assert redact_public_text("x" * 20, max_chars=5) == "xxxxx...[truncated]"
    assert redact_public_data({"password": SECRET, "secret_ref": "OPERANT_KEY"}) == {
        "password": "[REDACTED]",
        "secret_ref": "OPERANT_KEY",
    }
    assert redact_public_data(
        {"databasePassword": SECRET, "nested": {"service_client_secret": SECRET}}
    ) == {
        "databasePassword": "[REDACTED]",
        "nested": {"service_client_secret": "[REDACTED]"},
    }
    assert redact_public_data([[[[SECRET]]]], max_depth=1) == [["[truncated: maximum depth]"]]
    assert redact_public_data(list(range(10)), max_items=2) == [0, 1, "[truncated items]"]
    assert redact_public_data({"value": "x" * 100}, max_bytes=20) == {
        "_truncated": True,
    }


@pytest.mark.parametrize(
    ("payload", "max_bytes", "expected_type"),
    [
        ("\x00" * 20_000, MAX_PUBLIC_SERIALIZED_BYTES, str),
        ("😀" * 200, 100, str),
        (
            {
                "nested": [
                    {"value": "\x00" * 500, "password": SECRET},
                    {"secret_ref": "OPERANT_KEY"},
                ]
            },
            100,
            dict,
        ),
        ([["😀" * 100] for _ in range(10)], 100, list),
    ],
)
def test_redactor_enforces_hard_final_json_byte_limit(
    payload: Any, max_bytes: int, expected_type: type[Any]
) -> None:
    safe = redact_public_data(payload, max_bytes=max_bytes)
    serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert len(serialized) <= max_bytes
    assert isinstance(safe, expected_type)
    assert SECRET not in serialized.decode("utf-8")
    json.loads(serialized)

    assert (
        len(json.dumps(redact_public_data(payload, max_bytes=1), separators=(",", ":")).encode())
        <= 1
    )


def test_redactor_keeps_normal_strings_and_secret_refs_within_byte_limit() -> None:
    payload = {"message": "正常文本😀", "secret_ref": "OPERANT_KEY"}
    assert redact_public_data(payload) == payload


def test_action_hashes_are_canonical_and_bind_exact_side_effect(tmp_path: Path) -> None:
    assert canonical_action_hash({"a": 1, "b": 2}) == canonical_action_hash({"b": 2, "a": 1})
    assert canonical_action_hash({"a": 1}) != canonical_action_hash({"a": 2})

    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(
            allowed_tools=("apply_patch", "run_command"),
            workspace_write=True,
            command_execution=True,
        ),
    )
    patch = {"path": "target.py", "old_text": "before", "new_text": "after"}
    same_patch_path = {
        "path": "nested/../target.py",
        "old_text": "before",
        "new_text": "after",
    }
    assert tools.action_hash("apply_patch", patch) == tools.action_hash(
        "apply_patch", same_patch_path
    )
    assert tools.action_hash("apply_patch", patch) != tools.action_hash(
        "apply_patch", {**patch, "new_text": "different"}
    )
    command = {"argv": ["python", "test.py"], "cwd": ".", "timeout_seconds": 60}
    assert tools.action_hash("run_command", command) != tools.action_hash(
        "run_command", {**command, "argv": ["python", "other.py"]}
    )


@pytest.mark.asyncio
async def test_workspace_output_receipt_event_and_model_feedback_share_redaction(
    tmp_path: Path,
) -> None:
    policy = ToolPolicy(
        allowed_tools=("run_command",),
        command_execution=True,
        approval_required=(),
    )
    tools = WorkspaceTools(tmp_path, policy=policy, runner=SecretRunner(), output_limit=500)
    provider = ToolThenStopProvider()
    gateway = RecordingGateway()
    events = [
        event
        async for event in AgentLoop(
            provider,
            tools,
            action_gateway=gateway,
        ).run(snapshot=_snapshot(policy), user_message="run safe test")
    ]

    completed = next(event for event in events if event.event_type == "tool.completed")
    serialized_event = json.dumps(completed.model_dump(mode="json"))
    assert SECRET not in gateway.receipt_result
    assert SECRET not in serialized_event
    assert SECRET not in provider.tool_result
    assert "[REDACTED]" in gateway.receipt_result
    assert "[REDACTED PRIVATE KEY]" in provider.tool_result
    assert "abc123" not in gateway.receipt_result
    assert "abc123" not in serialized_event
    assert "abc123" not in provider.tool_result


@pytest.mark.parametrize("failure_type", [ToolError, ValueError, json.JSONDecodeError])
@pytest.mark.asyncio
async def test_tool_failures_share_redaction_before_receipt_model_and_runtime_event(
    tmp_path: Path,
    failure_type: type[Exception],
) -> None:
    policy = ToolPolicy(
        allowed_tools=("run_command",),
        command_execution=True,
        approval_required=(),
    )
    tools = FailingWorkspaceTools(tmp_path, policy=policy, failure_type=failure_type)
    provider = ToolThenStopProvider()
    gateway = RecordingGateway()
    events = [
        event
        async for event in AgentLoop(
            provider,
            tools,
            action_gateway=gateway,
        ).run(snapshot=_snapshot(policy), user_message="exercise safe failure")
    ]

    failed = next(event for event in events if event.event_type == "tool.failed")
    assert SECRET not in gateway.receipt_result
    assert SECRET not in provider.tool_result
    assert SECRET not in json.dumps(failed.model_dump(mode="json"))
    assert "abc123" not in gateway.receipt_result
    assert "abc123" not in provider.tool_result
    assert "abc123" not in json.dumps(failed.model_dump(mode="json"))
    assert "[REDACTED]" in gateway.receipt_result


@pytest.mark.asyncio
async def test_gateway_requires_bound_receipt_and_persisted_positive_decision(
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("before\n", encoding="utf-8")
    service, role_id = _service_with_role(
        tmp_path,
        WaitingProvider(),
        policy=ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True),
    )
    session = service.create_session(role_id)
    agent = service.store.create_agent(session.id)
    tools = WorkspaceTools(tmp_path, policy=session.role_snapshot.tool_policy)
    gateway = _PersistentActionGateway(
        store=service.store,
        session_id=session.id,
        agent_id=agent.id,
        tools=tools,
    )
    arguments = {"path": "target.py", "old_text": "before\n", "new_text": "after\n"}
    claim = gateway.reserve_tool_action(
        tool_call_id="approval-bound",
        name="apply_patch",
        arguments=arguments,
    )
    approval_payload = gateway.request_approval(
        claim,
        tool_call_id="approval-bound",
        category="workspace_write",
        detail="apply_patch requires approval",
    )
    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE approval_requests SET status = 'approved' WHERE id = ?",
            (approval_payload["approval_id"],),
        )
    with pytest.raises(ToolError, match="not valid"):
        gateway.verify_approval(
            claim,
            tool_call_id="approval-bound",
            name="apply_patch",
            arguments=arguments,
        )

    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE approval_requests SET status = 'pending' WHERE id = ?",
            (approval_payload["approval_id"],),
        )
    approved, decision, changed = service.store.decide_approval(
        session.id,
        "approval-bound",
        approved=True,
    )
    assert changed and decision.approved and approved.status is ApprovalStatus.APPROVED

    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE approval_decisions SET approved = 0 WHERE approval_id = ?",
            (approval_payload["approval_id"],),
        )
    with pytest.raises(ToolError, match="not valid"):
        gateway.verify_approval(
            claim,
            tool_call_id="approval-bound",
            name="apply_patch",
            arguments=arguments,
        )
    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE approval_decisions SET approved = 1 WHERE approval_id = ?",
            (approval_payload["approval_id"],),
        )
        connection.execute(
            "UPDATE tool_action_receipts SET action_hash = ? WHERE id = ?",
            ("f" * 64, claim.receipt_id),
        )
    with pytest.raises(ToolError, match="exact agent action"):
        gateway.verify_approval(
            claim,
            tool_call_id="approval-bound",
            name="apply_patch",
            arguments=arguments,
        )
    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE tool_action_receipts SET action_hash = ?, agent_id = ? WHERE id = ?",
            (claim.action_hash, "agent_context_mismatch", claim.receipt_id),
        )
    with pytest.raises(ToolError, match="exact agent action"):
        gateway.verify_approval(
            claim,
            tool_call_id="approval-bound",
            name="apply_patch",
            arguments=arguments,
        )
    with sqlite3.connect(service.store.path) as connection:
        connection.execute(
            "UPDATE tool_action_receipts SET agent_id = ? WHERE id = ?",
            (agent.id, claim.receipt_id),
        )
    gateway.verify_approval(
        claim,
        tool_call_id="approval-bound",
        name="apply_patch",
        arguments=arguments,
    )
    result = await tools.execute("apply_patch", arguments)
    gateway.complete_tool_action(claim, result)
    assert (tmp_path / "target.py").read_text(encoding="utf-8") == "after\n"
    assert service.store.get_tool_action_receipt(claim.receipt_id).status.value == "completed"


@pytest.mark.asyncio
async def test_application_service_replays_duplicate_tool_call_without_second_side_effect(
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("before\n", encoding="utf-8")
    service, role_id = _service_with_role(
        tmp_path,
        DuplicatePatchProvider(),
        policy=ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True),
    )
    session = service.create_session(role_id)
    events = [
        event
        async for event in service.run_session(
            session.id,
            user_message="apply exactly once",
            workspace=tmp_path,
        )
    ]
    assert (tmp_path / "target.py").read_text(encoding="utf-8") == "after\n"
    event_types = [event.event_type for event in events]
    assert event_types.count("tool.completed") == 2
    assert "tool.failed" not in event_types
    with sqlite3.connect(service.store.path) as connection:
        rows = connection.execute("SELECT status, result_json FROM tool_action_receipts").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "completed"
    assert '"created": false' in rows[0][1]


@pytest.mark.asyncio
async def test_service_session_single_flight_cancel_close_and_cross_session_concurrency(
    tmp_path: Path,
) -> None:
    service, role_id = _service_with_role(
        tmp_path,
        WaitingProvider(),
        policy=ToolPolicy(),
    )
    first_session = service.create_session(role_id)
    second_session = service.create_session(role_id)
    first = service.run_session(
        first_session.id,
        user_message="first",
        workspace=tmp_path,
    )
    first_started = await anext(first)
    assert first_started.event_type == "agent.started"

    duplicate = service.run_session(
        first_session.id,
        user_message="duplicate",
        workspace=tmp_path,
    )
    duplicate_event = await anext(duplicate)
    assert duplicate_event.event_type == "agent.stream_error"
    assert duplicate_event.payload["error"]["code"] == "session_run_conflict"
    await duplicate.aclose()
    with sqlite3.connect(service.store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agents WHERE session_id = ?",
                (first_session.id,),
            ).fetchone()[0]
            == 1
        )

    other = service.run_session(
        second_session.id,
        user_message="other session",
        workspace=tmp_path,
    )
    assert (await anext(other)).event_type == "agent.started"
    assert service.cancel_session(first_session.id) is True
    assert service.cancel_session(second_session.id) is True
    assert (await anext(first)).event_type == "agent.cancelled"
    assert (await anext(other)).event_type == "agent.cancelled"
    await first.aclose()
    await other.aclose()

    closed_early = service.run_session(
        first_session.id,
        user_message="close early",
        workspace=tmp_path,
    )
    assert (await anext(closed_early)).event_type == "agent.started"
    await closed_early.aclose()
    after_close = service.run_session(
        first_session.id,
        user_message="slot released",
        workspace=tmp_path,
    )
    assert (await anext(after_close)).event_type == "agent.started"
    assert service.cancel_session(first_session.id) is True
    assert (await anext(after_close)).event_type == "agent.cancelled"
    await after_close.aclose()


@pytest.mark.asyncio
async def test_restart_pending_durable_approval_blocks_new_service_run(tmp_path: Path) -> None:
    service, role_id = _service_with_role(
        tmp_path,
        WaitingProvider(),
        policy=ToolPolicy(),
    )
    session = service.create_session(role_id)
    agent = service.store.create_agent(session.id)
    receipt, _created = service.store.reserve_tool_action(
        ToolActionReceipt(
            scope=f"session:{session.id}:agent:{agent.id}:attempt:1",
            session_id=session.id,
            agent_id=agent.id,
            idempotency_key="durable-pending",
            action_hash="a" * 64,
            command_name="run_command",
        )
    )
    service.store.create_approval_request(
        ApprovalRequest(
            session_id=session.id,
            agent_id=agent.id,
            tool_action_receipt_id=receipt.id,
            tool_call_id="durable-pending",
            action_hash=receipt.action_hash,
            category="network",
            detail_summary="run_command category=network; executable=curl; argument_count=1",
        )
    )

    restarted = ApplicationService(SQLiteStore(service.store.path), WaitingProvider())
    restarted.initialize()
    with sqlite3.connect(restarted.store.path) as connection:
        agent_count = connection.execute(
            "SELECT COUNT(*) FROM agents WHERE session_id = ?",
            (session.id,),
        ).fetchone()[0]
    blocked = restarted.run_session(
        session.id,
        user_message="must remain blocked",
        workspace=tmp_path,
    )
    blocked_event = await anext(blocked)
    assert blocked_event.event_type == "agent.stream_error"
    assert blocked_event.payload["error"]["code"] == "session_pending_approval"
    await blocked.aclose()
    with sqlite3.connect(restarted.store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agents WHERE session_id = ?",
                (session.id,),
            ).fetchone()[0]
            == agent_count
        )

    restarted.decide_approval(session.id, "durable-pending", approved=False)
    admitted = restarted.run_session(
        session.id,
        user_message="approval reconciled",
        workspace=tmp_path,
    )
    assert (await anext(admitted)).event_type == "agent.started"
    assert restarted.cancel_session(session.id) is True
    assert (await anext(admitted)).event_type == "agent.cancelled"
    await admitted.aclose()


@pytest.mark.asyncio
async def test_approval_decision_between_request_commit_and_future_creation_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "change.txt").write_text("change\n", encoding="utf-8")
    service, role_id = _service_with_role(
        tmp_path,
        GitAddProvider(),
        policy=ToolPolicy(allowed_tools=("run_command",), command_execution=True),
    )
    session = service.create_session(role_id)
    original_create = service.store.create_approval_request

    def create_then_decide(approval: ApprovalRequest) -> ApprovalRequest:
        persisted = original_create(approval)
        service.decide_approval(
            approval.session_id,
            approval.tool_call_id,
            approved=True,
        )
        return persisted

    monkeypatch.setattr(service.store, "create_approval_request", create_then_decide)
    events = [
        event
        async for event in service.run_session(
            session.id,
            user_message="stage the file",
            workspace=tmp_path,
        )
    ]
    assert events[-1].event_type == "agent.completed"
    approval = service.store.list_approval_requests(session.id, status=None)[0]
    assert approval.status is ApprovalStatus.APPROVED
    assert service.store.get_approval_decision(approval.id) is not None
    assert [
        event.event_type for event in service.store.list_approval_audit_events(approval.id)
    ] == ["approval.requested", "approval.decided"]
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "change.txt"

    monkeypatch.setattr(service.store, "create_approval_request", original_create)
    pending_receipt, _ = service.store.reserve_tool_action(
        ToolActionReceipt(
            scope="restart-pending",
            session_id=session.id,
            agent_id=approval.agent_id,
            idempotency_key="restart-pending",
            action_hash="f" * 64,
            command_name="run_command",
        )
    )
    pending = service.store.create_approval_request(
        ApprovalRequest(
            session_id=session.id,
            agent_id=approval.agent_id,
            tool_action_receipt_id=pending_receipt.id,
            tool_call_id="restart-pending",
            action_hash=pending_receipt.action_hash,
            category="network",
            detail_summary="run_command category=network; executable=curl; argument_count=1",
        )
    )
    restarted = ApplicationService(SQLiteStore(service.store.path), GitAddProvider())
    restarted.initialize()
    listed = restarted.list_pending_approvals(session.id)
    assert listed[0]["approval_id"] == pending.id
    assert listed[0]["continuation_available"] is False


@pytest.mark.parametrize(
    "name",
    (
        ".env.production",
        ".ENV.PRODUCTION",
        ".npmrc",
        ".NPMRC",
        ".pypirc",
        ".netrc",
        "id_ecdsa",
        "ID_RSA",
        "credentials.json",
        "CREDENTIALS.JSON",
        "client.pem",
        "CLIENT.PEM",
    ),
)
def test_workspace_tools_protect_common_credential_files(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    target.write_text(SECRET, encoding="utf-8")
    tools = WorkspaceTools(tmp_path)
    with pytest.raises(ToolError, match="protected"):
        tools.read_file(name)


@pytest.mark.parametrize("runtime_directory", (".operant", ".OPERANT"))
def test_workspace_tools_protect_operant_runtime_data(
    tmp_path: Path, runtime_directory: str
) -> None:
    directory = tmp_path / runtime_directory
    directory.mkdir()
    state = directory / "state.txt"
    state.write_text("authoritative local state", encoding="utf-8")
    tools = WorkspaceTools(tmp_path)

    with pytest.raises(ToolError, match="protected"):
        tools.read_file(f"{runtime_directory}/state.txt")
    with pytest.raises(ToolError, match="protected"):
        tools.search_files("authoritative", runtime_directory)
    with pytest.raises(ToolError, match="protected"):
        tools.apply_patch(
            f"{runtime_directory}/state.txt",
            "authoritative local state",
            "mutated",
        )


def test_search_files_resolves_symlinks_and_only_follows_safe_internal_files(
    tmp_path: Path,
) -> None:
    safe_directory = tmp_path / "safe"
    safe_directory.mkdir()
    (safe_directory / "target.txt").write_text("needle in safe source\n", encoding="utf-8")
    (tmp_path / ".env").write_text("needle in environment\n", encoding="utf-8")
    runtime_directory = tmp_path / ".operant"
    runtime_directory.mkdir()
    (runtime_directory / "state.txt").write_text("needle in runtime\n", encoding="utf-8")
    external = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    external.write_text("needle outside workspace\n", encoding="utf-8")

    links = tmp_path / "links"
    links.mkdir()
    (links / "safe-alias.txt").symlink_to(Path("../safe/target.txt"))
    (links / "environment-alias.txt").symlink_to(Path("../.env"))
    (links / "runtime-alias.txt").symlink_to(Path("../.operant/state.txt"))
    (links / "external-alias.txt").symlink_to(external)
    (links / "broken-alias.txt").symlink_to(Path("../missing.txt"))

    result = WorkspaceTools(tmp_path).search_files("needle", "links")

    assert result == {
        "matches": [
            {
                "path": "links/safe-alias.txt",
                "line": 1,
                "text": "needle in safe source",
            }
        ],
        "truncated": False,
    }


def test_workspace_tools_do_not_overprotect_similarly_named_source(tmp_path: Path) -> None:
    source = tmp_path / "operant_runtime_adapter.py"
    source.write_text("VALUE = 'normal source'\n", encoding="utf-8")

    assert WorkspaceTools(tmp_path).read_file(source.name)["content"] == (
        "VALUE = 'normal source'\n"
    )
