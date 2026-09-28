"""Opt-in Agent tool bindings for installed local capability adapters."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from operant.domain.messages import ToolDefinition
from operant.domain.models import ToolPolicy
from operant.domain.security import Capability
from operant.plugins.capability_registry import CapabilityPluginRecord, CapabilityPluginRegistry
from operant.plugins.external_tool import installed_external_tool_extensions
from operant.remote.local_browser import BrowserTargetPolicy
from operant.remote.local_worker import BROWSER_PLUGIN, COMPUTER_PLUGIN, _validate_core_origin
from operant.remote.operator import (
    BrowserCapabilityOperator,
    CapabilityLeaseBinding,
    ComputerCapabilityOperator,
)
from operant.tools.extensions import ToolExtension
from sdk.python_client.phase56_generated import Phase56Client
from sdk.python_client.transport import TransportRequest, TransportResponse

_BROWSER_NAMES = (
    "ext_browser_observe",
    "ext_browser_navigate",
    "ext_browser_fill",
    "ext_browser_click",
)
_COMPUTER_NAMES = ("ext_computer_observe", "ext_computer_click_button")
_ToolSpec = tuple[
    str,
    str,
    dict[str, Any],
    Capability,
    bool,
    Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
]


def _required_arguments(arguments: dict[str, Any], keys: set[str]) -> None:
    if set(arguments) != keys or any(not isinstance(arguments[key], str) for key in keys):
        raise ValueError("capability tool arguments do not match the declared operation")


def _binding(prefix: str) -> CapabilityLeaseBinding | None:
    keys = ("TARGET_ID", "LEASE_ID", "LEASE_FENCING", "LEASE_TOKEN")
    values = {key: os.environ.get(f"OPERANT_{prefix}_{key}", "") for key in keys}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise ValueError(f"{prefix.lower()} capability lease binding is incomplete")
    return CapabilityLeaseBinding(
        target_id=values["TARGET_ID"],
        lease_id=values["LEASE_ID"],
        token=values["LEASE_TOKEN"],
        fencing=int(values["LEASE_FENCING"]),
    )


def _client(core_origin: str) -> Phase56Client:
    origin = _validate_core_origin(core_origin)

    def transport(request: TransportRequest) -> TransportResponse:
        with httpx.Client(timeout=15, trust_env=False) as connection:
            response = connection.request(
                request.method,
                request.url,
                headers=dict(request.headers),
                content=request.body,
            )
        return TransportResponse(
            status=response.status_code,
            headers=dict(response.headers),
            body=response.content,
        )

    return Phase56Client(origin, transport=transport)


def _definition(name: str, description: str, properties: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    )


def _text() -> dict[str, str]:
    return {"type": "string"}


def _tool(
    record: CapabilityPluginRecord,
    *,
    name: str,
    description: str,
    properties: dict[str, Any],
    capability: Capability,
    side_effecting: bool,
    execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> ToolExtension:
    return ToolExtension(
        plugin_id=record.plugin_id,
        plugin_version=record.version,
        host_api_version="operant-tool-extension.v1",
        definition=_definition(name, description, properties),
        capabilities=(capability,),
        side_effecting=side_effecting,
        execute=execute,
        approval_category=("browser" if "browser" in name else "computer"),
    )


def local_capability_tool_extensions(
    database_path: Path, policy: ToolPolicy
) -> dict[str, ToolExtension]:
    """Expose only installed, enabled adapters with an explicit local lease."""
    requested = set(policy.allowed_tools)
    external = installed_external_tool_extensions(database_path, policy)
    if not requested.intersection((*_BROWSER_NAMES, *_COMPUTER_NAMES)):
        return external
    registry = CapabilityPluginRegistry(
        database_path.expanduser().resolve().parent / "capability-plugins"
    )
    origin = _validate_core_origin(os.environ.get("OPERANT_CORE_ORIGIN", "http://127.0.0.1:8000"))
    extensions: dict[str, ToolExtension] = {}

    if requested.intersection(_BROWSER_NAMES):
        browser_binding = _binding("BROWSER")
        try:
            browser_record = registry.get(BROWSER_PLUGIN.plugin_id, require_enabled=True)
        except (KeyError, PermissionError):
            browser_record = None
        if browser_binding is not None and browser_record is not None:

            def browser_operator() -> BrowserCapabilityOperator:
                current = registry.get(BROWSER_PLUGIN.plugin_id, require_enabled=True)
                return BrowserCapabilityOperator(
                    _client(origin),
                    browser_binding,
                    BrowserTargetPolicy(frozenset(current.allowed_targets)),
                )

            async def browser_observe(arguments: dict[str, Any]) -> dict[str, Any]:
                _required_arguments(arguments, set())
                return await asyncio.to_thread(lambda: browser_operator().observe())

            async def browser_navigate(arguments: dict[str, Any]) -> dict[str, Any]:
                _required_arguments(arguments, {"url", "observation_hash", "idempotency_key"})
                return await asyncio.to_thread(
                    lambda: browser_operator().act("navigate", **arguments)
                )

            async def browser_fill(arguments: dict[str, Any]) -> dict[str, Any]:
                _required_arguments(
                    arguments, {"selector", "value", "observation_hash", "idempotency_key"}
                )
                return await asyncio.to_thread(lambda: browser_operator().act("fill", **arguments))

            async def browser_click(arguments: dict[str, Any]) -> dict[str, Any]:
                _required_arguments(arguments, {"selector", "observation_hash", "idempotency_key"})
                return await asyncio.to_thread(lambda: browser_operator().act("click", **arguments))

            browser_specs: tuple[_ToolSpec, ...] = (
                (
                    "ext_browser_observe",
                    "Observe the dedicated browser and return a short-lived observation hash.",
                    {},
                    Capability.BROWSER_OBSERVE,
                    False,
                    browser_observe,
                ),
                (
                    "ext_browser_navigate",
                    "Navigate to an approved exact origin using a current observation hash.",
                    {"url": _text(), "observation_hash": _text(), "idempotency_key": _text()},
                    Capability.BROWSER_NAVIGATE,
                    True,
                    browser_navigate,
                ),
                (
                    "ext_browser_fill",
                    "Fill a non-password field shown in the current browser observation.",
                    {
                        "selector": _text(),
                        "value": _text(),
                        "observation_hash": _text(),
                        "idempotency_key": _text(),
                    },
                    Capability.BROWSER_SUBMIT,
                    True,
                    browser_fill,
                ),
                (
                    "ext_browser_click",
                    "Click an element shown in the current browser observation.",
                    {
                        "selector": _text(),
                        "observation_hash": _text(),
                        "idempotency_key": _text(),
                    },
                    Capability.BROWSER_SUBMIT,
                    True,
                    browser_click,
                ),
            )
            for name, description, properties, capability, side_effecting, execute in browser_specs:
                if name in requested:
                    extensions[name] = _tool(
                        browser_record,
                        name=name,
                        description=description,
                        properties=properties,
                        capability=capability,
                        side_effecting=side_effecting,
                        execute=execute,
                    )

    if requested.intersection(_COMPUTER_NAMES):
        computer_binding = _binding("COMPUTER")
        try:
            computer_record = registry.get(COMPUTER_PLUGIN.plugin_id, require_enabled=True)
        except (KeyError, PermissionError):
            computer_record = None
        if computer_binding is not None and computer_record is not None:

            def computer_operator() -> ComputerCapabilityOperator:
                current = registry.get(COMPUTER_PLUGIN.plugin_id, require_enabled=True)
                return ComputerCapabilityOperator(
                    _client(origin), computer_binding, frozenset(current.allowed_targets)
                )

            async def computer_observe(arguments: dict[str, Any]) -> dict[str, Any]:
                _required_arguments(arguments, set())
                return await asyncio.to_thread(lambda: computer_operator().observe())

            async def computer_click(arguments: dict[str, Any]) -> dict[str, Any]:
                _required_arguments(
                    arguments, {"button_name", "observation_hash", "idempotency_key"}
                )
                return await asyncio.to_thread(
                    lambda: computer_operator().click_button(**arguments)
                )

            computer_specs: tuple[_ToolSpec, ...] = (
                (
                    "ext_computer_observe",
                    "Observe the approved foreground App and return an observation hash.",
                    {},
                    Capability.COMPUTER_OBSERVE,
                    False,
                    computer_observe,
                ),
                (
                    "ext_computer_click_button",
                    "Click a unique button in the approved foreground App.",
                    {
                        "button_name": _text(),
                        "observation_hash": _text(),
                        "idempotency_key": _text(),
                    },
                    Capability.COMPUTER_INPUT,
                    True,
                    computer_click,
                ),
            )
            for (
                name,
                description,
                properties,
                capability,
                side_effecting,
                execute,
            ) in computer_specs:
                if name in requested:
                    extensions[name] = _tool(
                        computer_record,
                        name=name,
                        description=description,
                        properties=properties,
                        capability=capability,
                        side_effecting=side_effecting,
                        execute=execute,
                    )
    if set(extensions).intersection(external):
        raise ValueError("external Tool conflicts with a bundled capability Tool")
    return {**external, **extensions}
