"""One bounded real-model child cancellation and Core restart acceptance.

Run from this checkout with OPERANT_BASE_URL, OPERANT_API_KEY and
OPERANT_BETA2_MODEL_ID supplied. The selected ID is checked with Provider
Discovery before the single child model run. All data stays in --evidence-dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from operant.api import create_app
from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread
from operant.persistence.sqlite import SQLiteStore


class PauseAfterFirstRealDelta:
    """Expose one real SSE delta, then hold the stream until cancellation."""

    def __init__(self, provider: Any, closed_marker: Path) -> None:
        self.provider = provider
        self.closed_marker = closed_marker
        self.paused = False

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return await self.provider.list_models(base_url=base_url, secret_ref=secret_ref)

    async def stream(self, **kwargs):  # type: ignore[no-untyped-def]
        source = self.provider.stream(**kwargs)
        try:
            async for event in source:
                yield event
                if not self.paused and event.event_type == "model.delta" and event.delta:
                    self.paused = True
                    await asyncio.Event().wait()
        finally:
            await source.aclose()
            self.closed_marker.write_text("closed", encoding="utf-8")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def wait_for(check, *, timeout: float, description: str):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


def start_core(evidence_dir: Path, port: int, suffix: str) -> subprocess.Popen[bytes]:
    output = (evidence_dir / f"core-{suffix}.log").open("wb")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--serve",
                "--evidence-dir",
                str(evidence_dir),
                "--port",
                str(port),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
    finally:
        output.close()
    return process


def stop_core(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


async def accept(evidence_dir: Path, max_output_tokens: int) -> None:
    base_url = os.environ["OPERANT_BASE_URL"]
    model_id = os.environ["OPERANT_BETA2_MODEL_ID"]
    secret_ref = "OPERANT_API_KEY"
    if not os.environ.get(secret_ref):
        raise RuntimeError(f"{secret_ref} is required")
    evidence_dir = evidence_dir.expanduser().resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    database = evidence_dir / "core.sqlite3"
    if database.exists():
        raise FileExistsError(f"refusing to reuse evidence database: {database}")
    workspace = evidence_dir / "workspace"
    workspace.mkdir()
    result_path = evidence_dir / "result.json"
    result: dict[str, Any] = {
        "status": "failed",
        "model_id": model_id,
        "max_output_tokens": max_output_tokens,
        "database": str(database),
        "controlled_first_real_delta_gate": True,
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        app = create_app(database, artifact_root=evidence_dir / "artifacts")
        service = app.state.operant_service
        try:
            discovered = await service.discover_models(base_url=base_url, secret_ref=secret_ref)
            if model_id not in discovered:
                raise RuntimeError("requested model ID was not returned by Provider Discovery")
            result["discovery_exact_id"] = True
            profile = service.add_model_profile(
                ModelProfile(
                    name="beta2-reliability-real",
                    base_url=base_url,
                    model_id=model_id,
                    secret_ref=secret_ref,
                )
            )
            role = service.create_role(
                RolePreset(
                    name="beta2-reliability-real",
                    model_profile_id=profile.id,
                    system_prompt="Complete only the assigned synthetic task. Use no tools.",
                    tool_policy=ToolPolicy(),
                    budget=Budget(
                        max_turns=1,
                        max_output_tokens=max_output_tokens,
                        timeout_seconds=180,
                    ),
                )
            )
            parent = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
            service.create_session(role.id, thread_id=parent.id)
            parent_id = parent.id
        finally:
            service.close()

        port = free_port()
        process = start_core(evidence_dir, port, "before")
        api = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(timeout=5) as client:

            async def ready() -> bool:
                if process is not None and process.poll() is not None:
                    raise RuntimeError("Core exited before readiness; inspect core-before.log")
                try:
                    response = await client.get(f"{api}/v1/workbench/threads/{parent_id}/children")
                except httpx.TransportError:
                    return False
                return response.status_code == 200

            await wait_for(ready, timeout=25, description="Core startup")
            response = await client.post(
                f"{api}/v1/workbench/threads/{parent_id}/children",
                json={
                    "task": (
                        "Synthetic reliability check: write 180 numbered short lines, "
                        "one per line, with a distinct word on each line. Begin immediately."
                    )
                },
            )
            assert response.status_code == 201, response.text
            child = response.json()
            child_id = str(child["thread_id"])
            session_id = str(child["session_id"])
            result.update(
                parent_thread_id=parent_id,
                child_thread_id=child_id,
                session_id=session_id,
            )
            cursor = 0
            observed_delta: int | None = None

            async def first_delta() -> bool:
                nonlocal cursor, observed_delta
                response = await client.get(
                    f"{api}/v1/sessions/{session_id}/events",
                    params={"after_cursor": cursor},
                )
                assert response.status_code == 200, response.text
                events = response.json()
                saw_delta = False
                for event in events:
                    cursor = max(cursor, int(event["cursor"]))
                    if event["event_type"] == "model.delta" and event["payload"].get("delta"):
                        observed_delta = cursor
                        saw_delta = True
                    if event["event_type"] in {
                        "model.completed",
                        "agent.completed",
                        "agent.failed",
                    }:
                        raise AssertionError("child completed before the cancellation checkpoint")
                return saw_delta

            await wait_for(first_delta, timeout=90, description="real model.delta")
            result["observed_model_delta_cursor"] = observed_delta
            cancelled = await client.post(f"{api}/v1/workbench/threads/{child_id}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"

            async def owner_stopped() -> bool:
                projection = await client.get(f"{api}/v1/workbench/threads/{parent_id}/children")
                assert projection.status_code == 200
                children = projection.json()
                if len(children) != 1 or children[0]["status"] != "cancelled":
                    return False
                events = await client.get(f"{api}/v1/sessions/{session_id}/events")
                assert events.status_code == 200
                terminal = any(e["event_type"] == "agent.cancelled" for e in events.json())
                lease = SQLiteStore(database).get_session_run_lease(session_id)
                return (
                    terminal
                    and lease.released_at is not None
                    and (evidence_dir / "provider-stream-closed.flag").exists()
                )

            await wait_for(
                owner_stopped,
                timeout=25,
                description="owner cancellation and lease release",
            )
            before = await client.get(f"{api}/v1/sessions/{session_id}/events")
            assert before.status_code == 200
            before_events = before.json()
            result["events_before_restart"] = len(before_events)
        stop_core(process)
        process = None

        restart_port = free_port()
        process = start_core(evidence_dir, restart_port, "after")
        restart_api = f"http://127.0.0.1:{restart_port}"
        async with httpx.AsyncClient(timeout=5) as client:

            async def restarted() -> bool:
                if process is not None and process.poll() is not None:
                    raise RuntimeError("Core exited during restart; inspect core-after.log")
                try:
                    response = await client.get(
                        f"{restart_api}/v1/workbench/threads/{parent_id}/children"
                    )
                except httpx.TransportError:
                    return False
                return response.status_code == 200

            await wait_for(restarted, timeout=25, description="Core restart")
            children_response = await client.get(
                f"{restart_api}/v1/workbench/threads/{parent_id}/children"
            )
            children = children_response.json()
            assert len(children) == 1 and children[0]["thread_id"] == child_id
            assert children[0]["status"] == "cancelled"
            await asyncio.sleep(0.5)
            after = await client.get(f"{restart_api}/v1/sessions/{session_id}/events")
            assert after.status_code == 200
            assert len(after.json()) == len(before_events)
            assert sum(e["event_type"] == "agent.cancelled" for e in after.json()) == 1
            assert SQLiteStore(database).get_session_run_lease(session_id).released_at is not None
            result.update(
                status="passed",
                child_count_after_restart=1,
                child_status_after_restart="cancelled",
                events_after_restart=len(after.json()),
                model_replay_after_restart=False,
            )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        stop_core(process)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 512 <= args.max_output_tokens <= 1024:
        parser.error("--max-output-tokens must be between 512 and 1024")
    if args.serve:
        if args.port is None:
            parser.error("--port is required with --serve")
        root = args.evidence_dir.expanduser().resolve()
        app = create_app(root / "core.sqlite3", artifact_root=root / "artifacts")
        if not (root / "provider-stream-closed.flag").exists():
            service = app.state.operant_service
            service.provider = PauseAfterFirstRealDelta(
                service.provider, root / "provider-stream-closed.flag"
            )
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    else:
        asyncio.run(accept(args.evidence_dir, args.max_output_tokens))


if __name__ == "__main__":
    main()
