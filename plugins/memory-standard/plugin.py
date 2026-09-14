"""Default, source-backed Operant memory engine.

The package has one implementation for both Host modes.  In a trusted
process the adapter converts dictionaries to the frozen Pydantic contracts;
in an isolated process the same logic uses the copied stdlib runtime and the
bidirectional Host JSON-RPC channel.  The engine only creates proposals and
candidate references.  It never publishes a head or decides that a source is
trusted evidence.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _load_runtime() -> Any:
    path = Path(__file__).with_name("memory_plugin_sdk.py")
    spec = importlib.util.spec_from_file_location("_operant_memory_standard_sdk", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("memory plugin stdlib runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_runtime = _load_runtime()

try:  # The isolated ``-I -S`` path intentionally takes the except branch.
    from pydantic import BaseModel

    from operant.contracts.b2_1 import (
        CandidateBatch,
        HostReadRequest,
        LifecycleResult,
        ProposalBatch,
        RecallRequest,
    )

    _IN_PROCESS = True
except ImportError:  # pragma: no cover - exercised by the real stdio process.
    BaseModel = object  # type: ignore[assignment,misc]
    _IN_PROCESS = False


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "memory-standard-config.v1",
    "normalization": "collapsed_whitespace",
    "max_source_bytes": 100_000,
    "maintenance_enabled": True,
}


def validate_config(value: Mapping[str, Any] | Any | None = None) -> dict[str, Any]:
    """Validate the package-specific config without changing Host config."""

    if value is None:
        raw: dict[str, Any] = {}
    elif hasattr(value, "model_dump"):
        raw = dict(value.model_dump(mode="json"))
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise ValueError("memory-standard config must be an object")
    unknown = set(raw).difference(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"unknown memory-standard config keys: {sorted(unknown)}")
    result = {**DEFAULT_CONFIG, **raw}
    if result["schema_version"] != DEFAULT_CONFIG["schema_version"]:
        raise ValueError("memory-standard config schema_version is unsupported")
    if result["normalization"] not in {"collapsed_whitespace", "preserve_lines"}:
        raise ValueError("memory-standard normalization is unsupported")
    if (
        isinstance(result["max_source_bytes"], bool)
        or not isinstance(result["max_source_bytes"], int)
        or not 1 <= result["max_source_bytes"] <= 100_000
    ):
        raise ValueError("memory-standard max_source_bytes is out of range")
    if not isinstance(result["maintenance_enabled"], bool):
        raise ValueError("memory-standard maintenance_enabled must be boolean")
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load the Core-written settings file, falling back to package defaults.

    A package can only read its own installed ``config`` directory.  The
    optional path is useful for an isolated test and is still validated as a
    regular file; the runtime call uses ``<installation>/config/settings.json``
    derived from this entrypoint.
    """

    settings_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parent.parent / "config" / "settings.json"
    )
    try:
        if settings_path.is_symlink() or not settings_path.is_file():
            return validate_config()
        value = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return validate_config()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("memory-standard settings file is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("memory-standard settings must be an object")
    nested = value.get("plugin_config") or value.get("config") or value.get("settings")
    if isinstance(nested, dict):
        value = nested
    if value.get("schema_version") == "operant-memory-config.v1":
        # PluginHost's common binding config may be the source of the file.
        # Only the package-owned keys are projected from it.
        value = {
            key: value[key] for key in DEFAULT_CONFIG if key in value and key != "schema_version"
        }
    return validate_config(value)


class _HostAdapter:
    """Normalize trusted Host API objects and stdio Host clients to one shape."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def check_cancelled(self) -> None:
        callback = getattr(self.raw, "check_cancelled", None)
        if callable(callback):
            callback()

    async def read_source(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if _IN_PROCESS:
            request = HostReadRequest.model_validate(params)
            result = self.raw.read_source(request)
            if hasattr(result, "__await__"):
                result = await result
            return result.model_dump(mode="json")
        return self.raw.request("host.read_source", params)

    async def search(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if _IN_PROCESS:
            request = RecallRequest.model_validate(params)
            result = self.raw.search(request)
            if hasattr(result, "__await__"):
                result = await result
            return result.model_dump(mode="json")
        return self.raw.request("host.search", params)


def _source_content(text: str) -> tuple[str, dict[str, Any]]:
    """Read Core's source envelope while accepting plain text sources too."""

    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text, {}
    if not isinstance(value, dict):
        return text, {}
    payload = value.get("payload")
    merged = {**value, **payload} if isinstance(payload, dict) else value
    for key in ("content", "text"):
        content = merged.get(key)
        if isinstance(content, str):
            return content, merged
    if isinstance(payload, str):
        return payload, merged
    return text, merged


def _record_id(
    context: Mapping[str, Any], source: Mapping[str, Any], envelope: Mapping[str, Any]
) -> str:
    explicit = envelope.get("record_id")
    proposal = envelope.get("proposal")
    if not isinstance(explicit, str) and isinstance(proposal, Mapping):
        explicit = proposal.get("record_id")
        if not isinstance(explicit, str):
            proposed_version = proposal.get("proposed_version")
            if isinstance(proposed_version, Mapping):
                explicit = proposed_version.get("record_id")
    if (
        isinstance(explicit, str)
        and explicit
        and explicit[0].isalnum()
        and all(char.isalnum() or char in "_.:-" for char in explicit)
    ):
        return explicit[:200]
    return _runtime.stable_id(
        "memory",
        str(context["dataset_id"]),
        str(source.get("source_type", "source")),
        str(source["source_id"]),
    )


def _base_head(envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
    proposal = envelope.get("proposal")
    if isinstance(proposal, Mapping):
        candidate = proposal.get("base_head") or proposal.get("head")
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _content_digest(envelope: Mapping[str, Any], source: Mapping[str, Any], computed: str) -> str:
    """Prefer the digest Core put on the pending version/source envelope."""

    proposal = envelope.get("proposal")
    proposed_version = proposal.get("proposed_version") if isinstance(proposal, Mapping) else None
    candidates = [
        proposed_version.get("content_digest") if isinstance(proposed_version, Mapping) else None,
        envelope.get("content_digest"),
    ]
    if "record_id" in envelope or isinstance(proposed_version, Mapping):
        candidates.append(source.get("content_digest"))
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and len(candidate) == 64
            and all(char in "0123456789abcdef" for char in candidate)
        ):
            return candidate
    return computed


async def _handle(operation: str, params: dict[str, Any], host: _HostAdapter) -> dict[str, Any]:
    context = _runtime.context_of(params)
    host.check_cancelled()
    config = load_config()
    if operation == "extract":
        sources = params.get("sources")
        if not isinstance(sources, list):
            raise ValueError("extract sources must be a list")
        proposals: list[dict[str, Any]] = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("extract source must be an object")
            source_result = await host.read_source(
                {
                    "context": context,
                    "source": source,
                    "max_bytes": config["max_source_bytes"],
                }
            )
            text = source_result.get("text")
            if not isinstance(text, str):
                raise ValueError("Host source result has no text")
            content, envelope = _source_content(text)
            if config["normalization"] == "preserve_lines":
                normalized = _runtime.normalize_text(content, preserve_lines=True)
            else:
                normalized = _runtime.normalize_text(content)
            if not normalized:
                continue
            proposal = envelope.get("proposal")
            proposed_version = (
                proposal.get("proposed_version") if isinstance(proposal, Mapping) else None
            )
            version = envelope.get(
                "version",
                proposed_version.get("version", 1) if isinstance(proposed_version, Mapping) else 1,
            )
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                version = 1
            operation_name = envelope.get("operation")
            if operation_name is None and isinstance(proposal, Mapping):
                operation_name = proposal.get("operation")
            if operation_name not in {"create", "modify", "merge", "supersede", "revoke"}:
                operation_name = "create"
            proposal_revision = envelope.get("proposal_revision", 0)
            if proposal_revision == 0 and isinstance(proposal, Mapping):
                proposal_revision = proposal.get("proposal_revision", 0)
            proposals.append(
                _runtime.proposal_for(
                    context,
                    record_id=_record_id(context, source, envelope),
                    content_digest=_content_digest(
                        envelope, source, _runtime.digest_text(normalized)
                    ),
                    source_refs=[source],
                    extractor_version="memory-standard.v1",
                    reason="source-backed standard memory extraction",
                    operation=operation_name,
                    version=version,
                    base_head=_base_head(envelope),
                    proposal_revision=(
                        proposal_revision
                        if isinstance(proposal_revision, int)
                        and not isinstance(proposal_revision, bool)
                        else 0
                    ),
                )
            )
        return {
            "request_id": str(context["request_id"]),
            "proposals": proposals,
            "source_watermark": str(params.get("source_watermark", "0")),
        }
    if operation == "recall":
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("recall query must be non-empty text")
        request = dict(params)
        request["query"] = query.strip()
        # Core's search callback is the authority for candidates, scores and
        # current permissions.  This plugin never invents a MemoryVersionRef.
        return await host.search(request)
    if operation == "maintain":
        return _runtime.empty_proposal_batch(context, str(params.get("source_watermark", "0")))
    if operation == "on_index_event":
        head = params.get("head")
        cursor = head.get("publication_cursor", "0") if isinstance(head, dict) else "0"
        outcome = (
            "revoked"
            if isinstance(head, dict) and head.get("state") in {"revoked", "deleted"}
            else "applied"
        )
        return {
            "event_id": str(params["event_id"]),
            "index_generation": "memory-standard-index-v1",
            "applied_cursor": str(cursor),
            "outcome": outcome,
        }
    if operation == "lifecycle":
        return _runtime.lifecycle_result(
            context, str(params.get("operation", "unsupported")), params.get("checkpoint_ref")
        )
    raise ValueError(f"unsupported operation: {operation}")


class StandardMemoryPlugin:
    """Host adapter returned by ``create_plugin`` in trusted mode."""

    async def handle(self, operation: str, request: BaseModel, host: Any) -> BaseModel:
        payload = request.model_dump(mode="json")
        result = await _handle(operation, payload, _HostAdapter(host))
        result_type: Any = {
            "extract": ProposalBatch,
            "recall": CandidateBatch,
            "maintain": ProposalBatch,
            "on_index_event": __import__(
                "operant.contracts.b2_1", fromlist=["IndexReceipt"]
            ).IndexReceipt,
            "lifecycle": LifecycleResult,
        }[operation]
        return result_type.model_validate(result)


def create_plugin() -> StandardMemoryPlugin:
    """Entrypoint consumed by ``PluginHost.start(..., trusted_in_process)``."""

    if not _IN_PROCESS:
        raise RuntimeError("create_plugin is only available in a Host process")
    return StandardMemoryPlugin()


def _stdio_handler(operation: str, params: dict[str, Any], host: Any) -> Mapping[str, Any]:
    return asyncio.run(_handle(operation, params, _HostAdapter(host)))


def main() -> int:
    return _runtime.serve_stdio(_stdio_handler, max_bytes=262_144)


if __name__ == "__main__":
    raise SystemExit(main())
