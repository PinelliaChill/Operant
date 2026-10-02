"""Seed an isolated resource lifecycle fixture for real GUI/TUI acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from operant.api import create_app
from operant.api_workbench_context import WorkbenchReferenceRequest, create_reference
from operant.application.context import PersistentContextComposer
from operant.domain.messages import Message, MessageRole
from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload


def seed(evidence: Path) -> None:
    if not evidence.is_absolute():
        raise ValueError("evidence path must be absolute")
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    database = evidence / "core.sqlite3"
    if database.exists():
        raise FileExistsError("refusing to reuse a client acceptance database")
    workspace = evidence / "workspace"
    workspace.mkdir()
    app = create_app(database, artifact_root=evidence / "artifacts")
    service = app.state.operant_service
    try:
        profile = service.add_model_profile(
            ModelProfile(
                name="Beta 2 合成验收",
                model_id="gpt-6-luna",
                secret_ref="OPERANT_API_KEY",
                base_url="https://example.invalid/v1",
                context_window=32000,
            )
        )
        role = service.create_role(
            RolePreset(
                name="Beta 2 只读验收",
                model_profile_id=profile.id,
                system_prompt="只读合成工作区；禁止外部副作用。",
                tool_policy=ToolPolicy(allowed_tools=("read_file",)),
                budget=Budget(max_output_tokens=128, max_turns=2),
            )
        )
        initialization, _ = service.initialize_workspace(workspace)
        thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
        session = service.create_session(role.id, thread_id=thread.id)
        turn = service.create_turn(Turn(thread_id=thread.id))
        service.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text="Beta 任务 2 合成客户端验收；这段历史应始终保留。"),
            )
        )
        references = {}
        for filename in ("unused.txt", "pinned.txt", "protected.txt"):
            (workspace / filename).write_text(f"合成文件 {filename}，仅用于资源清理验收。\n")
            references[filename] = create_reference(
                service, thread.id, WorkbenchReferenceRequest(kind="file", target=filename)
            )
        governance = app.state.workbench_resources
        pinned_id = f"artifact:{references['pinned.txt'].reference.target_id}"
        governance.pin(thread.id, pinned_id, pinned=True)
        agent = service.store.create_agent(session.id)
        composer = PersistentContextComposer(
            store=service.store,
            session=session,
            agent_id=agent.id,
            workspace=workspace,
            thread_id=thread.id,
            references=(references["protected.txt"].reference,),
            memory_resolver=lambda identity: service.get_memory(identity),
            artifact_reader=service._read_artifact_for_context,
            artifact_writer=lambda content: service._write_tool_result_artifact(content=content),
        )
        composer.compose(
            snapshot=session.role_snapshot,
            messages=(
                Message(role=MessageRole.SYSTEM, content=role.system_prompt),
                Message(role=MessageRole.USER, content="保留上述引用来源。"),
            ),
            tools=(),
            request_ordinal=1,
        )
        manifest = {
            "database": str(database),
            "workspace": str(workspace),
            "workspace_id": initialization.id,
            "thread_id": thread.id,
            "session_id": session.id,
            "cleanup_id": f"artifact:{references['unused.txt'].reference.target_id}",
            "pinned_id": pinned_id,
            "protected_id": f"artifact:{references['protected.txt'].reference.target_id}",
        }
        (evidence / "seed.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(json.dumps(manifest, ensure_ascii=False))
    finally:
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    seed(parser.parse_args().evidence_dir)
