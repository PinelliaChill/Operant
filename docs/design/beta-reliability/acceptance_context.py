"""Isolated real-Provider H-13 long-history automatic and manual compaction.

Run from this checkout after model discovery with OPERANT_BASE_URL,
OPERANT_API_KEY, and OPERANT_BETA2_MODEL_ID set. --evidence-dir must be a fresh
absolute directory; its database and redacted, synthetic evidence are retained.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from operant.api import create_app
from operant.domain.commands import ContextBaselineOperation
from operant.domain.models import Budget, ModelProfile, RolePreset
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload

FACTS = {
    0: "目标：完成蓝杉内测报告。",
    2: "请把正文控制在两页以内。",
    25: "决定：采用模板 B。",
    27: "未完成项：核对合计数。",
    30: "来源：docs/facts.md。",
}
QUESTION = (
    "根据早期任务历史，分别写出目标、篇幅限制、已定格式、未完成项和来源。"
    "每项一行，沿用历史原词，不要猜测。"
)


def judge(answer: str) -> bool:
    return all(
        (
            "蓝杉" in answer,
            "两页" in answer or "2页" in answer,
            "模板 B" in answer or "模板B" in answer,
            "合计数" in answer,
            "docs/facts.md" in answer,
        )
    )


async def main(evidence_dir: Path) -> None:
    if not evidence_dir.is_absolute():
        raise ValueError("--evidence-dir must be absolute")
    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    database = evidence_dir / "core.sqlite3"
    if database.exists():
        raise FileExistsError(f"refusing to reuse evidence database: {database}")
    workspace = evidence_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    model_id = os.environ["OPERANT_BETA2_MODEL_ID"]
    base_url = os.environ["OPERANT_BASE_URL"]
    if not os.environ.get("OPERANT_API_KEY"):
        raise RuntimeError("OPERANT_API_KEY is required")
    result: dict[str, object] = {"status": "failed", "model_id": model_id}
    service = None
    try:
        app = create_app(database, artifact_root=evidence_dir / "artifacts")
        service = app.state.operant_service
        discovered = await service.discover_models(base_url=base_url, secret_ref="OPERANT_API_KEY")
        if model_id not in discovered:
            raise RuntimeError("model ID was not returned by Provider Discovery")
        profile = service.add_model_profile(
            ModelProfile(
                name="h13-acceptance",
                model_id=model_id,
                base_url=base_url,
                secret_ref="OPERANT_API_KEY",
                context_window=8192,
            )
        )
        role = service.create_role(
            RolePreset(
                name="h13-acceptance",
                system_prompt="Answer from the authorized conversation history and its sources.",
                model_profile_id=profile.id,
                memory_scope="none",
                budget=Budget(max_turns=1, max_output_tokens=256),
            )
        )

        async def scenario(mode: str) -> dict[str, object]:
            session = service.create_session(role.id)
            thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
            turn = service.create_turn(Turn(thread_id=thread.id))
            items = []
            for index in range(50):
                text = FACTS.get(index, "进度记录：仍在核对材料。" + "x" * 700)
                items.append(
                    service.append_item(
                        Item(
                            thread_id=thread.id,
                            turn_id=turn.id,
                            payload=UserMessagePayload(text=text),
                        )
                    )
                )
            baseline_id = None
            if mode == "manual":
                baseline = service.append_context_baseline(
                    session_id=session.id,
                    thread_id=thread.id,
                    operation=ContextBaselineOperation.COMPACT,
                    agent_id=service.factory.create_agent(session.id).id,
                )
                baseline_id = baseline.id
            events = [
                event
                async for event in service.run_session(
                    session.id,
                    user_message=QUESTION,
                    workspace=workspace,
                    thread_id=thread.id,
                )
            ]
            if not events or events[-1].event_type != "agent.completed":
                event_types = [event.event_type for event in events]
                failure_type = events[-1].payload.get("error_type") if events else None
                raise AssertionError(f"{mode} did not complete: {event_types}, {failure_type}")
            revision = service.list_context_revisions(session.id)[0]
            if revision.compaction_id is None:
                raise AssertionError(f"{mode} did not bind a Compaction")
            compaction = service.store.get_compaction(revision.compaction_id)
            summary = compaction.summary
            rendered = "\n".join(message.content or "" for message in revision.messages)
            for fact in FACTS.values():
                if fact not in rendered:
                    raise AssertionError(f"{mode} Provider input lost a historical fact: {fact}")
            if [item.id for item in service.list_items(thread.id)] != [item.id for item in items]:
                raise AssertionError(f"{mode} changed Canonical History")
            if not any("两页以内" in fact for fact in summary.workspace_state["user_requirements"]):
                raise AssertionError(f"{mode} summary lost the ordinary constraint")
            if not any("模板 B" in fact for fact in summary.decisions):
                raise AssertionError(f"{mode} summary lost the decision")
            if not any("合计数" in fact for fact in summary.open_tasks):
                raise AssertionError(f"{mode} summary lost the open task")
            if not any("docs/facts.md" in fact for fact in summary.workspace_state["source_notes"]):
                raise AssertionError(f"{mode} summary lost the source")
            answer = str(events[-1].payload.get("content", ""))
            (evidence_dir / f"{mode}_summary.json").write_text(
                summary.model_dump_json(indent=2), encoding="utf-8"
            )
            (evidence_dir / f"{mode}_revision.json").write_text(
                revision.model_dump_json(indent=2), encoding="utf-8"
            )
            if not judge(answer):
                raise AssertionError(f"{mode} model answer omitted a required fact")
            return {
                "session_id": session.id,
                "thread_id": thread.id,
                "baseline_id": baseline_id,
                "revision_id": revision.id,
                "compaction_id": compaction.id,
                "canonical_item_count": len(items),
                "answer": answer,
                "answer_follows_facts": True,
                "summary_file": f"{mode}_summary.json",
                "revision_file": f"{mode}_revision.json",
            }

        result["automatic"] = await scenario("automatic")
        result["manual"] = await scenario("manual")
        result["status"] = "passed"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        if service is not None:
            service.close()
        result["database"] = str(database)
        result_path = evidence_dir / "result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.evidence_dir))
