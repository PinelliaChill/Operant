"""Single-file stdlib runtime copied into installed memory packages.

The file deliberately has no package-relative imports.  ``build_packages.py``
copies it as ``memory_plugin_sdk.py`` so ``python -I -S plugin.py`` can load it
from an absolute ``__file__`` path.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


class JsonRpcError(RuntimeError):
    """Raised for malformed or mismatched stdio frames."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


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
    """Synchronous client for Host callbacks exposed over plugin stdio."""

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
        if hasattr(self.stdout, "buffer"):
            self.stdout.write(encoded.decode("utf-8"))
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
    """Serve engine calls until stdin closes, preserving JSON-only stdout."""

    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    host = StdioHostClient(input_stream, output_stream, max_bytes=max_bytes)
    for line in input_stream:
        frame: dict[str, Any] = {}
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
            response = {
                "jsonrpc": "2.0",
                "id": frame.get("id", 0),
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
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str, length: int = 32) -> str:
    material = "\x1f".join((prefix, *parts)).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()[:length]}"


def context_of(params: Mapping[str, Any]) -> dict[str, Any]:
    context = params.get("context")
    if not isinstance(context, dict):
        raise ValueError("request context must be an object")
    required = (
        "request_id",
        "installation_id",
        "dataset_id",
        "scope",
        "permission_epoch",
    )
    if any(not isinstance(context.get(key), (str, int, dict)) for key in required):
        raise ValueError("request context is incomplete")
    if not isinstance(context.get("request_id"), str) or not context["request_id"]:
        raise ValueError("request context has no request_id")
    if not isinstance(context.get("dataset_id"), str) or not context["dataset_id"]:
        raise ValueError("request context has no dataset_id")
    if not isinstance(context.get("scope"), dict):
        raise ValueError("request context has no scope")
    return context


def normalize_text(text: str, *, preserve_lines: bool = False) -> str:
    if not isinstance(text, str):
        raise ValueError("source text must be text")
    if preserve_lines:
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return " ".join(text.split())


def proposal_for(
    context: Mapping[str, Any],
    *,
    record_id: str,
    content_digest: str,
    source_refs: list[dict[str, Any]],
    extractor_version: str,
    reason: str,
    operation: str = "create",
    version: int = 1,
    base_head: Mapping[str, Any] | None = None,
    proposal_revision: int = 0,
) -> dict[str, Any]:
    dataset_id = str(context["dataset_id"])
    permission_epoch = int(context["permission_epoch"])
    owner = {
        "kind": "plugin_dataset",
        "owner_namespace": f"dataset:{dataset_id}",
        "dataset_id": dataset_id,
        "principal_id": "user",
    }
    ref = {
        "dataset_id": dataset_id,
        "record_id": record_id,
        "version": version,
        "content_digest": content_digest,
    }
    head = dict(
        base_head
        or {
            "dataset_id": dataset_id,
            "record_id": record_id,
            "published_version": None,
            "revision": 0,
            "publication_cursor": "0",
            "state": "unpublished",
            "permission_epoch": permission_epoch,
        }
    )
    # The source envelope may carry a Core-provided head snapshot.  Keep the
    # plugin a proposer: this data is still re-authorized by Host/Core before
    # any commit and is never treated as a publication decision.
    head.setdefault("dataset_id", dataset_id)
    head.setdefault("record_id", record_id)
    head.setdefault("permission_epoch", permission_epoch)
    proposal_id = stable_id("proposal", dataset_id, record_id, content_digest, reason)
    return {
        "proposal_id": proposal_id,
        "proposal_revision": proposal_revision,
        "owner": owner,
        "operation": operation,
        "base_head": head,
        "proposed_version": ref,
        "source_refs": list(source_refs),
        "extractor_version": extractor_version,
        "reason": reason,
        "state": "pending",
    }


def empty_proposal_batch(context: Mapping[str, Any], watermark: str) -> dict[str, Any]:
    return {
        "request_id": str(context["request_id"]),
        "proposals": [],
        "source_watermark": watermark,
    }


def lifecycle_result(
    context: Mapping[str, Any], operation: str, checkpoint_ref: Any
) -> dict[str, Any]:
    request_id = str(context["request_id"])
    if operation in {"negotiate", "health"}:
        state = "ready"
    elif operation == "cancel":
        state = "cancelled"
    elif operation == "checkpoint":
        state = "checkpointed"
    elif operation == "restore":
        state = "restored" if checkpoint_ref else "failed"
    else:
        state = "unsupported"
    return {
        "request_id": request_id,
        "sdk_version": "operant-memory-sdk.v1",
        "state": state,
        "checkpoint_ref": checkpoint_ref,
    }


def package_digest(root: Path, *, max_files: int = 4_096, max_bytes: int = 100_000_000) -> str:
    """Mirror the Host package identity algorithm without external imports."""

    root = Path(root)
    root_stat = root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("plugin package must be a real directory")
    digest = hashlib.sha256()
    count = 0
    total = 0
    entries = sorted(
        (path for path in root.rglob("*") if path != root),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in entries:
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode):
            raise ValueError("plugin package cannot contain symlinks")
        if stat.S_ISDIR(item.st_mode):
            continue
        if not stat.S_ISREG(item.st_mode):
            raise ValueError("plugin package contains a non-regular file")
        data = path.read_bytes()
        count += 1
        total += len(data)
        if count > max_files or total > max_bytes:
            raise ValueError("plugin package exceeds the configured limit")
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def build_manifest(
    package_root: Path,
    *,
    plugin_id: str,
    config_schema_ref: str,
    state_schema_version: str,
    plugin_version: str = "1.0.0",
    capabilities: tuple[str, ...] = ("extract", "recall", "maintain", "on_index_event"),
) -> dict[str, Any]:
    package_root = Path(package_root)

    def _digest(filename: str) -> str:
        return hashlib.sha256((package_root / filename).read_bytes()).hexdigest()

    return {
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "sdk_version": "operant-memory-sdk.v1",
        "host_api_versions": ["operant-memory-sdk.v1"],
        "package_digest": package_digest(package_root),
        "dependencies_digest": _digest("dependencies.json"),
        "permissions_digest": _digest("permissions.json"),
        "entrypoint": "plugin.py",
        "config_schema_ref": config_schema_ref,
        "config_schema_digest": _digest("config.schema.json"),
        "state_schema_version": state_schema_version,
        "capabilities": list(capabilities),
        "memory_mb": 128,
        "max_rpc_bytes": 262_144,
        "max_concurrency": 2,
        "export_supported": True,
        "import_supported": True,
        "recoverable": True,
    }


JsonRpcHostClient = StdioHostClient
HostClient = StdioHostClient
serve = serve_stdio


__all__ = [
    "HostClient",
    "JsonRpcError",
    "JsonRpcHostClient",
    "StdioHostClient",
    "canonical_json",
    "context_of",
    "decode_frame",
    "digest_text",
    "empty_proposal_batch",
    "encode_frame",
    "lifecycle_result",
    "normalize_text",
    "build_manifest",
    "package_digest",
    "proposal_for",
    "serve_stdio",
    "serve",
    "stable_id",
]
