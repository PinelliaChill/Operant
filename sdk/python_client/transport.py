"""Hand-written Python transport and SSE adapters for the generated SDK."""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

Body = str | bytes | bytearray | Iterator[str | bytes] | None
RawText = str | Callable[[], str]

# These limits are deliberately finite at every layer of the SSE parser.  A
# line can be large enough for a bounded JSON field, while a frame may contain
# several lines and a data payload may span multiple ``data:`` fields.  Keep
# the frame limit above the data limit so the parser can report the narrowest
# violated protocol bound.
MAX_SSE_LINE_BYTES = 64 * 1024
MAX_SSE_DATA_BYTES = 256 * 1024
MAX_SSE_FRAME_BYTES = 512 * 1024


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
    """A response carrying raw JSON/text so int64 parsing remains lossless."""

    status: int
    headers: Mapping[str, str]
    body: Body = None
    json: RawText | None = None
    text: RawText | None = None


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
        if not isinstance(value, str):
            raise TypeError("Transport text callback must return raw text")
        return value
    return _body_bytes(response.body).decode("utf-8", errors="replace")


def response_json(response: TransportResponse) -> Any:
    if response.json is not None:
        value = response.json() if callable(response.json) else response.json
        if not isinstance(value, str):
            raise TypeError("Transport JSON must be raw text; pre-parsed objects are unsupported")
        return json.loads(value) if value.strip() else None
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
        valid_recoveries = {
            "none",
            "retry",
            "retry_later",
            "retry_same_idempotency_key",
            "use_new_idempotency_key",
            "refresh_and_retry",
            "manual_reconcile",
        }
        payload: Any = None
        try:
            payload = response_json(response)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            retryable = error.get("retryable") if isinstance(error, dict) else None
            recovery = error.get("recovery") if isinstance(error, dict) else None
            if (
                "detail" in payload
                and isinstance(code, str)
                and bool(code)
                and isinstance(message, str)
                and bool(message)
                and isinstance(retryable, bool)
                and isinstance(recovery, str)
                and recovery in valid_recoveries
            ):
                return cls(
                    code,
                    message,
                    retryable=retryable,
                    recovery=recovery,
                    detail=payload.get("detail"),
                )
        return cls(
            "invalid_error_envelope",
            "Core returned an invalid error envelope",
            retryable=False,
            recovery="none",
            detail=payload,
        )


# Phase 2/3 reuses this bounded transport but exposes a protocol-specific name.
Phase23Error = Phase1EError
# Phase 4/5A reuses the same bounded wire transport and typed error envelope.
Phase45Error = Phase1EError
# Phase 5B/6 reuses the same bounded wire transport and typed error envelope.
Phase56Error = Phase1EError

BetaError = Phase1EError


class SseProtocolError(Phase1EError):
    """A bounded or malformed SSE response from Core."""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(code, message, recovery="manual_reconcile", detail=detail)


def _decode_utf8(decoder: codecs.IncrementalDecoder, data: bytes, *, final: bool) -> str:
    try:
        return decoder.decode(data, final=final)
    except UnicodeDecodeError as exc:
        raise SseProtocolError(
            "sse_invalid_utf8", "SSE response contains invalid UTF-8", detail=None
        ) from exc


def _chunks(source: Body) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    if source is None:
        return
    if isinstance(source, str):
        yield source
        return
    if isinstance(source, (bytes, bytearray)):
        decoded = _decode_utf8(decoder, bytes(source), final=False)
        if decoded:
            yield decoded
        tail = _decode_utf8(decoder, b"", final=True)
        if tail:
            yield tail
        return
    for chunk in source:
        if isinstance(chunk, str):
            tail = _decode_utf8(decoder, b"", final=True)
            if tail:
                yield tail
            yield chunk
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            continue
        decoded = _decode_utf8(decoder, bytes(chunk), final=False)
        if decoded:
            yield decoded
    tail = _decode_utf8(decoder, b"", final=True)
    if tail:
        yield tail


def _sse_line_size(text: str, terminator_bytes: int = 0) -> int:
    try:
        return len(text.encode("utf-8")) + terminator_bytes
    except UnicodeEncodeError as exc:
        raise SseProtocolError("sse_invalid_text", "SSE response contains invalid text") from exc


def _sse_lines(source: Body) -> Iterator[tuple[str, int]]:
    """Yield SSE lines and their raw UTF-8 sizes for LF, CRLF, or CR input."""

    line: list[str] = []
    line_bytes = 0
    pending_cr = False
    for chunk in _chunks(source):
        for character in chunk:
            if pending_cr:
                if character == "\n":
                    size = line_bytes + 2
                    if size > MAX_SSE_LINE_BYTES:
                        raise SseProtocolError(
                            "sse_line_too_large", "SSE line exceeds the protocol limit"
                        )
                    yield "".join(line), size
                    line = []
                    line_bytes = 0
                    pending_cr = False
                    continue
                size = line_bytes + 1
                if size > MAX_SSE_LINE_BYTES:
                    raise SseProtocolError(
                        "sse_line_too_large", "SSE line exceeds the protocol limit"
                    )
                yield "".join(line), size
                line = []
                line_bytes = 0
                pending_cr = False
            if character == "\r":
                pending_cr = True
            elif character == "\n":
                size = line_bytes + 1
                if size > MAX_SSE_LINE_BYTES:
                    raise SseProtocolError(
                        "sse_line_too_large", "SSE line exceeds the protocol limit"
                    )
                yield "".join(line), size
                line = []
                line_bytes = 0
            else:
                line.append(character)
                line_bytes += _sse_line_size(character)
                if line_bytes > MAX_SSE_LINE_BYTES:
                    raise SseProtocolError(
                        "sse_line_too_large", "SSE line exceeds the protocol limit"
                    )
    if pending_cr or line:
        size = line_bytes + (1 if pending_cr else 0)
        if size > MAX_SSE_LINE_BYTES:
            raise SseProtocolError("sse_line_too_large", "SSE line exceeds the protocol limit")
        yield "".join(line), size


def parse_sse(source: Body) -> Iterator[dict[str, Any]]:
    """Parse bounded SSE id/event/data fields, preserving non-JSON data."""

    fields: dict[str, Any] = {"data": []}
    frame_bytes = 0
    data_bytes = 0

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

    for line, line_size in _sse_lines(source):
        frame_bytes += line_size
        if frame_bytes > MAX_SSE_FRAME_BYTES:
            raise SseProtocolError("sse_frame_too_large", "SSE frame exceeds the protocol limit")
        if not line:
            frame = dispatch()
            if frame is not None:
                yield frame
            fields = {"data": []}
            frame_bytes = 0
            data_bytes = 0
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
            value_bytes = _sse_line_size(value)
            data_bytes += value_bytes + (1 if fields["data"] else 0)
            if data_bytes > MAX_SSE_DATA_BYTES:
                raise SseProtocolError("sse_data_too_large", "SSE data exceeds the protocol limit")
            fields["data"].append(value)
    frame = dispatch()
    if frame is not None:
        yield frame
