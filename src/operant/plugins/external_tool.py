"""Isolated, digest-bound third-party Tool packages.

Only a small JSON/stdio tool protocol is admitted. Packages are copied into
private Host storage and never imported into Core. Sandbox admission is
mandatory; platforms without a proven isolator fail closed.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from operant.domain.messages import ToolDefinition
from operant.domain.models import ToolPolicy
from operant.domain.security import Capability
from operant.plugins.protocol import SandboxEvidence, SandboxProbe
from operant.protocol import redact_public_data
from operant.tools.extensions import EXTENSION_TOOL_NAME, ToolExtension

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9_]{2,48}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_ARGUMENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_MANIFEST = 16_384
_MAX_SCRIPT = 1_000_000
_MAX_REQUEST = 32_768
_MAX_RESPONSE = 65_536
ExtensionCategory = Literal["tool", "command", "event", "provider", "runtime", "capability_driver"]
_CATEGORY_FIELDS: dict[ExtensionCategory, str] = {
    "tool": "tools",
    "command": "commands",
    "event": "events",
    "provider": "providers",
    "runtime": "runtimes",
    "capability_driver": "capability_drivers",
}
_RUNNER = """import resource, runpy, sys
def cap(kind, limit):
    soft, hard = resource.getrlimit(kind)
    values = [limit]
    if soft != resource.RLIM_INFINITY: values.append(soft)
    if hard != resource.RLIM_INFINITY: values.append(hard)
    resource.setrlimit(kind, (min(values), hard))
