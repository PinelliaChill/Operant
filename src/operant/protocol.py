from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

MAX_PUBLIC_TEXT_CHARS = 20_000
MAX_PUBLIC_COLLECTION_ITEMS = 200
MAX_PUBLIC_SERIALIZED_BYTES = 100_000
MAX_PUBLIC_RECURSION_DEPTH = 12
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "client_secret",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "private_key",
        "secret",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "private_key",
    "client_secret",
    "secret",
)
_SECRET_KEY_SUFFIX_PATTERN = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|"
    r"passwd|authorization|cookie|private[_-]?key|token|secret)"
)
_SECRET_KEY_PATTERN = (
    rf"(?:{_SECRET_KEY_SUFFIX_PATTERN}|"
    rf"[A-Za-z_][A-Za-z0-9_-]+{_SECRET_KEY_SUFFIX_PATTERN})"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^\r\n]*?PRIVATE KEY-----.*?"
    r"-----END [^\r\n]*?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_PARTIAL_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^\r\n]*?PRIVATE KEY-----.*",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_PATTERN = re.compile(r"""(?i)\bbearer[ \t]+[^\s\\"']+""")
_BASIC_PATTERN = re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{4,}={0,2}")
_URL_USERINFO_PATTERN = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/@\s]+@)")
_JSON_SECRET_PATTERN = re.compile(
    rf"""(?i)((?:"|')({_SECRET_KEY_PATTERN})(?:"|')\s*:\s*)((?:"|'))(.*?)\3"""
)
_QUOTED_SECRET_PATTERN = re.compile(rf"(?i)\b({_SECRET_KEY_PATTERN})\b(\s*[:=]\s*)(['\"])(.*?)\3")
_UNQUOTED_SECRET_PATTERN = re.compile(
    rf"(?i)\b({_SECRET_KEY_PATTERN})\b(\s*[:=]\s*)([^\s,;\]}}'\"]+)"
)
_STANDALONE_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)


class RecoveryAction(str, Enum):
    NONE = "none"
    RETRY = "retry"
    RETRY_LATER = "retry_later"
    RETRY_SAME_IDEMPOTENCY_KEY = "retry_same_idempotency_key"
    USE_NEW_IDEMPOTENCY_KEY = "use_new_idempotency_key"
    REFRESH_AND_RETRY = "refresh_and_retry"
    MANUAL_RECONCILE = "manual_reconcile"


