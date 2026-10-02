"""Isolated formal Provider + Memory formation, recall, correction acceptance.

Run from the beta task-2 checkout with OPERANT_BASE_URL, OPERANT_API_KEY and
OPERANT_BETA2_MODEL_ID set after `operant model discover` confirms the ID.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from operant.api import create_app
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.models import Budget, ModelProfile, RolePreset
from operant.memory_plugins.manager import MemoryManager


async def main(evidence_dir: Path) -> None:
    base_url = os.environ["OPERANT_BASE_URL"]
    model_id = os.environ["OPERANT_BETA2_MODEL_ID"]
    secret_ref = "OPERANT_API_KEY"
    if not os.environ.get(secret_ref):
        raise RuntimeError(f"{secret_ref} is required")
    evidence_dir = evidence_dir.expanduser().resolve()
    if not evidence_dir.is_absolute():
        raise ValueError("evidence directory must be absolute")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    workspace = evidence_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    database = evidence_dir / "core.sqlite3"
    if database.exists():
        raise FileExistsError(f"refusing to reuse evidence database: {database}")
    result_path = evidence_dir / "result.json"
    result: dict[str, object] = {"status": "failed", "model_id": model_id}
    try:
        app = create_app(database, artifact_root=evidence_dir / "artifacts")
        service = app.state.operant_service
        manager = MemoryManager(service)
        service.memory_manager = manager

        async def command(**arguments):
            return await manager.execute(ManagementCommand(**arguments))

        async def answer(role_id: str) -> tuple[str, str, str]:
            session = service.create_session(role_id)
            events = [
                event
                async for event in service.run_session(
                    session.id,
                    user_message="报告应使用什么语言？只回答语言名称。",
                    workspace=workspace,
                )
            ]
            if not events or events[-1].event_type != "agent.completed":
                raise AssertionError([event.event_type for event in events])
            revision = service.list_context_revisions(session.id)[0]
            return (
                str(events[-1].payload.get("content", "")),
                "\n".join(message.content or "" for message in revision.messages),
                revision.id,
            )

        try:
            discovered = await service.discover_models(base_url=base_url, secret_ref=secret_ref)
            if model_id not in discovered:
                raise RuntimeError("requested model ID was not returned by Provider Discovery")
            project = (
                await command(
                    action="project_create", name="memory-acceptance", workspace_path=str(workspace)
                )
            ).state.projects[-1]
            installation = (
                await command(
                    action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
                )
            ).state.installations[-1]
            await command(
                action="binding_select",
                project_id=project.project_id,
                installation_id=installation.installation_id,
            )
            profile = service.add_model_profile(
                ModelProfile(
                    name="memory-acceptance",
                    base_url=base_url,
                    model_id=model_id,
                    secret_ref=secret_ref,
                    context_window=8192,
                )
            )
            role = service.create_role(
                RolePreset(
                    name="memory-acceptance",
                    system_prompt=(
                        "Answer the user's question using relevant confirmed project memory."
                    ),
                    model_profile_id=profile.id,
                    memory_scope="project",
                    budget=Budget(max_turns=1, max_output_tokens=128),
                )
            )
            saved = await command(
                action="memory_save",
                project_id=project.project_id,
                content="报告使用英文。",
                confirmed=True,
            )
            original = saved.state.records[0]
            old_answer, old_input, old_revision = await answer(role.id)
            if "报告使用英文。" not in old_input:
                raise AssertionError("old confirmed memory was absent from Provider input")
            if not any(word in old_answer.casefold() for word in ("英语", "英文", "english")):
                raise AssertionError("old Provider answer did not follow the published preference")
            proposed = await command(
                action="memory_propose",
                project_id=project.project_id,
                record_id=original.record_id,
                expected_revision=original.revision,
                content="报告使用中文。",
            )
            record = next(
                item for item in proposed.state.records if item.record_id == original.record_id
            )
            if record.content != "报告使用英文。" or not record.proposals:
                raise AssertionError("unconfirmed correction changed published memory")
            await command(
                action="memory_confirm",
                project_id=project.project_id,
                proposal_id=record.proposals[-1].proposal_id,
                expected_revision=original.revision,
            )
            new_answer, new_input, new_revision = await answer(role.id)
            if "报告使用中文。" not in new_input or "报告使用英文。" in new_input:
                raise AssertionError("corrected memory was not the sole recalled version")
            if not any(word in new_answer.casefold() for word in ("中文", "汉语", "chinese")):
                raise AssertionError("new Provider answer did not follow the correction")
            result.update(
                status="passed",
                old_answer=old_answer,
                new_answer=new_answer,
                old_context_has_english=True,
                new_context_has_chinese_only=True,
                record_id=original.record_id,
                old_revision=old_revision,
                new_revision=new_revision,
                database=str(database),
            )
        finally:
            await manager.close()
            service.close()
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.evidence_dir))
