"""Isolated Core process for workbench crash recovery tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import uvicorn

from operant.api import create_app
from operant.domain.messages import ModelResponse, ProviderEvent, ToolCall


class CrashProvider:
    def __init__(self, mode: str, marker: Path, recipient: str) -> None:
        self.mode = mode
        self.marker = marker
        self.recipient = recipient

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["local-test"]

    async def stream(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        if self.mode == "model":
            self.marker.write_text("model.entered", encoding="utf-8")
            await asyncio.Event().wait()
        if self.mode == "tool":
            call = ToolCall(
                id="send-once",
                name="send_agent_message",
                arguments_json=json.dumps(
                    {
                        "recipient_thread_id": self.recipient,
                        "body": "durable once",
                        "idempotency_key": "crash-once",
                    }
                ),
            )
        elif self.mode == "approval":
            call = ToolCall(
                id="approval-once",
                name="run_command",
                arguments_json=json.dumps({"argv": ["git", "add", "change.txt"]}),
            )
        else:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="unexpected resumed call", finish_reason="stop"),
            )
            return
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(tool_calls=(call,), finish_reason="tool_calls"),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--mode", choices=("model", "tool", "approval", "complete"), required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    app = create_app(args.db, artifact_root=args.artifact_root)
    service = app.state.operant_service
    service.provider = CrashProvider(args.mode, args.marker, args.recipient)
    service._session_lease_ttl_seconds = 1.5
    service._session_lease_heartbeat_seconds = 0.2
    if args.mode == "tool":
        complete = service.store.complete_tool_action

        def crash_before_receipt(*call_args: Any, **call_kwargs: Any) -> Any:
            args.marker.write_text("tool.effect_committed", encoding="utf-8")
            time.sleep(120)
            return complete(*call_args, **call_kwargs)

        service.store.complete_tool_action = crash_before_receipt
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
