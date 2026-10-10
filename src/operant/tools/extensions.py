"""Versioned, host-registered tool extension boundary.

An extension is trusted code supplied by the host at construction time.  A
policy name alone never imports a package or creates a callable tool.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from operant.domain.messages import ToolDefinition
from operant.domain.security import Capability

if TYPE_CHECKING:
    from operant.persistence.sqlite import SessionRunLease

EXTENSION_TOOL_NAME = re.compile(r"^ext_[a-z][a-z0-9_]{1,98}$")


@dataclass(frozen=True)
class ToolInvocation:
    tool_call_id: str
    tool_name: str
    receipt_id: str
    action_hash: str
    session_lease: SessionRunLease | None


_CURRENT_INVOCATION: ContextVar[ToolInvocation | None] = ContextVar(
    "operant_tool_invocation", default=None
)


@contextmanager
def bind_tool_invocation(invocation: ToolInvocation | None) -> Iterator[None]:
    token = _CURRENT_INVOCATION.set(invocation)
    try:
        yield
    finally:
        _CURRENT_INVOCATION.reset(token)


def current_tool_invocation() -> ToolInvocation | None:
    return _CURRENT_INVOCATION.get()


@dataclass(frozen=True)
class ToolExtension:
    plugin_id: str
    plugin_version: str
    host_api_version: Literal["operant-tool-extension.v1"]
    definition: ToolDefinition
    capabilities: tuple[Capability, ...]
    side_effecting: bool
    execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    approval_category: str | None = None

    def __post_init__(self) -> None:
        if not self.plugin_id or not self.plugin_version:
            raise ValueError("tool extension identity is incomplete")
        if not EXTENSION_TOOL_NAME.fullmatch(self.definition.name):
            raise ValueError("tool extension name is invalid")
        if not self.capabilities:
            raise ValueError("tool extension has no requested capability")
        if self.definition.parameters.get("type") != "object":
            raise ValueError("tool extension arguments require an object schema")
