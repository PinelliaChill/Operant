"""Explicit, digest-bound lifecycle for bundled local capability adapters.

Only Core-shipped adapters can be installed here. An untrusted Python package
is never imported from the registry, and a source update requires reinstall.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from operant.remote.local_browser import BrowserTargetPolicy
from operant.remote.local_computer import ComputerTargetError, ComputerTargetPolicy
from operant.remote.local_worker import BROWSER_PLUGIN, COMPUTER_PLUGIN, LocalCapabilityPlugin


class CapabilityPluginRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: Literal["operant.chrome.browser", "operant.macos.computer"]
    version: Literal["phase56.v1"]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_targets: tuple[str, ...] = Field(min_length=1, max_length=100)
    state: Literal["disabled", "enabled"] = "disabled"
    installed_at: AwareDatetime
    # Old v1 registry records predate this field. Their first real state
    # transition receives a fresh generation and invalidates old bindings.
    generation: str = Field(default="legacy", pattern=r"^(?:legacy|[0-9a-f]{32})$")


_PLUGIN_BY_ID: dict[str, LocalCapabilityPlugin] = {
    BROWSER_PLUGIN.plugin_id: BROWSER_PLUGIN,
    COMPUTER_PLUGIN.plugin_id: COMPUTER_PLUGIN,
}
_ADAPTER_FILE: dict[str, str] = {
    BROWSER_PLUGIN.plugin_id: "local_browser.py",
    COMPUTER_PLUGIN.plugin_id: "local_computer.py",
}
_TARGET_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}$")


def _source_digest(plugin_id: str) -> str:
    if plugin_id not in _PLUGIN_BY_ID:
        raise ValueError("unknown bundled capability plugin")
    remote_root = Path(__file__).resolve().parents[1] / "remote"
    digest = hashlib.sha256()
    files = [
        "local_worker.py",
        "operator.py",
        "tool_extensions.py",
        "sealed_input.py",
        _ADAPTER_FILE[plugin_id],
    ]
    if plugin_id == BROWSER_PLUGIN.plugin_id:
        files.append("browser_proxy.py")
    for filename in files:
        content = (remote_root / filename).read_bytes()
        digest.update(filename.encode())
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


class CapabilityPluginRegistry:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise ValueError("capability plugin root must be an absolute directory, not a symlink")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("capability plugin root is not private")
        self.root = root
        self.state_path = root / "registry.json"
        self.lock_path = root / "registry.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, CapabilityPluginRecord]:
        if self.state_path.is_symlink():
            raise ValueError("capability plugin registry is a symlink")
        if not self.state_path.exists():
            return {}
        raw = json.loads(self.state_path.read_text())
        if not isinstance(raw, dict) or raw.get("schema") != "operant-capability-registry.v1":
            raise ValueError("capability plugin registry version is unsupported")
        records = raw.get("plugins")
        if not isinstance(records, dict):
            raise ValueError("capability plugin registry is malformed")
        result = {
            key: CapabilityPluginRecord.model_validate(value) for key, value in records.items()
        }
        if any(key != record.plugin_id for key, record in result.items()):
            raise ValueError("capability plugin registry identity mismatch")
        return result

    def _write(self, records: dict[str, CapabilityPluginRecord]) -> None:
        payload = {
            "schema": "operant-capability-registry.v1",
            "plugins": {
                key: value.model_dump(mode="json") for key, value in sorted(records.items())
            },
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".registry-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def install(self, plugin_id: str, allowed_targets: tuple[str, ...]) -> CapabilityPluginRecord:
        plugin = _PLUGIN_BY_ID.get(plugin_id)
        if plugin is None:
            raise ValueError("only bundled capability plugins can be installed")
        targets = tuple(dict.fromkeys(item.strip() for item in allowed_targets))
        if not targets or len(targets) > 100:
            raise ValueError("capability plugin target allowlist is invalid")
        if plugin_id == BROWSER_PLUGIN.plugin_id:
            targets = tuple(sorted(BrowserTargetPolicy(frozenset(targets)).allowed_origins))
        else:
            if any(not _TARGET_PATTERN.fullmatch(item) for item in targets):
                raise ValueError("capability plugin target allowlist is invalid")
            try:
                ComputerTargetPolicy(frozenset(targets))
            except ComputerTargetError as exc:
                raise ValueError(str(exc)) from exc
        record = CapabilityPluginRecord(
            plugin_id=plugin.plugin_id,
            version=plugin.protocol_version,
            source_digest=_source_digest(plugin_id),
            allowed_targets=targets,
            installed_at=datetime.now(timezone.utc),
            generation=uuid4().hex,
        )
        with self._locked():
            records = self._read()
            if plugin_id in records and records[plugin_id].state == "enabled":
                raise ValueError("disable the capability plugin before reinstalling")
            records[plugin_id] = record
            self._write(records)
        return record

    def list(self) -> tuple[CapabilityPluginRecord, ...]:
        with self._locked():
            return tuple(self._read().values())

    def get(self, plugin_id: str, *, require_enabled: bool = False) -> CapabilityPluginRecord:
        with self._locked():
            record = self._read().get(plugin_id)
        if record is None:
            raise KeyError(plugin_id)
        if record.source_digest != _source_digest(plugin_id):
            raise ValueError("bundled capability plugin source changed; reinstall is required")
        if require_enabled and record.state != "enabled":
            raise PermissionError("capability plugin is disabled")
        return record

    def set_enabled(self, plugin_id: str, enabled: bool) -> CapabilityPluginRecord:
        with self._locked():
            records = self._read()
            record = records.get(plugin_id)
            if record is None:
                raise KeyError(plugin_id)
            if enabled and record.source_digest != _source_digest(plugin_id):
                raise ValueError("bundled capability plugin source changed; reinstall is required")
            state = "enabled" if enabled else "disabled"
            updated = (
                record.model_copy(update={"state": state, "generation": uuid4().hex})
                if record.state != state
                else record
            )
            records[plugin_id] = updated
            self._write(records)
            return updated

    def uninstall(self, plugin_id: str) -> None:
        with self._locked():
            records = self._read()
            record = records.get(plugin_id)
            if record is None:
                raise KeyError(plugin_id)
            if record.state == "enabled":
                raise ValueError("disable the capability plugin before uninstalling")
            del records[plugin_id]
            self._write(records)
