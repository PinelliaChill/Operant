"""A small, independent key/value notebook memory engine.

Notebook records are parsed as explicit ``key=value`` (or ``key: value``)
pairs.  Trusted Host mode keeps that index in a Host-managed private-index
resource; isolated stdio mode cannot register a resource with the frozen
MP-0 wire surface, so it uses the same exact-key query through
``host.search``.  Both paths return only Core-authorized references and
pending proposals.
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
    spec = importlib.util.spec_from_file_location("_operant_memory_notebook_sdk", path)
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
        PrivateIndexRequest,
        ProposalBatch,
        RecallRequest,
    )

    _IN_PROCESS = True
except ImportError:  # pragma: no cover - exercised by the real stdio process.
    BaseModel = object  # type: ignore[assignment,misc]
    _IN_PROCESS = False


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "memory-notebook-config.v1",
    "match_mode": "exact",
    "case_sensitive": True,
    "separator": "=",
    "max_pairs_per_source": 100,
}


def validate_config(value: Mapping[str, Any] | Any | None = None) -> dict[str, Any]:
    """Validate the notebook-specific configuration and reject fuzzy modes."""

    if value is None:
        raw: dict[str, Any] = {}
    elif hasattr(value, "model_dump"):
        raw = dict(value.model_dump(mode="json"))
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise ValueError("memory-notebook config must be an object")
    unknown = set(raw).difference(DEFAULT_CONFIG)
    if unknown:
        raise ValueError(f"unknown memory-notebook config keys: {sorted(unknown)}")
    result = {**DEFAULT_CONFIG, **raw}
    if result["schema_version"] != DEFAULT_CONFIG["schema_version"]:
        raise ValueError("memory-notebook config schema_version is unsupported")
    if result["match_mode"] != "exact":
        raise ValueError("memory-notebook only supports exact matching")
    if not isinstance(result["case_sensitive"], bool):
        raise ValueError("memory-notebook case_sensitive must be boolean")
    if result["separator"] not in {"=", ":"}:
        raise ValueError("memory-notebook separator must be '=' or ':'")
    if (
        isinstance(result["max_pairs_per_source"], bool)
        or not isinstance(result["max_pairs_per_source"], int)
        or not 1 <= result["max_pairs_per_source"] <= 1000
    ):
        raise ValueError("memory-notebook max_pairs_per_source is out of range")
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load only Core's settings file for this installed package."""

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
        raise ValueError("memory-notebook settings file is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("memory-notebook settings must be an object")
    nested = value.get("plugin_config") or value.get("config") or value.get("settings")
    if isinstance(nested, dict):
        value = nested
    if value.get("schema_version") == "operant-memory-config.v1":
        value = {
            key: value[key] for key in DEFAULT_CONFIG if key in value and key != "schema_version"
        }
    return validate_config(value)


class _HostAdapter:
    def __init__(self, raw: Any, revisions: dict[str, int] | None = None) -> None:
        self.raw = raw
        self.revisions = revisions if revisions is not None else {}

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

    def read_private_index(self, context: Mapping[str, Any]) -> dict[str, Any] | None:
        """Read the persistent index only when Host exposes registration in-process."""

        if not _IN_PROCESS or not hasattr(self.raw, "register_resource"):
            return None
        resource = self.raw.register_resource(
            relative_path="indexes/notebook-key-value.json",
            category="index",
            reconstructible=True,
        )
        expected_revision = self.revisions.get(resource.resource_id, 0)
        # MP-0 exposes a fenced read but no separate ``resource_revision``
        # query.  After a Host restart, probe a small bounded revision window;
        # each successful read remains subject to the Host's CAS fence.
        revisions = [expected_revision, *range(0, 65)]
        result = None
        last_error: Exception | None = None
        for candidate_revision in dict.fromkeys(revisions):
            request = PrivateIndexRequest(
                context=context,
                resource=resource,
                operation="read",
                expected_revision=candidate_revision,
                content_digest=None,
                payload=None,
            )
            try:
                result = self.raw.private_index(request)
            except Exception as exc:
                last_error = exc
                if getattr(exc, "code", None) != "revision_conflict":
                    raise
                continue
            break
        if result is None:
            raise ValueError("notebook private index revision is unavailable") from last_error
        self.revisions[resource.resource_id] = result.revision
        payload = result.payload
        if payload is None or payload == "":
            index = _empty_index()
        else:
            try:
                value = json.loads(payload)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("notebook private index is not valid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError("notebook private index schema is unsupported")
            index = value
        return {"resource": resource, "revision": result.revision, "index": index}

    def write_private_index(
        self, context: Mapping[str, Any], state: Mapping[str, Any], index: Mapping[str, Any]
    ) -> int:
        if not _IN_PROCESS:
            return int(state.get("revision", 0))
        payload = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = _runtime.digest_text(payload)
        request = PrivateIndexRequest(
            context=context,
            resource=state["resource"],
            operation="replace",
            expected_revision=int(state["revision"]),
            content_digest=digest,
            payload=payload,
        )
        result = self.raw.private_index(request)
        self.revisions[state["resource"].resource_id] = result.revision
        return result.revision


def _source_content(text: str) -> tuple[str, dict[str, Any]]:
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


def _pairs(content: str, *, separator: str, max_pairs: int) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        marker = separator if separator in line else (":" if ":" in line else None)
        if marker is None:
            continue
        key, value = (part.strip() for part in line.split(marker, 1))
        if key and value:
            result.append((key, value))
            if len(result) >= max_pairs:
                break
    return result


def _entry_ref(context: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": str(context["dataset_id"]),
        "record_id": str(entry["record_id"]),
        "version": int(entry["version"]),
        "content_digest": str(entry["content_digest"]),
    }


def _record_id(context: Mapping[str, Any], entry_key: str, envelope: Mapping[str, Any]) -> str:
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
    return _runtime.stable_id("notebook", str(context["dataset_id"]), entry_key)


def _proposal_mapping(envelope: Mapping[str, Any]) -> Mapping[str, Any] | None:
    proposal = envelope.get("proposal")
    return proposal if isinstance(proposal, Mapping) else None


def _content_digest(envelope: Mapping[str, Any], source: Mapping[str, Any], computed: str) -> str:
    proposal = _proposal_mapping(envelope)
    proposed_version = proposal.get("proposed_version") if proposal else None
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


def _empty_index() -> dict[str, Any]:
    return {"schema_version": "memory-notebook.index.v2", "published": {}, "pending": {}}


def _index_from_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize the private index without treating old candidates as live."""

    if state is None:
        return _empty_index()
    raw = state.get("index")
    if (
        isinstance(raw, dict)
        and raw.get("schema_version") == "memory-notebook.index.v2"
        and isinstance(raw.get("published"), dict)
        and isinstance(raw.get("pending"), dict)
    ):
        return raw
    converted = _empty_index()
    # A v1 index was only an extraction cache.  Quarantine those entries in
    # pending so a stale candidate cannot become a published recall result.
    old_entries = raw.get("entries") if isinstance(raw, dict) else None
    if isinstance(old_entries, dict):
        for key, value in old_entries.items():
            if not isinstance(value, dict):
                continue
            proposal_id = str(value.get("proposal_id") or _runtime.stable_id("legacy", str(key)))
            converted["pending"][proposal_id] = {"key": key, **value}
    return converted


def _matches_ref(entry: Mapping[str, Any], ref: Mapping[str, Any]) -> bool:
    return all(
        entry.get(field) == ref.get(field) for field in ("record_id", "version", "content_digest")
    )


def _memory_version_source(
    context: Mapping[str, Any], ref: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Turn a Host-authorized version reference into a readable source claim."""

    if ref.get("dataset_id") != context.get("dataset_id"):
        return None
    record_id = ref.get("record_id")
    version = ref.get("version")
    content_digest = ref.get("content_digest")
    if (
        not isinstance(record_id, str)
        or not record_id
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or not isinstance(content_digest, str)
        or len(content_digest) != 64
        or any(char not in "0123456789abcdef" for char in content_digest)
    ):
        return None
    return {
        "source_type": "memory_version",
        "source_id": record_id,
        "revision": version,
        "content_digest": content_digest,
        "scope": context["scope"],
        "permission_epoch": context["permission_epoch"],
        "availability": "available",
    }


async def _exact_search_candidates(
    context: Mapping[str, Any],
    query: str,
    config: Mapping[str, Any],
    result: Mapping[str, Any],
    host: _HostAdapter,
) -> list[dict[str, Any]]:
    """Filter Core's substring candidates by reading each version's content."""

    raw_candidates = result.get("candidates")
    if not isinstance(raw_candidates, list):
        return []
    lookup_key = query if config["case_sensitive"] else query.casefold()
    verified: list[dict[str, Any]] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, Mapping):
            continue
        ref = candidate.get("ref")
        if not isinstance(ref, Mapping):
            continue
        source = _memory_version_source(context, ref)
        if source is None:
            continue
        source_result = await host.read_source(
            {"context": context, "source": source, "max_bytes": 100_000}
        )
        content = source_result.get("text")
        if not isinstance(content, str):
            continue
        content, _ = _source_content(content)
        pairs = _pairs(
            content,
            separator=str(config["separator"]),
            max_pairs=int(config["max_pairs_per_source"]),
        )
        if any(
            (key if config["case_sensitive"] else key.casefold()) == lookup_key
            for key, _value in pairs
        ):
            verified.append(dict(candidate))
    return verified


async def _handle(operation: str, params: dict[str, Any], host: _HostAdapter) -> dict[str, Any]:
    context = _runtime.context_of(params)
    host.check_cancelled()
    config = load_config()
    if operation == "extract":
        sources = params.get("sources")
        if not isinstance(sources, list):
            raise ValueError("extract sources must be a list")
        proposals: list[dict[str, Any]] = []
        state = host.read_private_index(context)
        index = _index_from_state(state)
        published = index["published"]
        pending = index["pending"]
        changed = False
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("extract source must be an object")
            source_result = await host.read_source(
                {"context": context, "source": source, "max_bytes": 100_000}
            )
            text = source_result.get("text")
            if not isinstance(text, str):
                raise ValueError("Host source result has no text")
            content, envelope = _source_content(text)
            pairs = _pairs(
                content,
                separator=str(envelope.get("separator", config["separator"])),
                # A single explicit save is the MP-2 notebook contract.  A
                # second pair is rejected instead of silently becoming a
                # batch whose candidate could race a published value.
                max_pairs=2,
            )
            if len(pairs) > 1:
                raise ValueError("memory-notebook extract accepts one key/value pair per source")
            for key, value in pairs:
                entry_key = key if config["case_sensitive"] else key.casefold()
                pair_content = f"{key}={value}"
                digest = _content_digest(envelope, source, _runtime.digest_text(pair_content))
                previous = published.get(entry_key) if isinstance(published, dict) else None
                proposal = _proposal_mapping(envelope)
                proposed_version = proposal.get("proposed_version") if proposal else None
                envelope_version = envelope.get("version")
                if envelope_version is None and isinstance(proposed_version, Mapping):
                    envelope_version = proposed_version.get("version")
                version = (
                    int(envelope_version)
                    if isinstance(envelope_version, int)
                    and not isinstance(envelope_version, bool)
                    and envelope_version >= 1
                    else 1
                )
                record_id = _record_id(context, entry_key, envelope)
                if isinstance(previous, dict) and previous.get("record_id") == record_id:
                    version = max(version, int(previous.get("version", 0)) + 1)
                operation_name = envelope.get("operation")
                if operation_name is None and proposal:
                    operation_name = proposal.get("operation")
                if operation_name not in {"create", "modify", "merge", "supersede", "revoke"}:
                    operation_name = "modify" if isinstance(previous, dict) else "create"
                base_head: Mapping[str, Any] | None = None
                if proposal:
                    candidate_head = proposal.get("base_head") or proposal.get("head")
                    if isinstance(candidate_head, Mapping):
                        base_head = candidate_head
                if base_head is None and isinstance(previous, dict):
                    old_ref = _entry_ref(context, previous)
                    base_head = {
                        "dataset_id": str(context["dataset_id"]),
                        "record_id": record_id,
                        "published_version": old_ref,
                        "revision": int(previous.get("head_revision", 0)),
                        "publication_cursor": str(previous.get("publication_cursor", "0")),
                        "state": "published",
                        "permission_epoch": int(context["permission_epoch"]),
                    }
                proposal_revision = envelope.get("proposal_revision", 0)
                if proposal_revision == 0 and proposal:
                    proposal_revision = proposal.get("proposal_revision", 0)
                if not isinstance(proposal_revision, int) or isinstance(proposal_revision, bool):
                    proposal_revision = 0
                proposal_value = _runtime.proposal_for(
                    context,
                    record_id=record_id,
                    content_digest=digest,
                    source_refs=[source],
                    extractor_version="memory-notebook.v1",
                    reason=f"exact notebook key: {key}",
                    operation=operation_name,
                    version=version,
                    base_head=base_head,
                    proposal_revision=proposal_revision,
                )
                proposals.append(proposal_value)
                if not isinstance(pending, dict):
                    pending = {}
                    index["pending"] = pending
                pending[proposal_value["proposal_id"]] = {
                    "proposal_id": proposal_value["proposal_id"],
                    "key": key,
                    "record_id": record_id,
                    "version": version,
                    "content_digest": digest,
                    "value": value,
                    "source_id": source["source_id"],
                    "source_revision": source.get("revision", 0),
                    "base_head": dict(base_head) if isinstance(base_head, Mapping) else None,
                }
                changed = True
        if changed and state is not None:
            host.write_private_index(context, state, index)
        return {
            "request_id": str(context["request_id"]),
            "proposals": proposals,
            "source_watermark": str(params.get("source_watermark", "0")),
        }
    if operation == "recall":
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("notebook query must be non-empty text")
        exact = query.strip()
        state = host.read_private_index(context)
        config = load_config()
        lookup_key = exact if config["case_sensitive"] else exact.casefold()
        if state is not None:
            index = _index_from_state(state)
            entry = index["published"].get(lookup_key)
            if isinstance(entry, dict):
                return {
                    "request_id": str(context["request_id"]),
                    "candidates": [{"ref": _entry_ref(context, entry), "score": 1.0}],
                }
            # Rebuild one missing key from Core's already-published search
            # result.  The callback performs the current permission/epoch
            # check; only that result can enter the published index.
            request = dict(params)
            request["query"] = exact
            result = await host.search(request)
            candidates = await _exact_search_candidates(context, exact, config, result, host)
            if candidates:
                ref = candidates[0].get("ref")
                if isinstance(ref, dict):
                    index["published"][lookup_key] = {
                        "key": exact,
                        "record_id": ref.get("record_id"),
                        "version": ref.get("version"),
                        "content_digest": ref.get("content_digest"),
                        "state": "published",
                    }
                    host.write_private_index(context, state, index)
            return {"request_id": str(context["request_id"]), "candidates": candidates}
        # The frozen stdio protocol has no register_resource call.  Preserve
        # exact-key semantics by sending the unmodified key to Core's search
        # callback, whose current index/authorization remains authoritative.
        request = dict(params)
        request["query"] = exact
        result = await host.search(request)
        candidates = await _exact_search_candidates(context, exact, config, result, host)
        return {"request_id": str(context["request_id"]), "candidates": candidates}
    if operation == "maintain":
        return _runtime.empty_proposal_batch(context, str(params.get("source_watermark", "0")))
    if operation == "on_index_event":
        head = params.get("head")
        cursor = head.get("publication_cursor", "0") if isinstance(head, dict) else "0"
        removes_record = isinstance(head, dict) and head.get("state") in {
            "inactive",
            "revoked",
            "deleted",
        }
        outcome = (
            "revoked"
            if isinstance(head, dict) and head.get("state") in {"revoked", "deleted"}
            else "applied"
        )
        state = host.read_private_index(context)
        if state is not None and isinstance(head, dict):
            index = _index_from_state(state)
            published = index["published"]
            pending = index["pending"]
            head_record_id = head.get("record_id")
            head_ref = head.get("published_version")
            changed = False
            for proposal_id, candidate in tuple(pending.items()):
                if not isinstance(candidate, dict) or candidate.get("record_id") != head_record_id:
                    continue
                if removes_record:
                    del pending[proposal_id]
                    changed = True
                    continue
                if head.get("state") != "published" or not isinstance(head_ref, dict):
                    continue
                if not _matches_ref(candidate, head_ref):
                    continue
                key = candidate.get("key")
                if not isinstance(key, str):
                    continue
                published_key = key if config["case_sensitive"] else key.casefold()
                existing = published.get(published_key)
                # A candidate for an already-published *different* record
                # cannot replace that value.  A new version of the same Core
                # record is allowed because the event is the authority.
                if isinstance(existing, dict) and existing.get("record_id") != head_record_id:
                    continue
                promoted = dict(candidate)
                promoted["state"] = "published"
                promoted["head_revision"] = head.get("revision", 0)
                promoted["publication_cursor"] = cursor
                published[published_key] = promoted
                del pending[proposal_id]
                changed = True
            if removes_record:
                for key, published_entry in tuple(published.items()):
                    if (
                        isinstance(published_entry, dict)
                        and published_entry.get("record_id") == head_record_id
                    ):
                        del published[key]
                        changed = True
            if changed:
                host.write_private_index(context, state, index)
        return {
            "event_id": str(params["event_id"]),
            "index_generation": "memory-notebook-index-v1",
            "applied_cursor": str(cursor),
            "outcome": outcome,
        }
    if operation == "lifecycle":
        return _runtime.lifecycle_result(
            context, str(params.get("operation", "unsupported")), params.get("checkpoint_ref")
        )
    raise ValueError(f"unsupported operation: {operation}")


class NotebookMemoryPlugin:
    def __init__(self) -> None:
        # Resource revisions are Host-owned.  Keeping the last observed value
        # avoids issuing a stale CAS after repeated recalls in this process;
        # Host still checks the revision on every read/write.
        self._index_revisions: dict[str, int] = {}

    async def handle(self, operation: str, request: BaseModel, host: Any) -> BaseModel:
        payload = request.model_dump(mode="json")
        result = await _handle(operation, payload, _HostAdapter(host, self._index_revisions))
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


def create_plugin() -> NotebookMemoryPlugin:
    """Entrypoint consumed by ``PluginHost.start(..., trusted_in_process)``."""

    if not _IN_PROCESS:
        raise RuntimeError("create_plugin is only available in a Host process")
    return NotebookMemoryPlugin()


_STDIO_INDEX_REVISIONS: dict[str, int] = {}


def _stdio_handler(operation: str, params: dict[str, Any], host: Any) -> Mapping[str, Any]:
    return asyncio.run(_handle(operation, params, _HostAdapter(host, _STDIO_INDEX_REVISIONS)))


def main() -> int:
    return _runtime.serve_stdio(_stdio_handler, max_bytes=262_144)


if __name__ == "__main__":
    raise SystemExit(main())
