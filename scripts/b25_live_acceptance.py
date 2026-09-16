"""Bounded B2-5 verification via public commands, Scheduler and a real model.

Only synthetic source content is sent. Credentials are read into this process;
no credential, provider URL or original request payload is written as evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-id", default="gpt-5.6-luna")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runroot = args.run_root.resolve()
    runroot.mkdir(parents=True, exist_ok=False)
    workspace = runroot / "workspace"
    workspace.mkdir()
    from operant.settings import load_local_env

    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    os.environ["OPERANT_DB_PATH"] = str(runroot / "core.sqlite3")
    result: dict[str, object] = {
        "phase": "b25_real_acceptance",
        "model_id": args.model_id,
        "workspace": str(workspace),
        "database": os.environ["OPERANT_DB_PATH"],
        "source_hashes": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "src/operant").rglob("*.py"))
        },
        "checks": {},
        "completed": False,
    }
    checks: dict[str, object] = {}
    result["checks"] = checks
    try:
        discovery = subprocess.run(
            ["uv", "run", "--frozen", "operant", "model", "discover"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=50,
        )
        discovered = {line.strip() for line in discovery.stdout.splitlines()}
        if discovery.returncode != 0 or args.model_id not in discovered:
            raise RuntimeError("discovery_unavailable")
        checks["discovery"] = {"entry": "uv run operant model discover", "selected": args.model_id}
        from fastapi.testclient import TestClient

        from operant.api import create_app
        from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
        from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload

        app = create_app(runroot / "core.sqlite3")
        service = app.state.operant_service
        profile = service.add_model_profile(
            ModelProfile(
                name="B25 synthetic verification",
                model_id=args.model_id,
                base_url=os.environ["OPERANT_BASE_URL"],
                secret_ref="OPERANT_API_KEY",
                context_window=32768,
            )
        )
        result["model_profile_id"] = profile.id
        with TestClient(app) as client:

            def command(body: dict[str, object], *, version: str = "b2-5", key: str | None = None):
                response = client.post(
                    f"/v1/{version}/commands",
                    json=body,
                    headers={"Idempotency-Key": key or f"verify-{time.monotonic_ns()}"},
                )
                if response.status_code != 200:
                    detail = response.json().get("detail", {})
                    code = (
                        detail.get("code", "http_error")
                        if isinstance(detail, dict)
                        else "http_error"
                    )
                    raise RuntimeError(f"{body['action']}:{response.status_code}:{code}")
                return response.json()

            project = command(
                {
                    "action": "project_create",
                    "name": "B25 真实整理验收",
                    "workspace_path": str(workspace),
                },
                version="b2-3",
            )["state"]["projects"][-1]["project_id"]
            installation = command(
                {
                    "action": "plugin_install",
                    "plugin_id": "memory-standard",
                    "mode": "trusted_in_process",
                },
                version="b2-3",
            )["state"]["installations"][-1]["installation_id"]
            command(
                {
                    "action": "binding_select",
                    "project_id": project,
                    "installation_id": installation,
                },
                version="b2-3",
            )
            command({"action": "maintenance_configure", "project_id": project, "enabled": True})
            result["project_id"] = project
            thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
            turn = service.create_turn(Turn(thread_id=thread.id))
            source = service.append_item(
                Item(
                    thread_id=thread.id,
                    turn_id=turn.id,
                    payload=UserMessagePayload(
                        text=(
                            "本合成验收项目的测试代号固定为 B25_CEDAR_73。"
                            "这是明确的项目约定，请在后续项目任务中沿用。"
                        )
                    ),
                )
            )
            result["source_item_id"] = source.id

            def wait_job(job_id: str):
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    response = client.get(f"/v1/b2-5/projects/{project}/governance")
                    if response.status_code != 200:
                        raise RuntimeError("job_projection_unavailable")
                    job = next((j for j in response.json()["jobs"] if j["job_id"] == job_id), None)
                    if job and job["state"] in {
                        "succeeded",
                        "no_change",
                        "dead_letter",
                        "cancelled",
                        "blocked",
                        "manual_reconcile_required",
                    }:
                        return job
                    time.sleep(0.5)
                raise TimeoutError("maintenance_job_timeout")

            request = {
                "action": "maintenance_create",
                "project_id": project,
                "model_profile_id": profile.id,
                "max_sources": 1,
                "max_output_tokens": 512,
                "max_attempts": 1,
            }
            created = command(request, key="b25-first-job")
            job_id = created["affected_ids"][0]
            job = wait_job(job_id)
            checks["maintenance"] = job
            if job["state"] != "succeeded" or not job["proposal_ids"]:
                raise RuntimeError("maintenance_did_not_produce_candidate")
            state = client.get(f"/v1/b2-5/projects/{project}/governance").json()
            entry = next(
                e
                for e in state["proposals"]
                if e["proposal"]["proposal_id"] == job["proposal_ids"][0]
            )
            assert entry["proposal"]["state"] == "pending"
            assert entry["version"]["evidence"] == "inferred"
            assert all(s["source_id"] == source.id for s in entry["version"]["sources"])
            checks["no_self_publication"] = not any(
                r["head"]["state"] == "published" for r in state["records"]
            )
            repeated = command(request, key="b25-first-job")
            assert repeated["affected_ids"] == [job_id]
            checks["repeat_idempotent"] = True
            no_new = command(request, key="b25-no-new-source")["affected_ids"][0]
            no_change = wait_job(no_new)
            checks["no_new_source"] = no_change
            assert no_change["state"] == "no_change" and not no_change["proposal_ids"]
            assert no_change["input_tokens"] == no_change["output_tokens"] == 0
            proposal = entry["proposal"]
            selected = {
                "proposal_id": proposal["proposal_id"],
                "proposal_revision": proposal["proposal_revision"],
                "proposed_version": proposal["proposed_version"],
                "base_head_revision": proposal["base_head"]["revision"],
            }
            accepted = command(
                {
                    "action": "review",
                    "project_id": project,
                    "decision": "accept",
                    "selections": [selected],
                }
            )
            assert any(r["head"]["state"] == "published" for r in accepted["state"]["records"])
            checks["explicit_confirmation"] = True
            role = service.create_role(
                RolePreset(
                    name="B25 read-only recall",
                    model_profile_id=profile.id,
                    system_prompt="依据授权项目记忆回答测试代号，简短回答。",
                    tool_policy=ToolPolicy(),
                    memory_scope="read: [project]; write: []",
                    budget=Budget(max_turns=1, max_output_tokens=128, timeout_seconds=60),
                )
            )
            session_thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
            session = service.create_session(role.id, thread_id=session_thread.id)
            result["session_id"] = session.id

            async def run():
                return [
                    e
                    async for e in service.run_session(
                        session.id,
                        user_message="本项目的测试代号是什么？",
                        workspace=str(workspace),
                        thread_id=session_thread.id,
                    )
                ]

            events = client.portal.call(run)
            answer = next(
                (
                    str(e.payload.get("content", ""))
                    for e in events
                    if e.event_type == "agent.completed"
                ),
                "",
            )
            checks["formal_recall"] = {
                "completed": bool(answer),
                "contains_expected_code": "B25_CEDAR_73" in answer,
                "entry": "ApplicationService.run_session",
                "tool_policy": [],
            }
            assert "B25_CEDAR_73" in answer
            command(
                {
                    "action": "source_revoke",
                    "project_id": project,
                    "source": entry["version"]["sources"][0],
                }
            )
            impact = client.get(f"/v1/b2-5/sessions/{session.id}/context-impact").json()
            checks["context_impact"] = impact
            assert any(not x["source_and_time_valid"] for x in impact["entries"])

            async def refused():
                try:
                    blocked_events = await run()
                except PermissionError:
                    return True
                return any(
                    e.event_type == "agent.failed"
                    and e.payload.get("error_type") == "PermissionError"
                    for e in blocked_events
                ) and not any(e.event_type == "model.completed" for e in blocked_events)

            checks["revoked_next_send_blocked"] = client.portal.call(refused)
            assert checks["revoked_next_send_blocked"]
            command({"action": "maintenance_configure", "project_id": project, "enabled": False})
            blocked = client.post("/v1/b2-5/commands", json=request)
            checks["disabled_new_job_blocked"] = blocked.status_code in {403, 409}
            assert checks["disabled_new_job_blocked"]
            result["completed"] = True
    except Exception as exc:
        result["error_class"] = type(exc).__name__
        # Only static assertion labels or safe typed codes become report text.
        if isinstance(exc, (RuntimeError, TimeoutError)):
            result["error_code"] = str(exc)[:200]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"source_hashes", "checks"}},
            ensure_ascii=False,
        )
    )
    print("checks_recorded=" + ",".join(checks))


if __name__ == "__main__":
    main()