class ErrorDescriptor(BaseModel):
    """Transport-neutral public error contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    retryable: bool = False
    recovery: RecoveryAction = RecoveryAction.NONE


def error_payload(
    *,
    code: str,
    message: str,
    retryable: bool = False,
    recovery: RecoveryAction = RecoveryAction.NONE,
    legacy_detail: Any | None = None,
) -> dict[str, Any]:
    descriptor = ErrorDescriptor(
        code=code,
        message=message,
        retryable=retryable,
        recovery=recovery,
    )
    return {
        "detail": message if legacy_detail is None else legacy_detail,
        "error": descriptor.model_dump(mode="json"),
    }


def canonical_action_hash(payload: dict[str, Any]) -> str:
    """Hash a normalized action without retaining its possibly sensitive input."""

    canonical = json.dumps(
        {
            "schema": "operant.action.v1",
            "version": 1,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def redact_public_text(value: str, *, max_chars: int = MAX_PUBLIC_TEXT_CHARS) -> str:
    """Remove common credential shapes and bound public/persisted text."""

    redacted = _PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", value)
    redacted = _PARTIAL_PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", redacted)
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    redacted = _BASIC_PATTERN.sub("Basic [REDACTED]", redacted)
    redacted = _URL_USERINFO_PATTERN.sub(r"\1[REDACTED]@", redacted)
    redacted = _JSON_SECRET_PATTERN.sub(r"\1\3[REDACTED]\3", redacted)
    redacted = _QUOTED_SECRET_PATTERN.sub(r"\1\2\3[REDACTED]\3", redacted)
    redacted = _UNQUOTED_SECRET_PATTERN.sub(r"\1\2[REDACTED]", redacted)
    for pattern in _STANDALONE_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    if len(redacted) <= max_chars:
        return redacted
    return f"{redacted[:max_chars]}...[truncated]"


def is_sensitive_key(key: str) -> bool:
    """Return whether a mapping key denotes a credential-bearing value.

    This is the shared key contract for public payload and model Context
    redaction. ``secret_ref`` is deliberately metadata: it names an
    environment variable and is not the secret value itself.
    """

    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized == "secret_ref":
        return False
    compact = normalized.replace("_", "")
    return normalized in _SENSITIVE_KEY_NAMES or any(
        normalized == suffix
        or normalized.endswith(f"_{suffix}")
        or compact.endswith(suffix.replace("_", ""))
        for suffix in _SENSITIVE_KEY_SUFFIXES
    )


def _bounded_public_data(
    value: Any,
    *,
    max_chars: int,
    max_depth: int,
    depth: int,
    remaining_items: list[int],
) -> Any:
    if depth > max_depth:
        return "[truncated: maximum depth]"
    if isinstance(value, str):
        return redact_public_text(value, max_chars=max_chars)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if remaining_items[0] <= 0:
                sanitized["_truncated_items"] = True
                break
            remaining_items[0] -= 1
            safe_key = redact_public_text(str(key), max_chars=500)
            if is_sensitive_key(safe_key):
                sanitized[safe_key] = "[REDACTED]"
            else:
                sanitized[safe_key] = _bounded_public_data(
                    item,
                    max_chars=max_chars,
                    max_depth=max_depth,
                    depth=depth + 1,
                    remaining_items=remaining_items,
                )
        return sanitized
    if isinstance(value, (list, tuple)):
        sanitized_items: list[Any] = []
        for item in value:
            if remaining_items[0] <= 0:
                sanitized_items.append("[truncated items]")
                break
            remaining_items[0] -= 1
            sanitized_items.append(
                _bounded_public_data(
                    item,
                    max_chars=max_chars,
                    max_depth=max_depth,
                    depth=depth + 1,
                    remaining_items=remaining_items,
                )
            )
        return sanitized_items
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_public_text(str(value), max_chars=max_chars)


def redact_public_data(
    value: Any,
    *,
    max_chars: int = MAX_PUBLIC_TEXT_CHARS,
    max_bytes: int = MAX_PUBLIC_SERIALIZED_BYTES,
    max_depth: int = MAX_PUBLIC_RECURSION_DEPTH,
    max_items: int = MAX_PUBLIC_COLLECTION_ITEMS,
) -> Any:
    """Apply one bounded redaction contract to public and persisted payloads."""

    if max_chars < 0 or max_bytes < 1 or max_depth < 0 or max_items < 1:
        raise ValueError("public redaction bounds must be positive")
    sanitized = _bounded_public_data(
        value,
        max_chars=max_chars,
        max_depth=max_depth,
        depth=0,
        remaining_items=[max_items],
    )
    try:
        serialized = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        sanitized = "[redacted: unserializable public payload]"
        serialized = json.dumps(sanitized, separators=(",", ":")).encode("utf-8")
    if len(serialized) <= max_bytes:
        return sanitized

    # The limit applies to the final JSON representation, not to the raw UTF-8
    # of a Python string.  JSON escaping can expand values such as NUL by 6x, so
    # return a small, type-preserving marker instead of slicing serialized JSON.
    if isinstance(sanitized, dict):
        candidates: tuple[Any, ...] = (
            {"_truncated": True, "_reason": "public payload exceeded byte limit"},
            {"_truncated": True},
            {},
        )
    elif isinstance(sanitized, list):
        candidates = (
            ["[truncated: public payload exceeded byte limit]"],
            ["[truncated]"],
            [],
        )
    else:
        candidates = (
            "[truncated: public payload exceeded byte limit]",
            "[truncated]",
            "",
        )
    for candidate in (*candidates, 0):
        candidate_bytes = json.dumps(
            candidate,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(candidate_bytes) <= max_bytes:
            return candidate
    raise AssertionError("a one-byte JSON truncation marker must fit the configured bound")
