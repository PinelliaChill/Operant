"""Host-owned installation, binding, certification and resource registry.

The registry is deliberately private to the first MP-1 implementation.  It
stores no secrets and never scans a directory looking for packages: installing
or reinstalling is always an explicit call.  A small JSON journal is used so
that cleanup plans and retained datasets survive a Host restart; the Core can
replace this repository with its transaction-backed implementation later.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl; isolated Host is unsupported there.
    fcntl = None  # type: ignore[assignment]

from operant.contracts.b2_1 import (
    Certification,
    CleanupItem,
    DatasetOwner,
    PluginConfig,
    PluginManifest,
    PrivateIndexResource,
    Resource,
    RpcContext,
    Scope,
)
from operant.domain.models import new_id, utc_now
from operant.plugins.protocol import (
    PackageUnavailableError,
    PermissionDeniedError,
    PluginError,
    compute_package_digest,
    copy_package,
    ensure_managed_resource,
    path_under,
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


class PluginLifecycleState(str, Enum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    DISABLING = "disabling"
    UNINSTALLING = "uninstalling"
    UNINSTALLED = "uninstalled"
    FAILED = "failed"
    RESTART_REQUIRED = "restart_required"
    BLOCKED = "blocked"


class RunLease(BaseModel):
    """Host-owned run dependency and fencing token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str = Field(min_length=1, max_length=300)
    run_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    installation_id: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=200)
    scope: Scope
    binding_epoch: int = Field(ge=0, le=2**53 - 1)
    permission_epoch: int = Field(ge=0, le=2**53 - 1)
    lease_fencing: int = Field(ge=1, le=2**53 - 1)
    expires_at: AwareDatetime
    status: Literal["active", "cancelled", "completed", "expired"] = "active"


class InstallationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: str = Field(min_length=1, max_length=200)
    manifest: PluginManifest
    package_relative: str
    dataset_id: str = Field(min_length=1, max_length=200)
    owner: DatasetOwner
    state: Literal[
        "disabled",
        "enabled",
        "disabling",
        "uninstalling",
        "uninstalled",
        "failed",
        "restart_required",
        "blocked",
    ]
    binding_id: str | None = None
    config: PluginConfig | None = None
    certification_id: str | None = None
    binding_epoch: int = Field(default=0, ge=0, le=2**53 - 1)
    permission_epoch: int = Field(default=0, ge=0, le=2**53 - 1)
    inventory_revision: int = Field(default=0, ge=0, le=2**53 - 1)
    last_error_code: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class BindingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str = Field(min_length=1, max_length=200)
    installation_id: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=200)
    config: PluginConfig
    enabled: bool = False
    global_enabled: bool = True
    binding_epoch: int = Field(default=0, ge=0, le=2**53 - 1)
    permission_epoch: int = Field(default=0, ge=0, le=2**53 - 1)
    active_run_ids: tuple[str, ...] = ()
    updated_at: AwareDatetime


class StoredResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: Resource
    relative_path: str
    path_kind: Literal["file", "directory"]
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    status: Literal["active", "retained", "deleted"] = "active"


class CleanupPlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    installation_id: str
    data_policy: Literal["keep", "delete"]
    inventory_revision: int = Field(ge=0, le=2**53 - 1)
    expected_binding_epoch: int = Field(ge=0, le=2**53 - 1)
    items: tuple[CleanupItem, ...]
    state: Literal["pending", "completed", "blocked"] = "pending"
    updated_at: AwareDatetime


class DatasetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    owner: DatasetOwner
    installation_id: str | None
    state: Literal["bound", "retained", "deleting", "deleted", "blocked"]
    revision: int = Field(default=0, ge=0, le=2**53 - 1)


class RegistryState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installations: tuple[InstallationRecord, ...] = ()
    bindings: tuple[BindingRecord, ...] = ()
    certifications: tuple[Certification, ...] = ()
    resources: tuple[StoredResource, ...] = ()
    runs: tuple[RunLease, ...] = ()
    cleanup_plans: tuple[CleanupPlanRecord, ...] = ()
    datasets: tuple[DatasetRecord, ...] = ()
    revoked_epochs: dict[str, int] = {}


