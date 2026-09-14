"""Bounded, stdlib-only JSON-RPC helpers for an isolated memory package."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any


class JsonRpcError(RuntimeError):
    """Raised for malformed or mismatched stdio frames."""


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise JsonRpcError("value is not JSON serializable") from exc


def encode_frame(value: Mapping[str, Any], *, max_bytes: int = 1_000_000) -> bytes:
    encoded = canonical_json(value)
    if len(encoded) > max_bytes:
        raise JsonRpcError("RPC frame exceeds the configured byte limit")
    return encoded + b"\n"


def decode_frame(line: bytes | str, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    raw = line.encode("utf-8") if isinstance(line, str) else line
    if len(raw) > max_bytes + 1:
        raise JsonRpcError("RPC frame exceeds the configured byte limit")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JsonRpcError("RPC frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise JsonRpcError("RPC frame must be an object")
    return value


class StdioHostClient:
    """Synchronous client for the Host callbacks exposed over plugin stdio."""

    def __init__(
        self, stdin: Any = None, stdout: Any = None, *, max_bytes: int = 1_000_000
    ) -> None:
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.max_bytes = max_bytes
        self._request_id = 0

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or not method:
            raise JsonRpcError("Host method must be non-empty text")
        self._request_id += 1
        frame = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": dict(params),
        }
        encoded = encode_frame(frame, max_bytes=self.max_bytes)
        if hasattr(self.stdin, "buffer"):
            self.stdout.write(encoded.decode("utf-8"))
            self.stdout.flush()
            line = self.stdin.readline()
        else:
            self.stdout.write(encoded)
            self.stdout.flush()
            line = self.stdin.readline()
        if not line:
            raise JsonRpcError("Host API response ended unexpectedly")
        response = decode_frame(line, max_bytes=self.max_bytes)
        if response.get("id") != self._request_id:
            raise JsonRpcError("Host API response ID mismatch")
        if "error" in response:
            raise JsonRpcError("Host API request failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise JsonRpcError("Host API result is invalid")
        return result


def serve_stdio(
    handler: Callable[[str, dict[str, Any], StdioHostClient], Mapping[str, Any]],
    *,
    stdin: Any = None,
    stdout: Any = None,
    max_bytes: int = 1_000_000,
) -> int:
    """Serve engine calls until stdin closes.

    The handler receives a reusable Host client.  Diagnostics stay off stdout
    so the Host can parse every line as a JSON-RPC frame.
    """

    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    host = StdioHostClient(input_stream, output_stream, max_bytes=max_bytes)
    for line in input_stream:
        try:
            frame = decode_frame(line, max_bytes=max_bytes)
            method = frame.get("method")
            rpc_id = frame.get("id")
            params = frame.get("params")
            if (
                not isinstance(method, str)
                or not isinstance(rpc_id, int)
                or not isinstance(params, dict)
            ):
                raise JsonRpcError("engine request frame is invalid")
            result = dict(handler(method, params, host))
            response: Mapping[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        except Exception as exc:
            # Do not echo arbitrary exception text over the boundary.  The
            # Host records the typed failure and limits stderr separately.
            response = {
                "jsonrpc": "2.0",
                "id": frame.get("id") if "frame" in locals() else 0,
                "error": {"code": "plugin_failed", "message": str(exc)[:300]},
            }
        encoded = encode_frame(response, max_bytes=max_bytes)
        if hasattr(output_stream, "buffer"):
            output_stream.write(encoded.decode("utf-8"))
        else:
            output_stream.write(encoded)
        output_stream.flush()
    return 0


def digest_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


JsonRpcHostClient = StdioHostClient
HostClient = StdioHostClient
serve = serve_stdio


__all__ = [
    "HostClient",
    "JsonRpcError",
    "JsonRpcHostClient",
    "StdioHostClient",
    "canonical_json",
    "decode_frame",
    "digest_text",
    "encode_frame",
    "serve",
    "serve_stdio",
]
