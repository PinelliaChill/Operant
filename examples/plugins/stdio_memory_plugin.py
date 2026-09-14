"""A bounded stdio plugin example using the bidirectional Host protocol.

The process can read source text only through ``host.read_source``.  It never
opens a Core database or receives a Host filesystem path.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _decode(line: str) -> dict[str, Any]:
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("RPC frame must be an object")
    return value


class _HostClient:
    """The example package keeps its wire client stdlib-only."""

    def __init__(self) -> None:
        self.request_id = 0

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        frame = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        sys.stdout.write(_encode(frame))
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("Host API response ended unexpectedly")
        response = _decode(line)
        if response.get("id") != self.request_id:
            raise RuntimeError("Host API response ID mismatch")
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Host API result is invalid")
        return result


def _result(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    context = params.get("context")
    if not isinstance(context, dict):
        raise ValueError("request has no context")
    request_id = context["request_id"]
    if operation in {"recall"}:
        return {"request_id": request_id, "candidates": []}
    if operation in {"extract", "maintain"}:
        return {
            "request_id": request_id,
            "proposals": [],
            "source_watermark": params.get("source_watermark", "0"),
        }
    if operation == "on_index_event":
        return {
            "event_id": params["event_id"],
            "index_generation": "example-index-v1",
            "applied_cursor": "0",
            "outcome": "applied",
        }
    if operation == "lifecycle":
        state = "cancelled" if params.get("operation") == "cancel" else "ready"
        return {
            "request_id": request_id,
            "sdk_version": "operant-memory-sdk.v1",
            "state": state,
            "checkpoint_ref": params.get("checkpoint_ref"),
        }
    raise ValueError(f"unsupported operation: {operation}")


def main() -> int:
    host = _HostClient()
    for raw_line in sys.stdin:
        frame = _decode(raw_line)
        if "method" not in frame or not isinstance(frame.get("id"), int):
            continue
        rpc_id = frame["id"]
        try:
            method = frame["method"]
            params = frame.get("params")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("invalid request")
            # The extract example demonstrates the actual callback path.  All
            # other operations are pure deterministic transformations.
            if method == "extract":
                context = params["context"]
                for source in params.get("sources", ()):
                    host.request(
                        "host.read_source",
                        {"context": context, "source": source, "max_bytes": 4_096},
                    )
            result = _result(method, params)
            response: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": "plugin_failed", "message": str(exc)[:300]},
            }
        sys.stdout.write(_encode(response))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