def _validate_id(value: str, label: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


class PluginRegistry:
    """Explicit-install registry with a recoverable private journal."""

    _KEEP_CATEGORIES = frozenset(
        {"record", "version", "proposal", "relation", "observation", "manifest", "config", "state"}
    )
    _RESOURCE_CATEGORIES = frozenset(
        {
            "package",
            "config",
            "state",
            "index",
            "cache",
            "tmp",
            "log",
            "record",
            "version",
            "proposal",
            "relation",
            "observation",
            "manifest",
            "job",
            "subscription",
        }
    )

    def __init__(
        self,
        managed_root: Path,
        *,
        trusted_issuers: Iterable[str] = (),
        state_path: Path | None = None,
    ) -> None:
        root = Path(managed_root)
        if not root.is_absolute():
            raise ValueError("PluginHost managed_root must be absolute")
        if root.exists() and root.is_symlink():
            raise ValueError("PluginHost managed_root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError("PluginHost managed_root must be a directory")
        self.managed_root = root.resolve()
        self.state_path = Path(state_path or self.managed_root / "registry.json")
        if not self.state_path.is_absolute():
            raise ValueError("registry state path must be absolute")
        try:
            self.state_path.resolve().relative_to(self.managed_root)
        except ValueError as exc:
            raise ValueError("registry state path must stay inside managed_root") from exc
        if fcntl is None:
            raise PluginError("unsupported", "single-writer PluginHost locking is unavailable")
        self._lock_handle = None
        lock_path = self.state_path.with_name(f".{self.state_path.name}.lock")
        try:
            self._lock_handle = lock_path.open("a+", encoding="utf-8")
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            if self._lock_handle is not None:
                self._lock_handle.close()
                self._lock_handle = None
            raise PluginError("revision_conflict", "another PluginHost owns this registry") from exc
        self._trusted_issuers = frozenset(_validate_id(item, "issuer") for item in trusted_issuers)
        self._state = self._load()
        if self._fence_recovered_runs():
            self._save()

    def close(self) -> None:
        """Release the single-writer registry lock."""

        handle = self._lock_handle
        self._lock_handle = None
        if handle is None:
            return
        if fcntl is not None:
            from contextlib import suppress

            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def __del__(self) -> None:  # pragma: no cover - best effort at interpreter shutdown.
        from contextlib import suppress

        with suppress(Exception):
            self.close()

    @property
    def state(self) -> RegistryState:
        return self._state

    def _load(self) -> RegistryState:
        if not self.state_path.exists():
            return RegistryState()
        try:
            if self.state_path.is_symlink() or not self.state_path.is_file():
                raise ValueError("registry state path is unsafe")
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return RegistryState.model_validate(payload)
        except Exception as exc:
            raise PluginError("package_unavailable", "plugin registry state is invalid") from exc

    def _fence_recovered_runs(self) -> bool:
        """Invalidate active leases left by a Host process that may have crashed."""

        active = {item.lease_id: item for item in self._state.runs if item.status == "active"}
        if not active:
            return False
        affected = {item.binding_id for item in active.values()}
        runs = tuple(
            item.model_copy(update={"status": "expired"}) if item.lease_id in active else item
            for item in self._state.runs
        )
        bindings: list[BindingRecord] = []
        for binding in self._state.bindings:
            if binding.binding_id not in affected:
                bindings.append(binding)
                continue
            bindings.append(
                binding.model_copy(
                    update={
                        "active_run_ids": (),
                        "binding_epoch": binding.binding_epoch + 1,
                        "updated_at": utc_now(),
                    }
                )
            )
        installations: list[InstallationRecord] = []
        for installation in self._state.installations:
            recovered_binding = next(
                (item for item in bindings if item.installation_id == installation.installation_id),
                None,
            )
            if (
                recovered_binding is None
                or recovered_binding.binding_epoch == installation.binding_epoch
            ):
                installations.append(installation)
            else:
                installations.append(
                    installation.model_copy(
                        update={
                            "binding_epoch": recovered_binding.binding_epoch,
                            "updated_at": utc_now(),
                        }
                    )
                )
        self._state = self._state.model_copy(
            update={
                "runs": runs,
                "bindings": tuple(bindings),
                "installations": tuple(installations),
            }
        )
        return True

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".registry-", suffix=".tmp", dir=self.state_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        self._state.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except Exception:
            from contextlib import suppress

            with suppress(OSError):
                os.unlink(temporary)
            raise

    def _replace(self, **changes: Any) -> None:
        self._state = self._state.model_copy(update=changes)
        self._save()

    def _installation(self, installation_id: str) -> InstallationRecord:
        for item in self._state.installations:
            if item.installation_id == installation_id:
                return item
        raise PluginError("package_unavailable", "plugin installation is not registered")

    def _binding(self, binding_id: str) -> BindingRecord:
        for item in self._state.bindings:
            if item.binding_id == binding_id:
                return item
        raise PluginError("package_unavailable", "plugin binding is not registered")

    def get_installation(self, installation_id: str) -> InstallationRecord:
        return self._installation(installation_id)

    def get_binding(self, binding_id: str) -> BindingRecord:
        return self._binding(binding_id)

    def list_installations(self) -> tuple[InstallationRecord, ...]:
        return self._state.installations

    def list_bindings(self) -> tuple[BindingRecord, ...]:
        return self._state.bindings

    def list_certifications(self) -> tuple[Certification, ...]:
        return self._state.certifications

    def installation_root(self, installation_id: str) -> Path:
        _validate_id(installation_id, "installation_id")
        return path_under(self.managed_root, installation_id)

    def package_path(self, installation_id: str) -> Path:
        return path_under(self.installation_root(installation_id), "package")

    def data_path(self, installation_id: str) -> Path:
        return path_under(self.installation_root(installation_id), "data")

    def state_path_for(self, installation_id: str) -> Path:
        return path_under(self.installation_root(installation_id), "state")

    def logs_path(self, installation_id: str) -> Path:
        return path_under(self.installation_root(installation_id), "logs")

    def tmp_path(self, installation_id: str) -> Path:
        return path_under(self.installation_root(installation_id), "tmp")

    def indexes_path(self, installation_id: str) -> Path:
        return path_under(self.installation_root(installation_id), "indexes")

    def register_certification(self, certification: Certification) -> Certification:
        if certification.issuer_id not in self._trusted_issuers:
            raise PluginError("certification_invalid", "certification issuer is not Host trusted")
        existing = next(
            (
                item
                for item in self._state.certifications
                if item.certification_id == certification.certification_id
            ),
            None,
        )
        if existing is not None and existing != certification:
            raise PluginError("certification_invalid", "certification record changed")
        if existing is None:
            self._replace(certifications=(*self._state.certifications, certification))
        return certification

    def revoke_certification(self, certification_id: str) -> int:
        certification = next(
            (
                item
                for item in self._state.certifications
                if item.certification_id == certification_id
            ),
            None,
        )
        if certification is None:
            raise PluginError("certification_invalid", "certification is not registered")
        current = self._state.revoked_epochs.get(certification_id, certification.revocation_epoch)
        epoch = max(current, certification.revocation_epoch) + 1
        revoked = {
            **self._state.revoked_epochs,
            certification_id: epoch,
        }
        bindings: list[BindingRecord] = []
        installations: list[InstallationRecord] = []
        affected_installations: set[str] = set()
        for binding in self._state.bindings:
            installation = self._installation(binding.installation_id)
            if installation.certification_id != certification_id:
                bindings.append(binding)
                continue
            affected_installations.add(installation.installation_id)
            bindings.append(
                binding.model_copy(
                    update={
                        "binding_epoch": binding.binding_epoch + 1,
                        "permission_epoch": binding.permission_epoch + 1,
                        "enabled": False,
                        "updated_at": utc_now(),
                    }
                )
            )
        for installation in self._state.installations:
            if installation.installation_id in affected_installations:
                installations.append(
                    installation.model_copy(
                        update={
                            "state": PluginLifecycleState.FAILED.value,
                            "binding_epoch": installation.binding_epoch + 1,
                            "permission_epoch": installation.permission_epoch + 1,
                            "last_error_code": "certification_invalid",
                            "updated_at": utc_now(),
                        }
                    )
                )
            else:
                installations.append(installation)
        self._state = self._state.model_copy(
            update={
                "revoked_epochs": revoked,
                "bindings": tuple(bindings),
                "installations": tuple(installations),
            }
        )
        self._save()
        return epoch

    def _certification_for(self, installation: InstallationRecord) -> Certification | None:
        if installation.certification_id is None:
            return None
        return next(
            (
                item
                for item in self._state.certifications
                if item.certification_id == installation.certification_id
            ),
            None,
        )

    def certification_is_current(self, installation: InstallationRecord) -> bool:
        certification = self._certification_for(installation)
        if (
            certification is None
            or certification.state != "valid"
            or certification.issuer_id not in self._trusted_issuers
        ):
            return False
        now = utc_now()
        if now < certification.valid_from or now >= certification.expires_at:
            return False
        return self._state.revoked_epochs.get(
            certification.certification_id, certification.revocation_epoch
        ) <= (certification.revocation_epoch)

    def verify_package(self, installation_id: str) -> str:
        installation = self._installation(installation_id)
        package = self.package_path(installation_id)
        digest = compute_package_digest(package)
        if digest != installation.manifest.package_digest:
            raise PackageUnavailableError("installed package content digest changed")
        return digest

    @staticmethod
    def _actual_metadata_digest(package: Path, filename: str) -> str | None:
        path = package / filename
        try:
            item_stat = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(item_stat.st_mode):
            raise PackageUnavailableError(f"plugin metadata file {filename} is unsafe")
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise PackageUnavailableError(
                f"plugin metadata file {filename} cannot be opened"
            ) from exc
        try:
            return hashlib.sha256(os.read(descriptor, 2_000_000)).hexdigest()
        finally:
            os.close(descriptor)

    def _validate_package_metadata(
        self, package: Path, manifest: PluginManifest, *, required: bool
    ) -> None:
        """Bind certification digests to package files, never to self-reported fields."""

        for filename, expected in (
            ("dependencies.json", manifest.dependencies_digest),
            ("permissions.json", manifest.permissions_digest),
        ):
            actual = self._actual_metadata_digest(package, filename)
            if actual is None:
                if required:
                    raise PluginError(
                        "certification_invalid", f"certified package lacks {filename}"
                    )
                continue
            if actual != expected:
                raise PluginError(
                    "certification_invalid", f"{filename} digest does not match manifest"
                )
            if filename == "dependencies.json":
                try:
                    value = json.loads((package / filename).read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PluginError(
                        "package_unavailable", "dependencies.json is not valid JSON"
                    ) from exc
                if not isinstance(value, dict) or value.get("stdlib_only") is not True:
                    raise PluginError(
                        "package_unavailable",
                        "external runtime dependencies are unsupported by the MP-1 Host",
                    )

    def verify_certification(self, installation_id: str, *, mode: str) -> Certification | None:
        installation = self._installation(installation_id)
        self.verify_package(installation_id)
        certification = self._certification_for(installation)
        if mode == "trusted_in_process":
            if not self.certification_is_current(installation) or certification is None:
                raise PluginError(
                    "certification_invalid",
                    "trusted in-process execution requires current certification",
                )
            if mode not in certification.allowed_modes:
                raise PluginError(
                    "certification_invalid", "certification does not allow in-process execution"
                )
            self._validate_certification_binding(certification, installation.manifest)
            self._validate_package_metadata(
                self.package_path(installation_id), installation.manifest, required=True
            )
            return certification
        if mode != "isolated":
            raise PluginError("certification_invalid", "unknown plugin execution mode")
        # An invalid/revoked certification may run only as an actually isolated
        # package.  A valid certification is still checked for digest binding.
        if certification is not None and self.certification_is_current(installation):
            self._validate_certification_binding(certification, installation.manifest)
            self._validate_package_metadata(
                self.package_path(installation_id), installation.manifest, required=True
            )
        return certification

    @staticmethod
    def _validate_certification_binding(
        certification: Certification, manifest: PluginManifest
    ) -> None:
        if (
            certification.plugin_id != manifest.plugin_id
            or certification.plugin_version != manifest.plugin_version
            or certification.package_digest != manifest.package_digest
            or certification.dependencies_digest != manifest.dependencies_digest
            or certification.permissions_digest != manifest.permissions_digest
        ):
            raise PluginError("certification_invalid", "certification does not bind this package")

    def install(
        self,
        manifest: PluginManifest,
        package: Path,
        *,
        dataset_id: str | None = None,
        principal_id: str = "user",
        certification: Certification | None = None,
        config: PluginConfig | None = None,
        installation_id: str | None = None,
    ) -> InstallationRecord:
        """Explicitly install a package and register its Host-owned resources."""

        source = Path(package)
        if not source.is_absolute():
            raise PackageUnavailableError("plugin package path must be absolute")
        digest = compute_package_digest(source)
        if digest != manifest.package_digest:
            raise PackageUnavailableError("plugin package digest does not match manifest")
        if manifest.sdk_version not in manifest.host_api_versions:
            raise PluginError("protocol_mismatch", "plugin manifest does not support the Host SDK")
        if certification is not None:
            self.register_certification(certification)
            self._validate_certification_binding(certification, manifest)
            self._validate_package_metadata(source, manifest, required=True)
        candidate_id = installation_id or new_id("plugin_installation")
        _validate_id(candidate_id, "installation_id")
        if any(item.installation_id == candidate_id for item in self._state.installations):
            raise PluginError("package_unavailable", "installation_id is already registered")
        dataset = dataset_id or new_id("dataset")
        _validate_id(dataset, "dataset_id")
        existing_dataset = next((d for d in self._state.datasets if d.dataset_id == dataset), None)
        if existing_dataset is not None and (
            existing_dataset.state != "retained"
            or existing_dataset.owner.principal_id != principal_id
        ):
            raise PluginError(
                "unknown_owner", "dataset is not eligible for explicit reinstallation"
            )
        principal = _validate_id(principal_id, "principal_id")
        if config is None:
            config = PluginConfig(
                schema_version="operant-memory-config.v1",
                config_id=new_id("plugin_config"),
                revision=0,
                extraction_model_profile_id=None,
                rerank_model_profile_id=None,
                recall_token_budget=2000,
                maintenance_enabled=False,
                scheduler_definition_id=None,
                secret_refs={},
                effective_at=utc_now(),
            )
        installation_root = self.managed_root / candidate_id
        if installation_root.exists() or installation_root.is_symlink():
            raise PluginError(
                "package_unavailable", "managed installation directory already exists"
            )
        installation_root.mkdir(mode=0o700)
        try:
            copy_package(source, installation_root / "package")
            for directory in ("data", "config", "state", "indexes", "cache", "tmp", "logs"):
                (installation_root / directory).mkdir(mode=0o700)
            if compute_package_digest(installation_root / "package") != digest:
                raise PackageUnavailableError("installed package digest could not be verified")
        except Exception:
            shutil.rmtree(installation_root, ignore_errors=True)
            raise
        owner = DatasetOwner(
            kind="plugin_dataset",
            owner_namespace=f"dataset:{dataset}",
            dataset_id=dataset,
            principal_id=principal,
        )
        now = utc_now()
        record = InstallationRecord(
            installation_id=candidate_id,
            manifest=manifest,
            package_relative=f"{candidate_id}/package",
            dataset_id=dataset,
            owner=owner,
            state=PluginLifecycleState.DISABLED.value,
            binding_id=None,
            config=config,
            certification_id=certification.certification_id if certification else None,
            created_at=now,
            updated_at=now,
        )
        resources = list(self._state.resources)
        for relative, category in (
            ("package", "package"),
            ("data", "state"),
            ("config", "config"),
            ("state", "state"),
            ("indexes", "index"),
            ("cache", "cache"),
            ("tmp", "tmp"),
            ("logs", "log"),
        ):
            resources.append(
                self._make_resource(
                    record, relative, category, path_kind="directory", reconstructible=True
                )
            )
        dataset_record = DatasetRecord(
            dataset_id=dataset,
            owner=owner,
            installation_id=candidate_id,
            state="bound",
            revision=0,
        )
        self._state = self._state.model_copy(
            update={
                "installations": (*self._state.installations, record),
                "resources": tuple(resources),
                "datasets": (
                    *tuple(d for d in self._state.datasets if d.dataset_id != dataset),
                    dataset_record,
                ),
            }
        )
        self._save()
        return record

    def _make_resource(
        self,
        installation: InstallationRecord,
        relative_path: str,
        category: str,
        *,
        path_kind: Literal["file", "directory"],
        reconstructible: bool,
    ) -> StoredResource:
        if category not in self._RESOURCE_CATEGORIES:
            raise PluginError("unsupported", "unsupported Host resource category")
        path_digest = hashlib.sha256(relative_path.encode()).hexdigest()[:20]
        locator = f"resource_{installation.installation_id}_{path_digest}"
        resource = Resource(
            resource_id=locator,
            owner=installation.owner,
            installation_id=installation.installation_id,
            storage="managed_directory",
            category=category,  # type: ignore[arg-type]
            locator_ref=f"managed_{installation.installation_id}_{relative_path.replace('/', '_')}",
            consumer_ids=(installation.installation_id,),
            retention_lock_ids=(),
            reconstructible=reconstructible,
        )
        return StoredResource(resource=resource, relative_path=relative_path, path_kind=path_kind)

    def bind(
        self,
        installation_id: str,
        *,
        dataset_id: str | None = None,
        config: PluginConfig | None = None,
        global_enabled: bool = True,
    ) -> BindingRecord:
        installation = self._installation(installation_id)
        if installation.state == PluginLifecycleState.UNINSTALLED.value:
            raise PluginError(
                "package_unavailable", "uninstalled plugin must be explicitly installed again"
            )
        if installation.binding_id is not None:
            return self._binding(installation.binding_id)
        if dataset_id is not None and dataset_id != installation.dataset_id:
            raise PluginError("unknown_owner", "binding dataset does not match installation owner")
        binding_id = new_id("plugin_binding")
        binding_config = config or installation.config
        if binding_config is None:
            raise PluginError("protocol_mismatch", "plugin binding requires a configuration")
        binding = BindingRecord(
            binding_id=binding_id,
            installation_id=installation_id,
            dataset_id=installation.dataset_id,
            config=binding_config,
            enabled=False,
            global_enabled=global_enabled,
            binding_epoch=installation.binding_epoch,
            permission_epoch=installation.permission_epoch,
            updated_at=utc_now(),
        )
        updated = installation.model_copy(
            update={"binding_id": binding_id, "config": binding_config, "updated_at": utc_now()}
        )
        installations = tuple(
            updated if item.installation_id == installation_id else item
            for item in self._state.installations
        )
        self._state = self._state.model_copy(
            update={"installations": installations, "bindings": (*self._state.bindings, binding)}
        )
        self._save()
        return binding

    def enable(self, binding_id: str) -> BindingRecord:
        binding = self._binding(binding_id)
        installation = self._installation(binding.installation_id)
        if not binding.global_enabled:
            raise PluginError("permission_denied", "global plugin disable is a hard barrier")
        if installation.state in {
            PluginLifecycleState.UNINSTALLING.value,
            PluginLifecycleState.UNINSTALLED.value,
        }:
            raise PluginError("package_unavailable", "plugin is being removed")
        self.verify_package(installation.installation_id)
        new_epoch = binding.binding_epoch + 1
        updated_binding = binding.model_copy(
            update={"enabled": True, "binding_epoch": new_epoch, "updated_at": utc_now()}
        )
        updated_installation = installation.model_copy(
            update={
                "state": PluginLifecycleState.ENABLED.value,
                "binding_epoch": new_epoch,
                "updated_at": utc_now(),
                "last_error_code": None,
            }
        )
        self._replace(
            bindings=tuple(
                updated_binding if item.binding_id == binding_id else item
                for item in self._state.bindings
            ),
            installations=tuple(
                updated_installation
                if item.installation_id == installation.installation_id
                else item
                for item in self._state.installations
            ),
        )
        return updated_binding

    def begin_disable(self, binding_id: str, *, stop_run_ids: Iterable[str] = ()) -> BindingRecord:
        binding = self._binding(binding_id)
        requested = set(stop_run_ids)
        active = set(binding.active_run_ids)
        remaining = active.difference(requested)
        if remaining:
            raise PluginError("cleanup_blocked", "active Run depends on this plugin binding")
        updated = binding.model_copy(
            update={
                "enabled": False,
                "binding_epoch": binding.binding_epoch + 1,
                "updated_at": utc_now(),
            }
        )
        installation = self._installation(binding.installation_id)
        updated_installation = installation.model_copy(
            update={
                "state": PluginLifecycleState.DISABLING.value,
                "binding_epoch": updated.binding_epoch,
                "updated_at": utc_now(),
            }
        )
        self._replace(
            bindings=tuple(
                updated if item.binding_id == binding_id else item for item in self._state.bindings
            ),
            installations=tuple(
                updated_installation
                if item.installation_id == installation.installation_id
                else item
                for item in self._state.installations
            ),
        )
        return updated

    def complete_disable(
        self, binding_id: str, *, failed: str | None = None, restart_required: bool = False
    ) -> BindingRecord:
        binding = self._binding(binding_id)
        installation = self._installation(binding.installation_id)
        state = (
            PluginLifecycleState.RESTART_REQUIRED.value
            if restart_required
            else PluginLifecycleState.FAILED.value
            if failed
            else PluginLifecycleState.DISABLED.value
        )
        updated_installation = installation.model_copy(
            update={"state": state, "last_error_code": failed, "updated_at": utc_now()}
        )
        self._replace(
            installations=tuple(
                updated_installation
                if item.installation_id == installation.installation_id
                else item
                for item in self._state.installations
            )
        )
        return binding

    def mark_failed(self, installation_id: str, code: str) -> InstallationRecord:
        installation = self._installation(installation_id)
        updated = installation.model_copy(
            update={
                "state": PluginLifecycleState.FAILED.value,
                "last_error_code": code,
                "updated_at": utc_now(),
            }
        )
        self._replace(
            installations=tuple(
                updated if item.installation_id == installation_id else item
                for item in self._state.installations
            )
        )
        return updated

    def create_run(
        self, binding_id: str, *, run_id: str, scope: Scope, ttl_seconds: float = 300
    ) -> RunLease:
        binding = self._binding(binding_id)
        if not binding.enabled:
            raise PluginError("permission_denied", "plugin binding is not enabled")
        _validate_id(run_id, "run_id")
        if run_id in binding.active_run_ids:
            raise PluginError("revision_conflict", "Run already has an active plugin dependency")
        installation = self._installation(binding.installation_id)
        lease = RunLease(
            lease_id=new_id("plugin_lease"),
            run_id=run_id,
            binding_id=binding.binding_id,
            installation_id=installation.installation_id,
            dataset_id=installation.dataset_id,
            scope=scope,
            binding_epoch=binding.binding_epoch,
            permission_epoch=binding.permission_epoch,
            lease_fencing=max(
                (item.lease_fencing for item in self._state.runs if item.binding_id == binding_id),
                default=0,
            )
            + 1,
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )
        updated_binding = binding.model_copy(
            update={"active_run_ids": (*binding.active_run_ids, run_id), "updated_at": utc_now()}
        )
        self._replace(
            bindings=tuple(
                updated_binding if item.binding_id == binding_id else item
                for item in self._state.bindings
            ),
            runs=(*self._state.runs, lease),
        )
        return lease

    def get_run(self, lease_id: str) -> RunLease:
        for item in self._state.runs:
            if item.lease_id == lease_id:
                return item
        raise PluginError("lease_expired", "plugin Run lease is unknown")

    def release_run(
        self, lease_id: str, *, status: Literal["cancelled", "completed", "expired"] = "completed"
    ) -> RunLease:
        lease = self.get_run(lease_id)
        # A release may be retried from a completion path or a delayed
        # callback.  Once this lease has reached a terminal state, preserve
        # that durable result and, in particular, do not touch the binding's
        # current run slot.
        if lease.status != "active":
            return lease

        updated = lease.model_copy(update={"status": status})
        binding = self._binding(lease.binding_id)
        # ``active_run_ids`` contains only run IDs, so use the lease fencing
        # identity as a CAS guard when removing one.  A late release for an
        # older lease must not clear the marker owned by a newer lease for the
        # same run ID.  Taking the highest fencing value also fails closed if
        # a malformed state ever contains more than one active lease.
        current_active = max(
            (
                item
                for item in self._state.runs
                if item.status == "active"
                and item.binding_id == lease.binding_id
                and item.run_id == lease.run_id
            ),
            key=lambda item: item.lease_fencing,
            default=None,
        )
        owns_active_slot = (
            current_active is not None
            and current_active.lease_id == lease.lease_id
            and current_active.lease_fencing == lease.lease_fencing
        )
        if owns_active_slot:
            updated_binding = binding.model_copy(
                update={
                    "active_run_ids": tuple(
                        item for item in binding.active_run_ids if item != lease.run_id
                    ),
                    "updated_at": utc_now(),
                }
            )
        else:
            updated_binding = binding
        self._replace(
            runs=tuple(updated if item.lease_id == lease_id else item for item in self._state.runs),
            bindings=tuple(
                updated_binding if item.binding_id == binding.binding_id else item
                for item in self._state.bindings
            ),
        )
        return updated

    def assert_lease(self, lease: RunLease, context: RpcContext) -> None:
        current_lease = self.get_run(lease.lease_id)
        if current_lease.status != "active" or current_lease.expires_at <= utc_now():
            raise PluginError("lease_expired", "plugin Run lease is no longer active")
        binding = self._binding(lease.binding_id)
        installation = self._installation(lease.installation_id)
        if (
            current_lease != lease
            or context.scope != lease.scope
            or context.installation_id != lease.installation_id
            or context.dataset_id != lease.dataset_id
            or context.binding_epoch != lease.binding_epoch
            or context.permission_epoch != lease.permission_epoch
            or context.lease_fencing != lease.lease_fencing
            or binding.binding_epoch != lease.binding_epoch
            or binding.permission_epoch != lease.permission_epoch
            or not binding.enabled
            or not binding.global_enabled
            or installation.state != PluginLifecycleState.ENABLED.value
        ):
            raise PluginError(
                "stale_epoch", "plugin request is bound to a stale or disabled binding"
            )
        self.verify_package(installation.installation_id)

    def create_resource(
        self,
        installation_id: str,
        *,
        relative_path: str,
        category: str = "state",
        reconstructible: bool = True,
        path_kind: Literal["file", "directory"] = "file",
    ) -> Resource:
        installation = self._installation(installation_id)
        if installation.state in {
            PluginLifecycleState.UNINSTALLING.value,
            PluginLifecycleState.UNINSTALLED.value,
        }:
            raise PluginError("permission_denied", "plugin resource registration is closed")
        path = path_under(self.installation_root(installation_id), relative_path)
        if path == self.installation_root(installation_id) or relative_path.startswith(
            ("package/", "config/")
        ):
            raise PermissionDeniedError(
                "plugin cannot register outside its private data directories"
            )
        existing = next(
            (
                item
                for item in self._state.resources
                if item.resource.installation_id == installation_id
                and item.relative_path == relative_path
            ),
            None,
        )
        if existing is not None:
            return existing.resource
        ensure_managed_resource(path, directory=path_kind == "directory")
        stored = self._make_resource(
            installation,
            relative_path,
            category,
            path_kind=path_kind,
            reconstructible=reconstructible,
        )
        self._replace(
            resources=(*self._state.resources, stored),
        )
        return stored.resource

    def create_private_index(
        self,
        installation_id: str,
        *,
        relative_path: str,
        reconstructible: bool = True,
    ) -> PrivateIndexResource:
        resource = self.create_resource(
            installation_id,
            relative_path=relative_path,
            category="index",
            reconstructible=reconstructible,
            path_kind="file",
        )
        return PrivateIndexResource.model_validate(resource.model_dump())

    def resource_lookup(
        self, installation_id: str, resource_id: str
    ) -> tuple[PrivateIndexResource, Path] | None:
        for stored in self._state.resources:
            resource = stored.resource
            if (
                resource.resource_id != resource_id
                or resource.installation_id != installation_id
                or stored.status == "deleted"
            ):
                continue
            if resource.category != "index" or resource.storage != "managed_directory":
                return None
            path = path_under(self.installation_root(installation_id), stored.relative_path)
            return PrivateIndexResource.model_validate(resource.model_dump()), path
        return None

    def resource_revision(self, resource_id: str) -> int:
        for stored in self._state.resources:
            if stored.resource.resource_id == resource_id:
                return stored.revision
        raise PermissionDeniedError("resource is not registered")

    def bump_resource_revision(self, resource_id: str) -> int:
        updated: list[StoredResource] = []
        revision: int | None = None
        for stored in self._state.resources:
            if stored.resource.resource_id == resource_id:
                revision = stored.revision + 1
                updated.append(stored.model_copy(update={"revision": revision}))
            else:
                updated.append(stored)
        if revision is None:
            raise PermissionDeniedError("resource is not registered")
        self._replace(resources=tuple(updated))
        return revision

    def resources_for(self, installation_id: str) -> tuple[StoredResource, ...]:
        return tuple(
            item
            for item in self._state.resources
            if item.resource.installation_id == installation_id and item.status != "deleted"
        )

    def begin_uninstall(
        self,
        installation_id: str,
        *,
        data_policy: Literal["keep", "delete"],
        stop_run_ids: Iterable[str] = (),
        expected_binding_epoch: int | None = None,
    ) -> CleanupPlanRecord:
        installation = self._installation(installation_id)
        binding = self._binding(installation.binding_id) if installation.binding_id else None
        epoch = installation.binding_epoch if binding is None else binding.binding_epoch
        if expected_binding_epoch is not None and expected_binding_epoch != epoch:
            raise PluginError("stale_epoch", "uninstall inventory is stale")
        requested = set(stop_run_ids)
        active = set(binding.active_run_ids) if binding else set()
        blockers = active.difference(requested)
        items: list[CleanupItem] = []
        for stored in self.resources_for(installation_id):
            resource = stored.resource
            if resource.consumer_ids and set(resource.consumer_ids) - {installation_id}:
                items.append(
                    CleanupItem(
                        resource_id=resource.resource_id,
                        outcome="blocked",
                        reason="shared_consumer",
                        blocker_ids=tuple(resource.consumer_ids),
                    )
                )
            elif resource.retention_lock_ids:
                items.append(
                    CleanupItem(
                        resource_id=resource.resource_id,
                        outcome="blocked",
                        reason="retention_lock",
                        blocker_ids=resource.retention_lock_ids,
                    )
                )
            elif blockers:
                items.append(
                    CleanupItem(
                        resource_id=resource.resource_id,
                        outcome="blocked",
                        reason="active_run",
                        blocker_ids=tuple(sorted(blockers)),
                    )
                )
            elif data_policy == "keep" and resource.category in self._KEEP_CATEGORIES:
                items.append(
                    CleanupItem(
                        resource_id=resource.resource_id,
                        outcome="retained",
                        reason="keep_selected",
                        blocker_ids=(),
                    )
                )
            else:
                items.append(
                    CleanupItem(
                        resource_id=resource.resource_id,
                        outcome="pending",
                        reason="exclusive",
                        blocker_ids=(),
                    )
                )
        operation_id = new_id("plugin_cleanup")
        plan = CleanupPlanRecord(
            operation_id=operation_id,
            installation_id=installation_id,
            data_policy=data_policy,
            inventory_revision=installation.inventory_revision,
            expected_binding_epoch=epoch,
            items=tuple(items),
            state="blocked" if blockers else "pending",
            updated_at=utc_now(),
        )
        updated = installation.model_copy(
            update={
                "state": PluginLifecycleState.UNINSTALLING.value,
                "binding_epoch": epoch + 1,
                "updated_at": utc_now(),
            }
        )
        dataset = next(
            item for item in self._state.datasets if item.dataset_id == installation.dataset_id
        )
        updated_dataset = dataset.model_copy(
            update={"state": "deleting", "revision": dataset.revision + 1}
        )
        self._state = self._state.model_copy(
            update={
                "installations": tuple(
                    updated if item.installation_id == installation_id else item
                    for item in self._state.installations
                ),
                "cleanup_plans": (*self._state.cleanup_plans, plan),
                "datasets": tuple(
                    updated_dataset if item.dataset_id == dataset.dataset_id else item
                    for item in self._state.datasets
                ),
            }
        )
        self._save()
        return plan

    def cleanup_plan(self, operation_id: str) -> CleanupPlanRecord:
        for plan in self._state.cleanup_plans:
            if plan.operation_id == operation_id:
                return plan
        raise PluginError("cleanup_blocked", "cleanup plan is unknown")

    def mark_resource_cleanup(
        self, operation_id: str, resource_id: str, item: CleanupItem
    ) -> CleanupPlanRecord:
        plan = self.cleanup_plan(operation_id)
        if not any(existing.resource_id == resource_id for existing in plan.items):
            raise PluginError("cleanup_blocked", "resource is not part of the cleanup inventory")
        items = tuple(
            item if existing.resource_id == resource_id else existing for existing in plan.items
        )
        updated_plan = plan.model_copy(update={"items": items, "updated_at": utc_now()})
        resources: list[StoredResource] = []
        for stored in self._state.resources:
            if stored.resource.resource_id != resource_id:
                resources.append(stored)
                continue
            status = (
                "deleted"
                if item.outcome == "deleted"
                else "retained"
                if item.outcome == "retained"
                else stored.status
            )
            resources.append(stored.model_copy(update={"status": status}))
        self._replace(
            cleanup_plans=tuple(
                updated_plan if existing.operation_id == operation_id else existing
                for existing in self._state.cleanup_plans
            ),
            resources=tuple(resources),
        )
        return updated_plan

    def complete_uninstall(self, operation_id: str) -> CleanupPlanRecord:
        plan = self.cleanup_plan(operation_id)
        unfinished = tuple(
            item
            for item in plan.items
            if item.outcome in {"pending", "blocked", "external_unconfirmed"}
        )
        if unfinished:
            updated = plan.model_copy(update={"state": "blocked", "updated_at": utc_now()})
            self._replace(
                cleanup_plans=tuple(
                    updated if item.operation_id == operation_id else item
                    for item in self._state.cleanup_plans
                )
            )
            raise PluginError("cleanup_blocked", "cleanup plan still contains unfinished resources")
        installation = self._installation(plan.installation_id)
        updated_installation = installation.model_copy(
            update={
                "state": PluginLifecycleState.UNINSTALLED.value,
                "binding_id": None,
                "config": None,
                "updated_at": utc_now(),
            }
        )
        dataset = next(
            item for item in self._state.datasets if item.dataset_id == installation.dataset_id
        )
        retained = any(item.outcome == "retained" for item in plan.items)
        updated_dataset = dataset.model_copy(
            update={
                "state": "retained" if retained else "deleted",
                "installation_id": None,
                "revision": dataset.revision + 1,
            }
        )
        completed = plan.model_copy(update={"state": "completed", "updated_at": utc_now()})
        self._replace(
            installations=tuple(
                updated_installation if item.installation_id == plan.installation_id else item
                for item in self._state.installations
            ),
            bindings=tuple(
                item
                for item in self._state.bindings
                if item.installation_id != plan.installation_id
            ),
            datasets=tuple(
                updated_dataset if item.dataset_id == dataset.dataset_id else item
                for item in self._state.datasets
            ),
            cleanup_plans=tuple(
                completed if item.operation_id == operation_id else item
                for item in self._state.cleanup_plans
            ),
        )
        return completed

    def set_global_enabled(self, binding_id: str, enabled: bool) -> None:
        binding = self._binding(binding_id)
        updated = binding.model_copy(update={"global_enabled": enabled, "updated_at": utc_now()})
        self._replace(
            bindings=tuple(
                updated if b.binding_id == binding_id else b for b in self._state.bindings
            )
        )

    def configure(self, installation_id: str, config: dict[str, Any]) -> None:
        installation = self._installation(installation_id)
        if installation.state != "disabled":
            raise PluginError("revision_conflict", "configuration requires a disabled installation")
        target = self.installation_root(installation_id) / "config" / "settings.json"
        if target.is_symlink() or target.parent.is_symlink():
            raise PermissionDeniedError("unsafe configuration path")
        temporary = target.with_suffix(".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(config, stream)
        temporary.replace(target)

    def pending_cleanup_plans(self) -> tuple[CleanupPlanRecord, ...]:
        return tuple(item for item in self._state.cleanup_plans if item.state != "completed")


__all__ = [
    "BindingRecord",
    "CleanupPlanRecord",
    "DatasetRecord",
    "InstallationRecord",
    "PluginLifecycleState",
    "PluginRegistry",
    "RegistryState",
    "RunLease",
    "StoredResource",
]
