"""PluginHost orchestration for MP-1.

This module owns admission, engine lifetime, Run leases, cancellation, and
resource cleanup.  It intentionally has no HTTP or SQLite dependency.  Core
integration can inject typed callbacks and later replace the private Registry
without changing the plugin adapters.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from operant.contracts.b2_1 import (
    Certification,
    CleanupItem,
    HostAdmission,
    LifecycleReceipt,
    LifecycleRequest,
    LifecycleResult,
    RpcContext,
    Scope,
)
from operant.domain.models import new_id, utc_now
from operant.plugins.payload import (
    allowed_host_operations,
    authorize_engine_request,
    authorize_engine_result,
)
from operant.plugins.protocol import (
    ENGINE_REQUEST_TYPES,
    ENGINE_RESULT_TYPES,
    BudgetExceededError,
    CancellationError,
    DeadlineExceededError,
    HostBudget,
    HostCallbacks,
    InProcessPluginEngine,
    PackageUnavailableError,
    PermissionDeniedError,
    PluginError,
    PluginImplementation,
    PluginProtocolError,
    RestartRequiredError,
    RestrictedHostApi,
    SandboxEvidence,
    SandboxProbe,
    StdioPluginEngine,
    _remove_empty_managed_directory,
    _remove_managed_path,
    canonical_json,
    digest_payload,
    path_under,
)
from operant.plugins.registry import (
    BindingRecord,
    CleanupPlanRecord,
    InstallationRecord,
    PluginLifecycleState,
    PluginRegistry,
    RunLease,
)

PluginFactory = Callable[[InstallationRecord], PluginImplementation]


@dataclass
class _EngineSlot:
    engine: InProcessPluginEngine | StdioPluginEngine
    admission: HostAdmission
    limits: HostBudget
    implementation: PluginImplementation | None = None
    sandbox_evidence: SandboxEvidence | None = None
    module_name: str | None = None


@dataclass
class _ActiveCall:
    installation_id: str
    request_id: str
    lease_id: str
    run_id: str
    event: asyncio.Event
    slot: _EngineSlot
    task: asyncio.Task[BaseModel] | None = None


def _receipt(
    *,
    operation_id: str,
    installation_id: str,
    state: Literal[
        "enabled",
        "disabling",
        "disabled",
        "uninstalling",
        "uninstalled",
        "failed",
        "restart_required",
        "blocked",
    ],
    binding_epoch: int,
    inventory_revision: int = 0,
    cleanup: Iterable[CleanupItem] = (),
    ack: Literal["host_accepted", "completed", "failed", "unknown"] = "completed",
) -> LifecycleReceipt:
    return LifecycleReceipt(
        operation_id=operation_id,
        installation_id=installation_id,
        state=state,
        binding_epoch=binding_epoch,
        inventory_revision=inventory_revision,
        cleanup=tuple(cleanup),
        cursor="0",
        ack=ack,
    )


class PluginHost:
    """A Core-owned Host with explicit lifecycle and fail-closed admission."""

    _CALL_QUIESCE_TIMEOUT_SECONDS = 1.0

    def __init__(
        self,
        registry: PluginRegistry,
        *,
        plugin_factories: Mapping[str, PluginFactory] | None = None,
        stdio_commands: Mapping[str, Sequence[str]] | None = None,
        callbacks: HostCallbacks | None = None,
        budget: HostBudget | None = None,
        sandbox_probe: SandboxProbe | None = None,
    ) -> None:
        self.registry = registry
        self.plugin_factories = dict(plugin_factories or {})
        self.stdio_commands = {key: tuple(value) for key, value in (stdio_commands or {}).items()}
        self.callbacks = callbacks or HostCallbacks()
        self.budget = budget or HostBudget()
        self.sandbox_probe = sandbox_probe or SandboxProbe()
        self._engines: dict[str, _EngineSlot] = {}
        self._calls: dict[str, _ActiveCall] = {}
        self._cancel_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cleanup_tasks: dict[str, asyncio.Task[Any]] = {}
        self._close_tasks: dict[str, asyncio.Task[Any]] = {}
        self._isolation_evidence: dict[str, SandboxEvidence] = {}
        self._lifecycle_fences: set[str] = set()
        self._closed = False
        # Loading a registry never imports/instantiates a plugin.  Only Host
        # cleanup metadata is read here, and cleanup is explicit via resume.

    def bind(
        self,
        installation_id: str,
        *,
        dataset_id: str | None = None,
        config: Any | None = None,
        global_enabled: bool = True,
    ) -> BindingRecord:
        return self.registry.bind(
            installation_id,
            dataset_id=dataset_id,
            config=config,
            global_enabled=global_enabled,
        )

    def enable(self, binding_id: str) -> LifecycleReceipt:
        installation_id = self.registry.get_binding(binding_id).installation_id
        if self._closed or installation_id in self._lifecycle_fences:
            raise RestartRequiredError("plugin lifecycle must finish before enabling again")
        binding = self.registry.enable(binding_id)
        installation = self.registry.get_installation(binding.installation_id)
        return _receipt(
            operation_id=new_id("plugin_enable"),
            installation_id=installation.installation_id,
            state="enabled",
            binding_epoch=binding.binding_epoch,
            inventory_revision=installation.inventory_revision,
        )

    def admission(
        self,
        installation_id: str,
        *,
        mode: Literal["auto", "trusted_in_process", "isolated"] = "auto",
    ) -> HostAdmission:
        installation = self.registry.get_installation(installation_id)
        if installation.binding_id is None:
            raise PluginError(
                "permission_denied", "plugin installation has no configuration binding"
            )
        binding = self.registry.get_binding(installation.binding_id)
        if not binding.enabled or not binding.global_enabled:
            raise PluginError("permission_denied", "plugin binding is disabled")
        current_cert = self._current_certification(installation)
        if mode == "auto":
            if current_cert is not None and "trusted_in_process" in current_cert.allowed_modes:
                mode = "trusted_in_process"
            else:
                mode = "isolated"
        if mode not in {"trusted_in_process", "isolated"}:
            raise PluginError("certification_invalid", "unknown plugin execution mode")
        certification: Certification | None
        if mode == "trusted_in_process":
            certification = self.registry.verify_certification(installation_id, mode=mode)
            assert certification is not None
            evidence_ref = None
        else:
            certification = self.registry.verify_certification(installation_id, mode=mode)
            evidence = self._probe_isolation(installation)
            self._isolation_evidence[installation_id] = evidence
            evidence_ref = evidence.evidence_ref
        return HostAdmission(
            host_instance_id=f"host_{id(self):x}",
            installation_id=installation_id,
            mode=mode,
            certification_id=certification.certification_id
            if mode == "trusted_in_process" and certification
            else None,
            isolation_evidence_ref=evidence_ref,
            eligibility="eligible",
            permission_epoch=binding.permission_epoch,
            restart_required=installation.state == PluginLifecycleState.RESTART_REQUIRED.value,
        )

    def _current_certification(self, installation: InstallationRecord) -> Certification | None:
        if installation.certification_id is None:
            return None
        if not self.registry.certification_is_current(installation):
            return None
        return next(
            (
                item
                for item in self.registry.list_certifications()
                if item.certification_id == installation.certification_id
            ),
            None,
        )

    def _probe_isolation(self, installation: InstallationRecord) -> SandboxEvidence:
        package = self.registry.package_path(installation.installation_id)
        return self.sandbox_probe.check(
            package_root=package,
            data_root=self.registry.data_path(installation.installation_id),
            state_root=self.registry.state_path_for(installation.installation_id),
            logs_root=self.registry.logs_path(installation.installation_id),
            tmp_root=self.registry.tmp_path(installation.installation_id),
            executable=Path(sys.executable).resolve(strict=True),
        )

    def _stdio_argv(self, installation: InstallationRecord) -> tuple[str, ...]:
        package = self.registry.package_path(installation.installation_id)
        configured = self.stdio_commands.get(
            installation.installation_id
        ) or self.stdio_commands.get(installation.manifest.plugin_id)
        if configured:
            if any(part in {"-c", "-m", "--command"} for part in configured):
                raise PluginProtocolError("stdio command must not execute shell or inline code")
            return configured
        entrypoint = installation.manifest.entrypoint
        path = path_under(package, entrypoint)
        if not path.is_file() or path.is_symlink():
            raise PackageUnavailableError("plugin manifest entrypoint is unavailable")
        if entrypoint.endswith(".py"):
            # ``-I`` removes PYTHONPATH/user-site injection.  Package metadata
            # is restricted to stdlib/package-local dependencies by Registry;
            # arbitrary environment dependency resolution is unsupported.
            return (str(Path(sys.executable).resolve(strict=True)), "-I", "-S", str(path))
        return (str(path),)

    def _load_inprocess_package(
        self, installation: InstallationRecord
    ) -> tuple[PluginImplementation, str]:
        package = self.registry.package_path(installation.installation_id)
        path = path_under(package, installation.manifest.entrypoint)
        if not path.is_file() or path.is_symlink():
            raise PackageUnavailableError("plugin manifest entrypoint is unavailable")
        module_name = (
            f"_operant_plugin_{installation.installation_id}_"
            f"{installation.manifest.package_digest[:12]}"
        )
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PackageUnavailableError("plugin entrypoint cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginError(
                "plugin_start_failed", "plugin entrypoint failed during load"
            ) from exc
        sys.modules[module_name] = module
        factory = getattr(module, "create_plugin", None)
        if not callable(factory):
            sys.modules.pop(module_name, None)
            raise PluginProtocolError("in-process package must expose create_plugin()")
        try:
            implementation = factory()
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginError("plugin_start_failed", "plugin factory failed") from exc
        if not hasattr(implementation, "handle"):
            sys.modules.pop(module_name, None)
            raise PluginProtocolError("plugin factory did not return a Host implementation")
        return implementation, module_name

    async def start(
        self,
        installation_id: str,
        *,
        mode: Literal["auto", "trusted_in_process", "isolated"] = "auto",
    ) -> HostAdmission:
        if self._closed:
            raise PluginError("package_unavailable", "PluginHost is closed")
        if installation_id in self._lifecycle_fences:
            raise RestartRequiredError("plugin lifecycle must finish before restarting")
        installation = self.registry.get_installation(installation_id)
        if (
            installation.state != PluginLifecycleState.ENABLED.value
            or installation.binding_id is None
        ):
            raise PluginError("permission_denied", "plugin must be explicitly enabled before start")
        existing = self._engines.get(installation_id)
        admission = self.admission(installation_id, mode=mode)
        if existing is not None:
            if existing.admission.mode != admission.mode:
                raise RestartRequiredError("changing plugin mode requires a safe restart")
            return admission
        limits = self.budget.intersect_manifest(installation.manifest)
        implementation: PluginImplementation | None = None
        sandbox_evidence: SandboxEvidence | None = None
        module_name: str | None = None
        engine: InProcessPluginEngine | StdioPluginEngine
        if admission.mode == "trusted_in_process":
            factory = self.plugin_factories.get(installation.manifest.plugin_id)
            try:
                if factory is not None:
                    implementation = factory(installation)
                    engine = InProcessPluginEngine(implementation)
                else:
                    implementation, module_name = self._load_inprocess_package(installation)
                    engine = InProcessPluginEngine(implementation)
                await engine.start()
            except Exception as exc:
                self.registry.mark_failed(installation_id, "plugin_start_failed")
                if isinstance(exc, PluginError):
                    raise
                raise PluginError("plugin_start_failed", "trusted plugin could not start") from exc
        else:
            sandbox_evidence = self._isolation_evidence.pop(installation_id, None)
            if sandbox_evidence is None:
                sandbox_evidence = self._probe_isolation(installation)
            engine = StdioPluginEngine(
                argv=self._stdio_argv(installation),
                package_root=self.registry.package_path(installation_id),
                mode="isolated",
                sandbox_probe=self.sandbox_probe,
                data_root=self.registry.data_path(installation_id),
                state_root=self.registry.state_path_for(installation_id),
                logs_root=self.registry.logs_path(installation_id),
                tmp_root=self.registry.tmp_path(installation_id),
                executable=Path(sys.executable).resolve(strict=True),
                limits=limits,
                sandbox_evidence=sandbox_evidence,
                failure_callback=lambda code: self.registry.mark_failed(installation_id, code),
            )
            try:
                await engine.start()
            except Exception as exc:
                self.registry.mark_failed(
                    installation_id, getattr(exc, "code", "plugin_start_failed")
                )
                if isinstance(exc, PluginError):
                    raise
                raise PluginError("plugin_start_failed", "isolated plugin could not start") from exc
        self._engines[installation_id] = _EngineSlot(
            engine=engine,
            admission=admission,
            limits=limits,
            implementation=implementation,
            sandbox_evidence=sandbox_evidence,
            module_name=module_name,
        )
        return admission

    def start_run(
        self,
        binding_id: str,
        *,
        run_id: str,
        scope: Scope,
        ttl_seconds: float = 300,
    ) -> RunLease:
        if self._closed:
            raise PluginError("package_unavailable", "PluginHost is closed")
        binding = self.registry.get_binding(binding_id)
        if binding.installation_id in self._lifecycle_fences:
            raise PluginError("stale_epoch", "plugin lifecycle transition is in progress")
        return self.registry.create_run(
            binding_id, run_id=run_id, scope=scope, ttl_seconds=ttl_seconds
        )

    def _assert_active(self, lease: RunLease, context: RpcContext) -> None:
        if lease.installation_id in self._lifecycle_fences:
            raise PluginError("stale_epoch", "plugin lifecycle transition is in progress")
        self.registry.assert_lease(lease, context)
        slot = self._engines.get(lease.installation_id)
        if slot is None:
            raise PackageUnavailableError("plugin engine has not been started")
        self.registry.verify_certification(lease.installation_id, mode=slot.admission.mode)

    def _host_api(
        self,
        lease: RunLease,
        context: RpcContext,
        limits: HostBudget,
        *,
        operation: str = "lifecycle",
    ) -> RestrictedHostApi:
        installation_root = self.registry.installation_root(lease.installation_id)
        config = self.registry.get_binding(lease.binding_id).config
        capabilities = self.registry.get_installation(lease.installation_id).manifest.capabilities
        cancel_event = asyncio.Event()
        active = _ActiveCall(
            installation_id=lease.installation_id,
            request_id=context.request_id,
            lease_id=lease.lease_id,
            run_id=lease.run_id,
            event=cancel_event,
            slot=self._engines[lease.installation_id],
        )
        self._calls[context.request_id] = active
        return RestrictedHostApi(
            context=context,
            installation_root=installation_root,
            callbacks=self.callbacks,
            limits=limits,
            resource_lookup=lambda resource_id: self.registry.resource_lookup(
                lease.installation_id, resource_id
            ),
            resource_register=lambda relative_path, category, reconstructible: (
                self.registry.create_private_index(
                    lease.installation_id,
                    relative_path=relative_path,
                    reconstructible=reconstructible,
                )
            ),
            resource_revision=self.registry.resource_revision,
            resource_bump=self.registry.bump_resource_revision,
            cancel_event=cancel_event,
            validate_active=lambda: self._assert_active(lease, context),
            allowed_operations=allowed_host_operations(capabilities),
            allowed_model_profiles=frozenset(
                profile
                for capability, profile in (
                    ("extract", config.extraction_model_profile_id),
                    ("recall", config.rerank_model_profile_id),
                )
                if capability == operation and capability in capabilities and profile is not None
            ),
        )

    @staticmethod
    def _request_context(request: BaseModel) -> RpcContext:
        context = getattr(request, "context", None)
        if not isinstance(context, RpcContext):
            raise PluginProtocolError("plugin request must carry RpcContext")
        return context

    @staticmethod
    def _validate_result(operation: str, request: BaseModel, result: BaseModel) -> BaseModel:
        expected = ENGINE_RESULT_TYPES.get(operation)
        if expected is None or not isinstance(result, expected):
            raise PluginProtocolError("plugin result has an unexpected type")
        request_context = getattr(request, "context", None)
        request_id = getattr(request_context, "request_id", None)
        result_request_id = getattr(result, "request_id", None)
        if result_request_id is not None and result_request_id != request_id:
            raise PluginProtocolError("plugin result is bound to another request")
        return result

    async def invoke(self, lease: RunLease, operation: str, request: BaseModel) -> BaseModel:
        """Invoke one typed engine operation through the active Host boundary."""

        if self._closed:
            raise PluginError("package_unavailable", "PluginHost is closed")
        request_type = ENGINE_REQUEST_TYPES.get(operation)
        if request_type is None:
            raise PluginProtocolError("unsupported plugin operation")
        if not isinstance(request, request_type):
            try:
                request = request_type.model_validate(request)
            except Exception as exc:
                raise PluginProtocolError("plugin request failed schema validation") from exc
        context = self._request_context(request)
        self._assert_active(lease, context)
        slot = self._engines.get(lease.installation_id)
        if slot is None:
            raise PackageUnavailableError("plugin engine has not been started")
        if operation != "lifecycle":
            manifest_capability = operation
            manifest = self.registry.get_installation(lease.installation_id).manifest
            if manifest_capability not in manifest.capabilities:
                raise PluginError(
                    "permission_denied", "plugin operation is not declared by its manifest"
                )
        request_bytes = len(canonical_json(request.model_dump(mode="json")))
        if request_bytes > slot.limits.max_request_bytes:
            raise BudgetExceededError()
        if utc_now() >= context.deadline:
            raise DeadlineExceededError()
        if operation != "lifecycle":
            authorize_engine_request(operation, request, context, self.callbacks)
        if context.request_id in self._calls:
            raise PluginError("revision_conflict", "request is already active")
        if (
            sum(call.installation_id == lease.installation_id for call in self._calls.values())
            >= slot.limits.max_concurrency
        ):
            raise PluginError("concurrency_limit", "plugin concurrency budget is exhausted")
        host = self._host_api(lease, context, slot.limits, operation=operation)
        call = self._calls[context.request_id]
        try:
            task = asyncio.create_task(slot.engine.invoke(operation, request, host))
            call.task = task
            timeout = min(
                slot.limits.timeout_seconds,
                max(0.001, (context.deadline - utc_now()).total_seconds()),
            )
            done, pending = await asyncio.wait({task}, timeout=timeout)
            if pending:
                call.event.set()
                cancel_finished = await self._cancel_engine(call)
                if not task.done():
                    task.cancel()
                    _done, still_pending = await asyncio.wait(
                        {task}, timeout=self._CALL_QUIESCE_TIMEOUT_SECONDS
                    )
                else:
                    still_pending = set()
                if not still_pending:
                    await asyncio.gather(task, return_exceptions=True)
                if isinstance(slot.engine, InProcessPluginEngine):
                    self.registry.mark_failed(lease.installation_id, "restart_required")
                if not cancel_finished or still_pending:
                    # Keep the call in the Host registry until its task really
                    # finishes.  Its completion callback removes it and
                    # consumes any late exception.
                    raise RestartRequiredError(
                        "in-process plugin call did not quiesce before its deadline"
                    )
                raise DeadlineExceededError()
            result = task.result()
            if call.event.is_set():
                raise CancellationError()
            self._assert_active(lease, context)
            result = self._validate_result(operation, request, result)
            if operation != "lifecycle":
                authorize_engine_result(operation, request, result, context, self.callbacks)
            response_bytes = len(canonical_json(result.model_dump(mode="json")))
            if response_bytes > slot.limits.max_response_bytes:
                raise BudgetExceededError()
            return result
        except PluginError as exc:
            if call.event.is_set() and exc.code not in {
                "stale_epoch",
                "lease_expired",
                "restart_required",
            }:
                raise CancellationError() from exc
            raise
        except Exception as exc:
            raise PluginError("plugin_failed", "plugin invocation failed") from exc
        finally:
            retained_task = call.task
            if retained_task is None or retained_task.done():
                self._calls.pop(context.request_id, None)
            else:
                retained_task.add_done_callback(
                    lambda finished: self._retire_call(context.request_id, call, finished)
                )

    def _retire_call(
        self, request_id: str, call: _ActiveCall, task: asyncio.Task[BaseModel]
    ) -> None:
        """Drop a call only after a retained engine task actually finishes."""

        with suppress(BaseException):
            task.exception()
        if self._calls.get(request_id) is call:
            self._calls.pop(request_id, None)

    async def _cancel_engine(self, call: _ActiveCall) -> bool:
        cancel_task = self._cancel_tasks.get(call.request_id)
        if cancel_task is None:
            cancel_task = asyncio.create_task(call.slot.engine.cancel(call.request_id))
            self._cancel_tasks[call.request_id] = cancel_task
        _done, pending = await asyncio.wait(
            {cancel_task},
            timeout=self._CALL_QUIESCE_TIMEOUT_SECONDS,
        )
        if pending:
            return False
        self._cancel_tasks.pop(call.request_id, None)
        try:
            cancel_task.result()
        except BaseException:
            return False
        return True
        # Cancellation is fail-closed.  The request is never accepted just
        # because a plugin ignored its cancellation callback.

    async def _quiesce_calls(self, installation_id: str) -> bool:
        """Request cancellation and wait for plugin code to leave the engine."""

        calls = tuple(
            call for call in self._calls.values() if call.installation_id == installation_id
        )
        if not calls:
            cancellation_succeeded = True
            pending_tasks: set[asyncio.Task[Any]] = set()
        else:
            for call in calls:
                call.event.set()
            cancel_results = await asyncio.gather(
                *(self._cancel_engine(call) for call in calls),
                return_exceptions=True,
            )
            cancellation_succeeded = all(result is True for result in cancel_results)
            tasks = tuple(call.task for call in calls)
            if any(task is None for task in tasks):
                return False
            pending_tasks = {task for task in tasks if task is not None and not task.done()}
        cleanup_task = self._cleanup_tasks.get(installation_id)
        if cleanup_task is not None:
            if not cleanup_task.done():
                return False
            self._cleanup_tasks.pop(installation_id, None)
            with suppress(BaseException):
                cleanup_task.result()
        if not pending_tasks:
            return cancellation_succeeded
        _done, pending = await asyncio.wait(
            pending_tasks,
            timeout=self._CALL_QUIESCE_TIMEOUT_SECONDS,
        )
        for task in pending:
            task.cancel()
        if pending:
            _done, pending = await asyncio.wait(
                pending,
                timeout=self._CALL_QUIESCE_TIMEOUT_SECONDS,
            )
        return cancellation_succeeded and not pending

    async def _run_cleanup_hook(
        self, installation_id: str, cleanup_hook: Callable[[], Any], timeout_seconds: float
    ) -> bool:
        """Run a trusted cleanup hook under a bounded cooperative wait."""

        existing = self._cleanup_tasks.get(installation_id)
        if existing is not None:
            if not existing.done():
                return False
            self._cleanup_tasks.pop(installation_id, None)
            with suppress(Exception):
                existing.result()
            return True

        async def run_hook() -> None:
            if inspect.iscoroutinefunction(cleanup_hook):
                result = cleanup_hook()
            else:
                result = await asyncio.to_thread(cleanup_hook)
            if inspect.isawaitable(result):
                await result

        task = asyncio.create_task(run_hook())
        self._cleanup_tasks[installation_id] = task
        _done, pending = await asyncio.wait({task}, timeout=timeout_seconds)
        if pending:
            return False
        self._cleanup_tasks.pop(installation_id, None)
        with suppress(Exception):
            task.result()
        return True

    async def _close_engine(self, installation_id: str, slot: _EngineSlot) -> bool:
        """Close an engine under a bounded wait, preserving pending work."""

        existing = self._close_tasks.get(installation_id)
        if existing is not None:
            if not existing.done():
                return False
            self._close_tasks.pop(installation_id, None)
            try:
                existing.result()
            except Exception:
                return False
            return True
        task = asyncio.create_task(slot.engine.close())
        self._close_tasks[installation_id] = task
        _done, pending = await asyncio.wait(
            {task},
            timeout=min(slot.limits.timeout_seconds, self._CALL_QUIESCE_TIMEOUT_SECONDS),
        )
        if pending:
            return False
        self._close_tasks.pop(installation_id, None)
        try:
            task.result()
        except Exception:
            return False
        return True

    async def invoke_batch(
        self,
        lease: RunLease,
        operation: str,
        requests: Sequence[BaseModel],
    ) -> tuple[BaseModel, ...]:
        """Run a bounded batch while reusing one already-admitted engine."""

        if (
            len(requests)
            > self.registry.get_installation(lease.installation_id).manifest.max_concurrency * 100
        ):
            raise BudgetExceededError("plugin batch is too large")
        results: list[BaseModel] = []
        for request in requests:
            results.append(await self.invoke(lease, operation, request))
        return tuple(results)

    async def lifecycle(
        self,
        lease: RunLease,
        *,
        operation: Literal["negotiate", "health", "cancel", "checkpoint", "restore"],
        checkpoint_ref: str | None = None,
    ) -> LifecycleResult:
        """Run a lifecycle handshake through the same typed engine channel."""

        context = RpcContext(
            sdk_version="operant-memory-sdk.v1",
            request_id=new_id("plugin_request"),
            installation_id=lease.installation_id,
            dataset_id=lease.dataset_id,
            scope=lease.scope,
            deadline=utc_now() + timedelta(seconds=30),
            cancel_token=new_id("plugin_cancel"),
            idempotency_key=new_id("plugin_lifecycle"),
            request_digest=digest_payload(
                {"operation": operation, "checkpoint_ref": checkpoint_ref}
            ),
            binding_epoch=lease.binding_epoch,
            permission_epoch=lease.permission_epoch,
            lease_fencing=lease.lease_fencing,
        )
        request = LifecycleRequest(
            context=context,
            operation=operation,
            checkpoint_ref=checkpoint_ref,
        )
        result = await self.invoke(lease, "lifecycle", request)
        assert isinstance(result, LifecycleResult)
        return result

    async def cancel(self, request_id: str) -> bool:
        active = self._calls.get(request_id)
        if active is None:
            return False
        active.event.set()
        await self._cancel_engine(active)
        return True

    async def stop(
        self,
        installation_id: str,
        *,
        stop_run_ids: Iterable[str] = (),
    ) -> LifecycleReceipt:
        installation = self.registry.get_installation(installation_id)
        if installation.binding_id is None:
            return _receipt(
                operation_id=new_id("plugin_stop"),
                installation_id=installation_id,
                state="disabled",
                binding_epoch=installation.binding_epoch,
                inventory_revision=installation.inventory_revision,
            )
        binding = self.registry.get_binding(installation.binding_id)
        requested = set(stop_run_ids)
        remaining = set(binding.active_run_ids).difference(requested)
        if remaining:
            return _receipt(
                operation_id=new_id("plugin_stop"),
                installation_id=installation_id,
                state="blocked",
                binding_epoch=binding.binding_epoch,
                inventory_revision=installation.inventory_revision,
                cleanup=(
                    CleanupItem(
                        resource_id=f"run_{run}",
                        outcome="blocked",
                        reason="active_run",
                        blocker_ids=(run,),
                    )
                    for run in sorted(remaining)
                ),
                ack="failed",
            )
        # Fence new invocations before asking existing calls to stop.  This
        # keeps the lifecycle transition closed while the cooperative
        # cancellation handshake is in progress.
        self._lifecycle_fences.add(installation_id)
        if not await self._quiesce_calls(installation_id):
            failed_binding = self.registry.begin_disable(binding.binding_id, stop_run_ids=requested)
            self.registry.complete_disable(failed_binding.binding_id, restart_required=True)
            current = self.registry.get_installation(installation_id)
            return _receipt(
                operation_id=new_id("plugin_stop"),
                installation_id=installation_id,
                state="restart_required",
                binding_epoch=failed_binding.binding_epoch,
                inventory_revision=current.inventory_revision,
                ack="failed",
            )
        for lease in tuple(self.registry.state.runs):
            if (
                lease.binding_id == binding.binding_id
                and lease.run_id in requested
                and lease.status == "active"
            ):
                self.registry.release_run(lease.lease_id, status="cancelled")
        binding = self.registry.begin_disable(binding.binding_id)
        slot = self._engines.get(installation_id)
        if slot is not None and not await self._close_engine(installation_id, slot):
            self.registry.complete_disable(binding.binding_id, restart_required=True)
            return _receipt(
                operation_id=new_id("plugin_stop"),
                installation_id=installation_id,
                state="restart_required",
                binding_epoch=binding.binding_epoch,
                inventory_revision=installation.inventory_revision,
                ack="failed",
            )
        self._engines.pop(installation_id, None)
        self.registry.complete_disable(binding.binding_id)
        installation = self.registry.get_installation(installation_id)
        self._lifecycle_fences.discard(installation_id)
        return _receipt(
            operation_id=new_id("plugin_stop"),
            installation_id=installation_id,
            state="disabled",
            binding_epoch=binding.binding_epoch,
            inventory_revision=installation.inventory_revision,
        )

    async def disable(
        self,
        binding_id: str,
        *,
        stop_run_ids: Iterable[str] = (),
    ) -> LifecycleReceipt:
        binding = self.registry.get_binding(binding_id)
        return await self.stop(binding.installation_id, stop_run_ids=stop_run_ids)

    async def uninstall(
        self,
        installation_id: str,
        *,
        data_policy: Literal["keep", "delete"],
        stop_run_ids: Iterable[str] = (),
        expected_binding_epoch: int | None = None,
    ) -> LifecycleReceipt:
        installation = self.registry.get_installation(installation_id)
        binding: BindingRecord | None = None
        cleanup_expected_epoch = expected_binding_epoch
        requested = set(stop_run_ids)
        if installation.binding_id is not None:
            binding = self.registry.get_binding(installation.binding_id)
            if (
                expected_binding_epoch is not None
                and expected_binding_epoch != binding.binding_epoch
            ):
                raise PluginError("stale_epoch", "uninstall inventory is stale")
            remaining = set(binding.active_run_ids).difference(requested)
            if remaining:
                return _receipt(
                    operation_id=new_id("plugin_uninstall"),
                    installation_id=installation_id,
                    state="blocked",
                    binding_epoch=binding.binding_epoch,
                    inventory_revision=installation.inventory_revision,
                    cleanup=(
                        CleanupItem(
                            resource_id=f"run_{run}",
                            outcome="blocked",
                            reason="active_run",
                            blocker_ids=(run,),
                        )
                        for run in sorted(remaining)
                    ),
                    ack="failed",
                )
        self._lifecycle_fences.add(installation_id)
        if not await self._quiesce_calls(installation_id):
            failed_binding = (
                self.registry.begin_disable(binding.binding_id, stop_run_ids=requested)
                if binding is not None
                else None
            )
            if failed_binding is not None:
                self.registry.complete_disable(failed_binding.binding_id, restart_required=True)
            else:
                self.registry.mark_failed(installation_id, "restart_required")
            current = self.registry.get_installation(installation_id)
            binding_epoch = (
                failed_binding.binding_epoch
                if failed_binding is not None
                else current.binding_epoch
            )
            return _receipt(
                operation_id=new_id("plugin_uninstall"),
                installation_id=installation_id,
                state="restart_required",
                binding_epoch=binding_epoch,
                inventory_revision=current.inventory_revision,
                ack="failed",
            )
        if binding is not None:
            for lease in tuple(self.registry.state.runs):
                if (
                    lease.binding_id == binding.binding_id
                    and lease.run_id in requested
                    and lease.status == "active"
                ):
                    self.registry.release_run(lease.lease_id, status="cancelled")
            binding = self.registry.begin_disable(binding.binding_id)
            cleanup_expected_epoch = binding.binding_epoch
        slot = self._engines.get(installation_id)
        if slot is not None:
            cleanup_hook = getattr(slot.implementation, "cleanup", None)
            if cleanup_hook is not None:
                cleanup_finished = await self._run_cleanup_hook(
                    installation_id,
                    cleanup_hook,
                    min(slot.limits.timeout_seconds, self._CALL_QUIESCE_TIMEOUT_SECONDS),
                )
                if not cleanup_finished:
                    self.registry.mark_failed(installation_id, "restart_required")
                    current = self.registry.get_installation(installation_id)
                    binding_epoch = (
                        self.registry.get_binding(binding.binding_id).binding_epoch
                        if binding is not None
                        else current.binding_epoch
                    )
                    return _receipt(
                        operation_id=new_id("plugin_uninstall"),
                        installation_id=installation_id,
                        state="restart_required",
                        binding_epoch=binding_epoch,
                        inventory_revision=current.inventory_revision,
                        ack="failed",
                    )
                # Hook failures do not authorize deleting unknown resources;
                # ``_run_cleanup_hook`` consumes them and the Host inventory
                # remains authoritative.
            if not await self._close_engine(installation_id, slot):
                if installation.binding_id is not None:
                    self.registry.mark_failed(installation_id, "restart_required")
                current = self.registry.get_installation(installation_id)
                binding_epoch = (
                    self.registry.get_binding(binding.binding_id).binding_epoch
                    if binding is not None
                    else current.binding_epoch
                )
                return _receipt(
                    operation_id=new_id("plugin_uninstall"),
                    installation_id=installation_id,
                    state="restart_required",
                    binding_epoch=binding_epoch,
                    inventory_revision=current.inventory_revision,
                    ack="failed",
                )
            self._engines.pop(installation_id, None)
        plan = self.registry.begin_uninstall(
            installation_id,
            data_policy=data_policy,
            stop_run_ids=stop_run_ids,
            expected_binding_epoch=cleanup_expected_epoch,
        )
        await self._apply_cleanup(plan)
        try:
            completed = self.registry.complete_uninstall(plan.operation_id)
        except PluginError:
            plan = self.registry.cleanup_plan(plan.operation_id)
            self._lifecycle_fences.discard(installation_id)
            return _receipt(
                operation_id=plan.operation_id,
                installation_id=installation_id,
                state="blocked",
                binding_epoch=plan.expected_binding_epoch,
                inventory_revision=plan.inventory_revision,
                cleanup=plan.items,
                ack="failed",
            )
        self._remove_empty_installation_root(installation_id)
        self._lifecycle_fences.discard(installation_id)
        return _receipt(
            operation_id=completed.operation_id,
            installation_id=installation_id,
            state="uninstalled",
            binding_epoch=plan.expected_binding_epoch + 1,
            inventory_revision=plan.inventory_revision,
            cleanup=completed.items,
        )

    async def _apply_cleanup(self, plan: CleanupPlanRecord) -> CleanupPlanRecord:
        # Child resources are removed first so a parent directory can be
        # deleted without relying on plugin cleanup hooks.
        resources = {
            item.resource.resource_id: item
            for item in self.registry.resources_for(plan.installation_id)
        }

        def depth(item: CleanupItem) -> int:
            stored = resources.get(item.resource_id)
            return len(stored.relative_path.split("/")) if stored is not None else 0

        ordered = sorted(plan.items, key=depth, reverse=True)
        current = plan
        for item in ordered:
            if item.outcome != "pending" and not (
                item.outcome == "blocked"
                and item.reason in {"directory_identity_changed", "retry_required"}
            ):
                continue
            stored = resources.get(item.resource_id)
            if stored is None:
                current = self.registry.mark_resource_cleanup(
                    current.operation_id,
                    item.resource_id,
                    item.model_copy(update={"outcome": "deleted", "reason": "exclusive"}),
                )
                continue
            try:
                path = path_under(
                    self.registry.installation_root(plan.installation_id), stored.relative_path
                )
                self._remove_owned_path(path, stored.path_kind)
                current = self.registry.mark_resource_cleanup(
                    current.operation_id,
                    item.resource_id,
                    item.model_copy(update={"outcome": "deleted", "reason": "exclusive"}),
                )
            except Exception as exc:
                reason = (
                    "directory_identity_changed"
                    if isinstance(exc, PermissionDeniedError)
                    else "retry_required"
                )
                current = self.registry.mark_resource_cleanup(
                    current.operation_id,
                    item.resource_id,
                    item.model_copy(
                        update={"outcome": "blocked", "reason": reason, "blocker_ids": ()}
                    ),
                )
        return current

    @staticmethod
    def _remove_owned_path(path: Path, path_kind: str) -> None:
        _remove_managed_path(path, directory=path_kind == "directory")

    def _remove_empty_installation_root(self, installation_id: str) -> None:
        root = self.registry.installation_root(installation_id)
        if not root.exists() or root.is_symlink():
            return
        from contextlib import suppress

        with suppress(OSError, PluginError):
            _remove_empty_managed_directory(root)

    async def resume_cleanup(self) -> tuple[LifecycleReceipt, ...]:
        """Resume Host-owned pending plans without starting plugin code."""

        receipts: list[LifecycleReceipt] = []
        for plan in self.registry.pending_cleanup_plans():
            if not any(
                item.outcome == "pending"
                or (
                    item.outcome == "blocked"
                    and item.reason in {"directory_identity_changed", "retry_required"}
                )
                for item in plan.items
            ):
                continue
            current = await self._apply_cleanup(plan)
            try:
                completed = self.registry.complete_uninstall(current.operation_id)
            except PluginError:
                current = self.registry.cleanup_plan(current.operation_id)
                receipts.append(
                    _receipt(
                        operation_id=current.operation_id,
                        installation_id=current.installation_id,
                        state="blocked",
                        binding_epoch=current.expected_binding_epoch,
                        inventory_revision=current.inventory_revision,
                        cleanup=current.items,
                        ack="failed",
                    )
                )
                self._lifecycle_fences.discard(current.installation_id)
                continue
            self._remove_empty_installation_root(completed.installation_id)
            self._lifecycle_fences.discard(completed.installation_id)
            receipts.append(
                _receipt(
                    operation_id=completed.operation_id,
                    installation_id=completed.installation_id,
                    state="uninstalled",
                    binding_epoch=completed.expected_binding_epoch + 1,
                    inventory_revision=completed.inventory_revision,
                    cleanup=completed.items,
                )
            )
        return tuple(receipts)

    async def close(self) -> None:
        if self._closed and not self._engines:
            return
        self._closed = True
        for installation_id in tuple(self._engines):
            runs = tuple(
                lease.run_id
                for lease in self.registry.state.runs
                if lease.installation_id == installation_id and lease.status == "active"
            )
            await self.stop(installation_id, stop_run_ids=runs)
        if self._engines:
            raise RestartRequiredError("Host shutdown retained in-flight plugin code")
        for lease in tuple(self.registry.state.runs):
            if lease.status == "active":
                self.registry.release_run(lease.lease_id, status="cancelled")
        self._isolation_evidence.clear()
        self.registry.close()


__all__ = ["PluginFactory", "PluginHost"]
