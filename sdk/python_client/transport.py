"""Hand-written Python transport and SSE adapters for the generated SDK."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

Body = str | bytes | bytearray | Iterator[str | bytes] | None


class HeaderCollection(Protocol):
    def items(self) -> Iterable[tuple[str, str]]: ...


@dataclass(frozen=True)
class TransportRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: str | None = None


@dataclass
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: Body = None
    json: Any = None
    text: Any = None


Transport = Callable[[TransportRequest], TransportResponse]


def _header_mapping(headers: HeaderCollection) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def default_transport(request: TransportRequest) -> TransportResponse:
    """Perform one HTTP request without hiding non-2xx response envelopes."""

    raw_body = None if request.body is None else request.body.encode("utf-8")
    outgoing = Request(
        request.url,
        data=raw_body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with urlopen(outgoing) as response:  # noqa: S310 - caller supplies the base URL
            body = response.read()
            return TransportResponse(
                status=response.status,
                headers=_header_mapping(response.headers),
                body=body,
            )
    except HTTPError as error:
        return TransportResponse(
            status=error.code,
            headers=_header_mapping(error.headers),
            body=error.read(),
        )
    except (OSError, TimeoutError, URLError) as error:
        raise Phase1EError(
            "transport_unavailable",
            "Core transport is unavailable",
            retryable=True,
            recovery="retry_later",
            detail=str(error),
        ) from error


def response_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _body_bytes(body: Body) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return b"".join(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8") for chunk in body)


def response_text(response: TransportResponse) -> str:
    if isinstance(response.text, str):
        return response.text
    if callable(response.text):
        value = response.text()
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
    return _body_bytes(response.body).decode("utf-8", errors="replace")


def response_json(response: TransportResponse) -> Any:
    if response.json is not None:
        value = response.json() if callable(response.json) else response.json
        return value
    text = response_text(response)
    if not text.strip():
        return None
    return json.loads(text)


class Phase1EError(RuntimeError):
    """Typed public error; recovery comes from the envelope, not HTTP prose."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        recovery: str = "none",
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.recovery = recovery
        self.detail = detail

    @classmethod
    def from_response(cls, response: TransportResponse) -> Phase1EError:
        payload: Any = None
        try:
            payload = response_json(response)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                if isinstance(code, str) and isinstance(message, str):
                    return cls(
                        code,
                        message,
                        retryable=error.get("retryable") is True,
                        recovery=(
                            error["recovery"] if isinstance(error.get("recovery"), str) else "none"
                        ),
                        detail=payload.get("detail"),
                    )
            detail = payload.get("detail")
            if isinstance(detail, str):
                return cls(f"http_{response.status}", detail, detail=detail)
        return cls(
            f"http_{response.status}",
            f"HTTP {response.status} request failed",
            retryable=response.status >= 500,
            detail=payload,
        )


def _chunks(source: Body) -> Iterator[str]:
    if source is None:
        return
    if isinstance(source, str):
        yield source
        return
    if isinstance(source, (bytes, bytearray)):
        yield bytes(source).decode("utf-8", errors="replace")
        return
    for chunk in source:
        yield chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk


def parse_sse(source: Body) -> Iterator[dict[str, Any]]:
    """Parse standard SSE id/event/data fields, preserving JSON data values."""

    buffer = ""
    fields: dict[str, Any] = {"data": []}

    def dispatch() -> dict[str, Any] | None:
        if not fields["data"]:
            return None
        raw_data = "\n".join(fields["data"])
        try:
            data: Any = json.loads(raw_data)
        except json.JSONDecodeError:
            data = raw_data
        frame: dict[str, Any] = {"data": data}
        if "id" in fields:
            frame["id"] = fields["id"]
        if "event" in fields:
            frame["event"] = fields["event"]
        return frame

    for chunk in _chunks(source):
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            if not line:
                frame = dispatch()
                if frame is not None:
                    yield frame
                fields = {"data": []}
                continue
            if line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if name == "id":
                fields["id"] = value
            elif name == "event":
                fields["event"] = value
            elif name == "data":
                fields["data"].append(value)
    if buffer:
        line = buffer[:-1] if buffer.endswith("\r") else buffer
        if line.startswith("data:"):
            fields["data"].append(line[5:].lstrip(" "))
    frame = dispatch()
    if frame is not None:
        yield frame