cap(resource.RLIMIT_CPU, 5)
cap(resource.RLIMIT_FSIZE, 128 * 1024)
cap(resource.RLIMIT_NOFILE, 64)
runpy.run_path(sys.argv[1], run_name='__main__')
"""


class ExternalToolManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str
    version: str
    host_api_version: Literal["operant-tool-extension.v1", "operant-local-extension.v1"]
    tools: tuple[ToolDefinition, ...] = Field(default=(), max_length=16)
    commands: tuple[ToolDefinition, ...] = Field(default=(), max_length=16)
    events: tuple[ToolDefinition, ...] = Field(default=(), max_length=16)
    providers: tuple[ToolDefinition, ...] = Field(default=(), max_length=8)
    runtimes: tuple[ToolDefinition, ...] = Field(default=(), max_length=8)
    capability_drivers: tuple[ToolDefinition, ...] = Field(default=(), max_length=8)

    def operations(self, category: ExtensionCategory) -> tuple[ToolDefinition, ...]:
        return cast(tuple[ToolDefinition, ...], getattr(self, _CATEGORY_FIELDS[category]))

    @model_validator(mode="after")
    def validate_package(self) -> ExternalToolManifest:
        if not _PLUGIN_ID.fullmatch(self.plugin_id) or not _VERSION.fullmatch(self.version):
            raise ValueError("external Tool identity or version is invalid")
        if self.host_api_version == "operant-tool-extension.v1":
            if not self.tools or any(
                self.operations(category) for category in _CATEGORY_FIELDS if category != "tool"
            ):
                raise ValueError("legacy external Tool package only supports tools")
        elif not any(self.operations(category) for category in _CATEGORY_FIELDS):
            raise ValueError("local extension must export at least one operation")
        definitions = [
            (category, definition)
            for category in _CATEGORY_FIELDS
            for definition in self.operations(category)
        ]
        names = [definition.name for _, definition in definitions]
        if len(names) != len(set(names)):
            raise ValueError("external extension operation names must be unique")
        for category, tool in definitions:
            if (
                not EXTENSION_TOOL_NAME.fullmatch(tool.name)
                or len(tool.name) > 85
                or not tool.name.startswith(f"ext_{self.plugin_id}_")
                or tool.name.startswith(("ext_browser_", "ext_computer_"))
            ):
                raise ValueError("external extension name must use its package namespace")
            if category == "event" and set(tool.parameters.get("properties", {})) != {
                "payload_json"
            }:
                raise ValueError("event adapter accepts only a bounded payload_json")
            if category == "provider" and set(tool.parameters.get("properties", {})) != {
                "payload_json"
            }:
                raise ValueError("provider adapter accepts only a bounded payload_json")
            if category == "runtime" and set(tool.parameters.get("properties", {})) != {
                "payload_json"
            }:
                raise ValueError("runtime adapter accepts only a bounded payload_json")
            if not 1 <= len(tool.description) <= 240 or any(
                ord(character) < 32 for character in tool.description
            ):
                raise ValueError("external Tool description must be one short line")
            schema = tool.parameters
            properties = schema.get("properties")
            required = schema.get("required")
            if (
                set(schema) != {"type", "properties", "required", "additionalProperties"}
                or schema.get("type") != "object"
                or schema.get("additionalProperties") is not False
                or not isinstance(properties, dict)
                or len(properties) > 20
                or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(required) != len(set(required))
                or any(key not in properties for key in required)
                or any(
                    not isinstance(key, str)
                    or not _ARGUMENT_NAME.fullmatch(key)
                    or not isinstance(value, dict)
                    or set(value) != {"type"}
                    or value["type"] not in {"string", "integer", "number", "boolean"}
                    for key, value in properties.items()
                )
            ):
                raise ValueError("external Tool schema must be a bounded flat object")
        return self


class ExternalToolRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: ExternalToolManifest
    installation_id: str = Field(
        default_factory=lambda: uuid4().hex[:12], pattern=r"^[0-9a-f]{12}$"
    )
    package_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["disabled", "enabled"] = "disabled"
    installed_at: AwareDatetime
    sandbox_evidence_ref: str | None = None
    granted_categories: tuple[ExtensionCategory, ...] = ()

    def granted_name(self, tool_name: str) -> str:
        return f"{tool_name}__{self.installation_id}"


def _regular_bytes(path: Path, maximum: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("external Tool package contains an invalid file") from exc
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("external Tool package contains an invalid file")
        content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("external Tool package file is too large")
        return content


def _digest(manifest: bytes, script: bytes) -> str:
    value = hashlib.sha256()
    for name, content in (
        (b"manifest.json", manifest),
        (b"plugin.py", script),
        (b"runner.py", _RUNNER.encode()),
    ):
        value.update(name)
        value.update(len(content).to_bytes(8, "big"))
        value.update(content)
    return value.hexdigest()


def _validate_arguments(
    definition: ToolDefinition, arguments: dict[str, Any], *, max_string: int = 4_000
) -> None:
    schema = definition.parameters
    properties = schema["properties"]
    required = schema["required"]
    if not set(required).issubset(arguments) or set(arguments) - set(properties):
        raise ValueError("external Tool arguments do not match its schema")
    for key, value in arguments.items():
        kind = properties[key]["type"]
        if kind == "string":
            valid = isinstance(value, str) and len(value) <= max_string
        elif kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        else:
            valid = isinstance(value, bool)
        if not valid:
            raise ValueError("external Tool argument type is invalid")


class ExternalToolRegistry:
    def __init__(self, root: Path, *, sandbox_probe: SandboxProbe | None = None) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise ValueError("external Tool root must be an absolute directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("external Tool root is not private")
        self.root = root
        self.packages = root / "packages"
        self.packages.mkdir(mode=0o700, exist_ok=True)
        package_info = self.packages.stat()
        if (
            self.packages.is_symlink()
            or package_info.st_uid != os.getuid()
            or package_info.st_mode & 0o077
        ):
            raise PermissionError("external Tool package directory is not private")
        self.state_path = root / "registry.json"
        self.lock_path = root / "registry.lock"
        self.sandbox_probe = sandbox_probe or SandboxProbe()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, ExternalToolRecord]:
        if self.state_path.is_symlink():
            raise ValueError("external Tool registry is a symlink")
        if not self.state_path.exists():
            return {}
        payload = json.loads(self.state_path.read_text())
        if not isinstance(payload, dict) or payload.get("schema") != "operant-external-tools.v1":
            raise ValueError("external Tool registry version is unsupported")
        records = payload.get("plugins")
        if not isinstance(records, dict):
            raise ValueError("external Tool registry is malformed")
        result = {key: ExternalToolRecord.model_validate(value) for key, value in records.items()}
        if any(key != value.manifest.plugin_id for key, value in result.items()):
            raise ValueError("external Tool registry identity mismatch")
        return result

    def _write(self, records: dict[str, ExternalToolRecord]) -> None:
        payload = {
            "schema": "operant-external-tools.v1",
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

    def _package_root(self, plugin_id: str) -> Path:
        if not _PLUGIN_ID.fullmatch(plugin_id):
            raise ValueError("external Tool identity is invalid")
        return self.packages / plugin_id

    def _verify(self, record: ExternalToolRecord) -> Path:
        package = self._package_root(record.manifest.plugin_id)
        if (
            package.is_symlink()
            or not package.is_dir()
            or package.stat().st_uid != os.getuid()
            or package.stat().st_mode & 0o077
        ):
            raise PermissionError("external Tool package is unavailable")
        if {item.name for item in package.iterdir()} != {
            "manifest.json",
            "plugin.py",
            "runner.py",
        }:
            raise PermissionError("external Tool package contains unbound files")
        manifest = _regular_bytes(package / "manifest.json", _MAX_MANIFEST)
        script = _regular_bytes(package / "plugin.py", _MAX_SCRIPT)
        runner = _regular_bytes(package / "runner.py", len(_RUNNER.encode()))
        if runner != _RUNNER.encode() or _digest(manifest, script) != record.package_digest:
            raise PermissionError("external Tool package source changed since installation")
        if ExternalToolManifest.model_validate_json(manifest) != record.manifest:
            raise PermissionError("external Tool manifest changed since installation")
        return package

    @staticmethod
    def inspect(source: Path) -> tuple[ExternalToolManifest, str]:
        source = source.expanduser().resolve(strict=True)
        manifest_bytes = _regular_bytes(source / "manifest.json", _MAX_MANIFEST)
        script_bytes = _regular_bytes(source / "plugin.py", _MAX_SCRIPT)
        manifest = ExternalToolManifest.model_validate_json(manifest_bytes)
        return manifest, _digest(manifest_bytes, script_bytes)

    def install(self, source: Path, *, expected_digest: str | None = None) -> ExternalToolRecord:
        source = source.expanduser().resolve(strict=True)
        manifest_bytes = _regular_bytes(source / "manifest.json", _MAX_MANIFEST)
        script_bytes = _regular_bytes(source / "plugin.py", _MAX_SCRIPT)
        manifest = ExternalToolManifest.model_validate_json(manifest_bytes)
        package_digest = _digest(manifest_bytes, script_bytes)
        if expected_digest is not None and expected_digest != package_digest:
            raise ValueError("external Tool package digest does not match the expected value")
        record = ExternalToolRecord(
            manifest=manifest,
            package_digest=package_digest,
            installed_at=datetime.now(timezone.utc),
        )
        with self._locked():
            records = self._read()
            if manifest.plugin_id in records:
                raise ValueError("uninstall the previous external Tool package first")
            installed_names = {
                tool.name for item in records.values() for tool in item.manifest.tools
            }
            if installed_names.intersection(tool.name for tool in manifest.tools):
                raise ValueError("external Tool name conflicts with another installation")
            temporary = Path(tempfile.mkdtemp(prefix=".package-", dir=self.packages))
            package = self._package_root(manifest.plugin_id)
            promoted = False
            try:
                for name, content in (
                    ("manifest.json", manifest_bytes),
                    ("plugin.py", script_bytes),
                    ("runner.py", _RUNNER.encode()),
                ):
                    target = temporary / name
                    target.write_bytes(content)
                    target.chmod(0o600)
                os.replace(temporary, package)
                promoted = True
                records[manifest.plugin_id] = record
                self._write(records)
            except Exception:
                if temporary.exists():
                    shutil.rmtree(temporary)
                if promoted and package.exists() and manifest.plugin_id not in self._read():
                    shutil.rmtree(package)
                raise
        return record

    def list(self) -> tuple[ExternalToolRecord, ...]:
        with self._locked():
            return tuple(self._read().values())

    def get(self, plugin_id: str, *, require_enabled: bool = False) -> ExternalToolRecord:
        with self._locked():
            record = self._read()[plugin_id]
            self._verify(record)
            if require_enabled and record.state != "enabled":
                raise PermissionError("external Tool package is disabled")
            return record

    def set_enabled(
        self,
        plugin_id: str,
        enabled: bool,
        *,
        granted_categories: tuple[ExtensionCategory, ...] | None = None,
    ) -> ExternalToolRecord:
        with self._locked():
            records = self._read()
            record = records[plugin_id]
            package = self._verify(record)
            evidence_ref = record.sandbox_evidence_ref
            grants = record.granted_categories
            if enabled:
                if granted_categories is None:
                    grants = (
                        ("tool",)
                        if record.manifest.host_api_version == "operant-tool-extension.v1"
                        else ()
                    )
                else:
                    if len(granted_categories) != len(set(granted_categories)) or any(
                        category not in _CATEGORY_FIELDS or not record.manifest.operations(category)
                        for category in granted_categories
                    ):
                        raise ValueError("extension grants must name exported categories once")
                    grants = granted_categories
                if not grants:
                    raise ValueError("enable requires an explicit exported category grant")
                evidence = self._probe(package, record.installation_id)
                evidence_ref = evidence.evidence_ref
            else:
                grants = ()
            updated = record.model_copy(
                update={
                    "state": "enabled" if enabled else "disabled",
                    "sandbox_evidence_ref": evidence_ref,
                    "granted_categories": grants,
                }
            )
            records[plugin_id] = updated
            self._write(records)
            return updated

    def uninstall(self, plugin_id: str) -> None:
        with self._locked():
            records = self._read()
            record = records[plugin_id]
            if record.state != "disabled":
                raise ValueError("disable the external Tool package before uninstalling")
            package = self._verify(record)
            del records[plugin_id]
            self._write(records)
            shutil.rmtree(package)

    def _managed_roots(self, package: Path, installation_id: str) -> tuple[Path, Path, Path, Path]:
        if not re.fullmatch(r"[0-9a-f]{12}", installation_id):
            raise ValueError("external Tool installation identity is invalid")
        storage_name = f"{package.name}-{installation_id}"
        result = tuple(self.root / name / storage_name for name in ("data", "state", "logs", "tmp"))
        for path in result:
            for directory in (path.parent, path):
                directory.mkdir(mode=0o700, exist_ok=True)
                info = directory.stat()
                if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise PermissionError("external Tool managed data directory is not private")
        return result  # type: ignore[return-value]

    def _probe(self, package: Path, installation_id: str) -> SandboxEvidence:
        data, state, logs, temporary = self._managed_roots(package, installation_id)
        return self.sandbox_probe.check(
            package_root=package,
            data_root=data,
            state_root=state,
            logs_root=logs,
            tmp_root=temporary,
            executable=Path(sys.executable),
        )

    def run(
        self,
        plugin_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        installation_id: str,
        category: ExtensionCategory = "tool",
    ) -> dict[str, Any]:
        with self._locked():
            record = self._read()[plugin_id]
            if record.installation_id != installation_id:
                raise PermissionError("external Tool installation changed; grant again")
            package = self._verify(record)
            if record.state != "enabled":
                raise PermissionError("external Tool package is disabled")
            if category not in record.granted_categories and not (
                category == "tool"
                and record.manifest.host_api_version == "operant-tool-extension.v1"
                and not record.granted_categories
            ):
                raise PermissionError("extension category is not granted")
            definition = next(
                (tool for tool in record.manifest.operations(category) if tool.name == tool_name),
                None,
            )
            if definition is None:
                raise ValueError("external Tool is not exported by this package")
            _validate_arguments(
                definition,
                arguments,
                max_string=24_000 if category in {"provider", "runtime"} else 4_000,
            )
            payload = json.dumps(
                {
                    "host_api_version": record.manifest.host_api_version,
                    "tool": tool_name,
                    **(
                        {"category": category}
                        if record.manifest.host_api_version == "operant-local-extension.v1"
                        else {}
                    ),
                    "arguments": arguments,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            if len(payload) > _MAX_REQUEST:
                raise ValueError("external Tool request is too large")
            evidence = self._probe(package, record.installation_id)
            _, _, _, temporary = self._managed_roots(package, record.installation_id)
            with (
                tempfile.TemporaryFile(dir=temporary) as output,
                tempfile.TemporaryFile(dir=temporary) as errors,
            ):
                process = subprocess.Popen(
                    [
                        evidence.runner,
                        "-p",
                        evidence.profile,
                        str(Path(sys.executable).resolve()),
                        "-I",
                        "-S",
                        str(package / "runner.py"),
                        str(package / "plugin.py"),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=errors,
                    cwd=package,
                    start_new_session=True,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "TMPDIR": str(temporary),
                    },
                )
                with ThreadPoolExecutor(max_workers=1) as executor:
                    completed = executor.submit(process.communicate, input=payload)
                    deadline = time.monotonic() + 10
                    while not completed.done():
                        if time.monotonic() >= deadline:
                            self._kill_group(process)
                            completed.result(timeout=3)
                            raise RuntimeError("external Tool timed out without a known result")
                        if process.poll() is None:
                            try:
                                usage = subprocess.run(
                                    ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                                    capture_output=True,
                                    text=True,
                                    timeout=2,
                                    check=False,
                                )
                                if usage.returncode != 0 or not usage.stdout.strip():
                                    if process.poll() is not None:
                                        break
                                    raise ValueError("resource monitor returned no live process")
                                rss_kb = int(usage.stdout.strip())
                            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                                self._kill_group(process)
                                completed.result(timeout=3)
                                raise RuntimeError("external Tool resource monitor failed") from exc
                            if rss_kb > 256 * 1024:
                                self._kill_group(process)
                                completed.result(timeout=3)
                                raise RuntimeError("external Tool exceeded memory budget")
                        time.sleep(0.1)
                    completed.result()
                if process.returncode != 0:
                    errors.seek(0)
                    diagnostic = errors.read(4_096)
                    categories = (
                        b"MemoryError",
                        b"ModuleNotFoundError",
                        b"PermissionError",
                        b"Operation not permitted",
                        b"can't open file",
                    )
                    error_category = next(
                        (item.decode() for item in categories if item in diagnostic), "unknown"
                    )
                    if error_category == "unknown":
                        exception = re.search(rb"(?m)^([A-Za-z][A-Za-z0-9]*Error):", diagnostic)
                        if exception is not None:
                            error_category = exception.group(1).decode("ascii")
                    if error_category == "ValueError":
                        limit = re.search(rb"RLIMIT_[A-Z]+", diagnostic)
                        if limit is not None:
                            error_category = f"ValueError:{limit.group().decode('ascii')}"
                    raise RuntimeError(f"isolated external Tool failed ({error_category})")
                output.seek(0)
                response = output.read(_MAX_RESPONSE + 1)
            if len(response) > _MAX_RESPONSE:
                raise RuntimeError("external Tool result is too large; outcome is unknown")
            try:
                result = json.loads(
                    response,
                    parse_constant=lambda _: (_ for _ in ()).throw(
                        ValueError("external Tool returned a non-finite value")
                    ),
                )
            except (ValueError, RecursionError) as exc:
                raise RuntimeError("external Tool result is invalid; outcome is unknown") from exc
            if (
                not isinstance(result, dict)
                or set(result) != {"result"}
                or not isinstance(result["result"], dict)
            ):
                raise RuntimeError("external Tool result is invalid; outcome is unknown")
            return result["result"]

    @staticmethod
    def _kill_group(process: subprocess.Popen[bytes]) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def installed_external_tool_extensions(
    database_path: Path, policy: ToolPolicy
) -> dict[str, ToolExtension]:
    requested = {name for name in policy.allowed_tools if EXTENSION_TOOL_NAME.fullmatch(name)}
    if not requested:
        return {}
    root = database_path.expanduser().resolve().parent / "external-tools"
    if not root.exists():
        return {}
    registry = ExternalToolRegistry(root)
    result: dict[str, ToolExtension] = {}
    for record in registry.list():
        if record.state != "enabled" or (
            "tool" not in record.granted_categories
            and record.manifest.host_api_version != "operant-tool-extension.v1"
        ):
            continue
        if not requested.intersection(
            record.granted_name(tool.name) for tool in record.manifest.tools
        ):
            continue
        registry.get(record.manifest.plugin_id, require_enabled=True)
        for definition in record.manifest.tools:
            granted_name = record.granted_name(definition.name)
            if granted_name not in requested:
                continue

            async def execute(
                arguments: dict[str, Any],
                *,
                plugin_id: str = record.manifest.plugin_id,
                name: str = definition.name,
                installation_id: str = record.installation_id,
            ) -> dict[str, Any]:
                value = await asyncio.to_thread(
                    registry.run,
                    plugin_id,
                    name,
                    arguments,
                    installation_id=installation_id,
                )
                return {
                    "plugin_id": plugin_id,
                    "trust": "untrusted",
                    "result": redact_public_data(value, max_chars=_MAX_RESPONSE),
                }

            result[granted_name] = ToolExtension(
                plugin_id=record.manifest.plugin_id,
                plugin_version=f"{record.manifest.version}+{record.installation_id}",
                host_api_version="operant-tool-extension.v1",
                definition=definition.model_copy(
                    update={
                        "description": (
                            f"Untrusted third-party package {record.manifest.plugin_id}: "
                            f"{definition.description}"
                        ),
                        "name": granted_name,
                    }
                ),
                capabilities=(Capability.PROCESS_EXEC_NO_NETWORK,),
                side_effecting=True,
                execute=execute,
                approval_category="external_plugin",
            )
    return result
