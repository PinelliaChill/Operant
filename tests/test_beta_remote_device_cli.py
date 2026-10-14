from __future__ import annotations

import json
from typing import Any

from operant.remote_control.device_cli import _hello


class _PagedSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.responses = [
            {"type": "hello_ack"},
            {
                "type": "events",
                "items": [
                    {"cursor": index + 1, "command_id": "other", "status": "accepted"}
                    for index in range(200)
                ],
                "next_cursor": 200,
            },
            {
                "type": "events",
                "items": [{"cursor": 201, "command_id": "pending", "status": "completed"}],
                "next_cursor": 201,
            },
        ]

    def send(self, frame: str) -> None:
        self.sent.append(json.loads(frame))

    def recv(self, *, timeout: int) -> str:
        assert timeout == 15
        return json.dumps(self.responses.pop(0))


def test_device_sync_reads_all_pages_before_pending_retry() -> None:
    socket = _PagedSocket()
    events = _hello(
        socket,
        {
            "host_id": "host",
            "device_id": "device",
            "session_id": "session",
            "cursor": 0,
        },
    )
    assert socket.sent[1:] == [
        {"type": "cursor_sync", "after_cursor": 0},
        {"type": "cursor_sync", "after_cursor": 200},
    ]
    assert events["next_cursor"] == 201
    assert events["items"][-1]["command_id"] == "pending"
