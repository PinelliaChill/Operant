"""Installed local extension dispatch on the existing isolated package host.

The package process never receives a Core object, database handle or secret.
Registration is not authority: a call checks the installation digest, enabled
state, category grant and exact installation identity each time.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from operant.domain.messages import Message, ProviderEvent, ToolDefinition
from operant.domain.models import RoleSnapshot
from operant.plugins.external_tool import (
    ExtensionCategory,
    ExternalToolRecord,
    ExternalToolRegistry,
    _validate_arguments,
)
from operant.protocol import redact_public_data
from operant.providers.base import ModelProvider


def registry_for_database(database_path: Path) -> ExternalToolRegistry:
    return ExternalToolRegistry(database_path.expanduser().resolve().parent / "external-tools")


def public_record(record: ExternalToolRecord) -> dict[str, Any]:
    manifest = record.manifest
    return {
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "host_api_version": manifest.host_api_version,
        "package_digest": record.package_digest,
        "installation_id": record.installation_id,
        "state": record.state,
        "granted_categories": list(record.granted_categories),
        "sandbox_evidence_ref": record.sandbox_evidence_ref,
        **{
            category: [
                {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.parameters,
                    "granted_name": record.granted_name(definition.name),
                }
                for definition in manifest.operations(category)
            ]
            for category in ("tool", "command", "event", "provider", "runtime", "capability_driver")
        },
    }


def _find(
    registry: ExternalToolRegistry, granted_name: str, category: ExtensionCategory
) -> tuple[ExternalToolRecord, ToolDefinition]:
    for record in registry.list():
        if record.state != "enabled" or category not in record.granted_categories:
            continue
        for definition in record.manifest.operations(category):
            if record.granted_name(definition.name) == granted_name:
                registry.get(record.manifest.plugin_id, require_enabled=True)
                return record, definition
    raise LookupError("enabled extension operation is not registered")


def list_operations(
    registry: ExternalToolRegistry, category: ExtensionCategory
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for record in registry.list():
        if record.state != "enabled" or category not in record.granted_categories:
            continue
        registry.get(record.manifest.plugin_id, require_enabled=True)
        for definition in record.manifest.operations(category):
            operations.append(
                {
                    "plugin_id": record.manifest.plugin_id,
                    "plugin_version": record.manifest.version,
                    "installation_id": record.installation_id,
                    "name": record.granted_name(definition.name),
                    "description": definition.description,
                    "parameters": definition.parameters,
                }
            )
    return operations


def run_operation(
    registry: ExternalToolRegistry,
    granted_name: str,
    category: ExtensionCategory,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    record, definition = _find(registry, granted_name, category)
    return registry.run(
        record.manifest.plugin_id,
        definition.name,
        arguments,
        installation_id=record.installation_id,
        category=category,
    )


def validate_operation_arguments(
    registry: ExternalToolRegistry,
    granted_name: str,
    category: ExtensionCategory,
    arguments: dict[str, Any],
) -> None:
    _, definition = _find(registry, granted_name, category)
    _validate_arguments(
        definition,
        arguments,
        max_string=24_000 if category in {"provider", "runtime"} else 4_000,
    )


def _safe_provider_result(value: dict[str, Any], snapshot: RoleSnapshot) -> RoleSnapshot:
    if set(value) != {"model_id", "base_url"}:
        raise ValueError("provider adapter may only select model_id and base_url")
    model_id, base_url = value["model_id"], value["base_url"]
    if not isinstance(model_id, str) or not 1 <= len(model_id) <= 200:
        raise ValueError("provider adapter model ID is invalid")
    if not isinstance(base_url, str) or not 1 <= len(base_url) <= 2048:
        raise ValueError("provider adapter URL is invalid")
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("provider adapter URL must be credential-free HTTP(S)")
    original = urlsplit(snapshot.base_url)
    if (
        parts.scheme != original.scheme
        or parts.hostname != original.hostname
        or parts.port != original.port
    ):
        raise PermissionError("provider adapter cannot change the credential origin")
    # Core's frozen safety fields, prompt, budget and secret_ref are untouched.
    return snapshot.model_copy(
        update={"provider": "openai-compatible", "model_id": model_id, "base_url": base_url}
    )


class ExtensionModelProvider:
    """Select a granted adapter from ModelProfile.provider, then use Core transport."""

    def __init__(self, delegate: ModelProvider, registry: ExternalToolRegistry) -> None:
        self.delegate = delegate
        self.registry = registry

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return await self.delegate.list_models(base_url=base_url, secret_ref=secret_ref)

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        adapted = snapshot
        for operation in list_operations(self.registry, "runtime"):
            value = await asyncio.to_thread(
                run_operation,
                self.registry,
                str(operation["name"]),
                "runtime",
                {
                    "payload_json": json.dumps(
                        {
                            "model_id": adapted.model_id,
                            "max_output_tokens": adapted.budget.max_output_tokens,
                        },
                        ensure_ascii=False,
                    )
                },
            )
            if value:
                if set(value) != {"max_output_tokens"}:
                    raise ValueError("runtime adapter may only reduce max_output_tokens")
                proposed = value["max_output_tokens"]
                current = adapted.budget.max_output_tokens
                if (
                    not isinstance(proposed, int)
                    or isinstance(proposed, bool)
                    or proposed < 1
                    or (current is not None and proposed > current)
                ):
                    raise ValueError("runtime adapter cannot increase the request budget")
                adapted = adapted.model_copy(
                    update={
                        "budget": adapted.budget.model_copy(update={"max_output_tokens": proposed})
                    }
                )
        if snapshot.provider.startswith("ext_"):
            value = await asyncio.to_thread(
                run_operation,
                self.registry,
                snapshot.provider,
                "provider",
                {
                    "payload_json": json.dumps(
                        {"model_id": snapshot.model_id, "base_url": snapshot.base_url},
                        ensure_ascii=False,
                    )
                },
            )
            adapted = _safe_provider_result(value, adapted)
            discovered = await self.delegate.list_models(
                base_url=adapted.base_url, secret_ref=snapshot.secret_ref
            )
            if adapted.model_id not in discovered:
                raise ValueError("provider adapter selected a model absent from Discovery")
        async for event in self.delegate.stream(snapshot=adapted, messages=messages, tools=tools):
            yield event


async def dispatch_persisted_runtime_event(
    registry: ExternalToolRegistry,
    *,
    event_type: str,
    turn: int,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deliver a redacted, bounded fact after Core has committed its event."""
    if event_type not in {
        "model.completed",
        "tool.completed",
        "tool.failed",
        "agent.completed",
        "agent.failed",
        "agent.cancelled",
    }:
        return []
    context = json.dumps(
        {
            "event_type": event_type,
            "turn": turn,
            "payload_keys": sorted(str(key) for key in payload)[:32],
        },
        ensure_ascii=False,
    )
    results: list[dict[str, Any]] = []
    for category in ("event",):
        try:
            operations = list_operations(registry, category)
        except Exception as exc:
            results.append(
                {
                    "operation": "registry",
                    "category": category,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        for operation in operations:
            if set(operation["parameters"].get("properties", {})) != {"payload_json"}:
                continue
            name = str(operation["name"])
            try:
                value = await asyncio.to_thread(
                    run_operation,
                    registry,
                    name,
                    category,
                    {"payload_json": context},
                )
                results.append(
                    {
                        "operation": name,
                        "category": category,
                        "status": "completed",
                        "summary": redact_public_data(value, max_chars=1_000),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "operation": name,
                        "category": category,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
    return results
