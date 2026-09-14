"""Host-side authorization for direct memory-plugin engine payloads.

The MP-0 contracts validate the shape of a payload.  They intentionally do
not decide whether a plugin may read a source, observe a memory head, or
publish a proposal.  ``PluginHost`` should call the two public functions in
this module after its lease/context check and before accepting a plugin
request or result.

The callbacks in :class:`PayloadAuthorizers` are Core-owned.  A plugin-supplied
``SourceRef`` or ``MemoryVersionRef`` is only a claim to be checked by Core;
matching fields in the claim are never sufficient on their own.  Every
callback must return exactly ``True``.  Missing callbacks, callback failures,
and non-boolean results fail closed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar, cast

from pydantic import BaseModel

from operant.contracts.b2_1 import (
    CandidateBatch,
    IndexEvent,
    IndexReceipt,
    MaintenanceInput,
    MemoryHead,
    MemoryProposal,
    MemoryVersionRef,
    ProposalBatch,
    RecallRequest,
    RpcContext,
    SourceBatch,
    SourceRef,
)

SourceAuthorizer = Callable[[RpcContext, SourceRef], bool]
MemoryRefAuthorizer = Callable[[RpcContext, MemoryVersionRef], bool]
HeadAuthorizer = Callable[[RpcContext, MemoryHead], bool]
ProposalAuthorizer = Callable[[RpcContext, MemoryProposal], bool]

RequestT = TypeVar("RequestT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)
ValueT = TypeVar("ValueT")


@dataclass(frozen=True, slots=True)
class PayloadAuthorizers:
    """Core callbacks used to authorize direct engine payload values.

    ``authorize_source`` and ``authorize_memory_ref`` have the same narrow
    callback shape as the existing ``HostCallbacks`` authorizers.  The two
    additional callbacks are needed because MP-0 has no authoritative Core
    lookup embedded in ``MemoryHead`` or ``MemoryProposal``.

    ``authorize_source`` must resolve the source by its immutable identity and
    verify the current dataset, scope grant, revision/digest, permission epoch
    and availability.  ``authorize_memory_ref`` must resolve the exact dataset,
    record, version and digest and apply the context's current scope/epoch
    grant.  ``authorize_head`` must resolve the exact record head and verify
    dataset, scope visibility, current permission epoch and the head revision
    or state.  ``authorize_proposal`` must verify dataset ownership/principal,
    target record, base-head CAS facts and every proposal-side grant.  These
    callbacks must query Core records rather than trusting fields supplied by
    the plugin.

    The two new callback names are ``authorize_head`` and
    ``authorize_proposal``.  The owner should add these exact fields to the
    HostCallbacks-like integration object.
    """

    authorize_source: SourceAuthorizer | None = None
    authorize_memory_ref: MemoryRefAuthorizer | None = None
    authorize_head: HeadAuthorizer | None = None
    authorize_proposal: ProposalAuthorizer | None = None

    @classmethod
    def from_host_callbacks(cls, callbacks: object | None) -> PayloadAuthorizers:
        """Copy authorization callables from a HostCallbacks-like object.

        This deliberately uses attribute lookup instead of importing
        ``HostCallbacks``.  It keeps this helper independent of the protocol
        adapter and lets the owner add the two new Core callback fields there
        without changing the frozen MP-0 contracts.
        """

        if callbacks is None:
            return cls()
        return cls(
            authorize_source=_callback(callbacks, "authorize_source"),
            authorize_memory_ref=_callback(callbacks, "authorize_memory_ref"),
            authorize_head=_callback(callbacks, "authorize_head"),
            authorize_proposal=_callback(callbacks, "authorize_proposal"),
        )


_REQUEST_TYPES: dict[str, type[BaseModel]] = {
    "extract": SourceBatch,
    "recall": RecallRequest,
    "maintain": MaintenanceInput,
    "on_index_event": IndexEvent,
}

_RESULT_TYPES: dict[str, type[BaseModel]] = {
    "extract": ProposalBatch,
    "recall": CandidateBatch,
    "maintain": ProposalBatch,
    "on_index_event": IndexReceipt,
}


def allowed_host_operations(capabilities: tuple[str, ...]) -> frozenset[str]:
    """Map MP-0 engine capabilities to the narrow nested Host APIs.

    Data authorizers and configured model profiles still apply independently.
    Index notifications alone grant no source/search/model access.
    """

    allowed: set[str] = set()
    if {"extract", "recall", "maintain"}.intersection(capabilities):
        allowed.add("read_source")
    if "recall" in capabilities:
        allowed.add("search")
    if {"extract", "recall"}.intersection(capabilities):
        allowed.add("model")
    return frozenset(allowed)


def authorize_engine_request(
    operation: str,
    request: RequestT,
    expected_context: RpcContext,
    authorizers: PayloadAuthorizers | object | None = None,
) -> RequestT:
    """Authorize one direct engine request and return the same typed object.

    ``expected_context`` is the Host context already checked against the
    active lease.  Requiring it at this boundary prevents a caller from using
    the request's self-asserted context as its only source of authority.
    ``PluginHost`` should invoke this after schema validation and lease
    fencing, before handing the request to an engine.

    ``MaintenanceInput`` has no source or head payload beyond its context, so
    the Host's lease/context check is the authority for that operation.  The
    other three operations require the corresponding Core authorizer for each
    data-bearing value.
    """

    _check_request_shape(operation, request, expected_context)
    callbacks = _coerce_authorizers(authorizers)
    context = expected_context

    if isinstance(request, SourceBatch):
        _authorize_sources(context, request.sources, callbacks)
    elif isinstance(request, RecallRequest):
        _authorize_memory_refs(context, request.explicit_refs, callbacks)
    elif isinstance(request, IndexEvent):
        _authorize_head(context, request.head, callbacks)
    elif not isinstance(request, MaintenanceInput):
        # _check_request_shape above makes this unreachable unless the
        # operation/type mapping is changed without updating this module.
        _protocol("engine request type is not handled")
    return request


def authorize_engine_result(
    operation: str,
    request: BaseModel,
    result: ResultT,
    expected_context: RpcContext,
    authorizers: PayloadAuthorizers | object | None = None,
) -> ResultT:
    """Authorize one direct engine result and return the same typed object.

    Besides data authorization, the result is tied to the triggering request:
    Candidate/Proposal batches must carry the request ID, Index receipts must
    carry the event ID, and extract/maintain proposals must echo the input
    source watermark.  A mismatch is a protocol error; unauthorized payload
    values are permission errors.
    """

    _check_request_shape(operation, request, expected_context)
    _check_result_shape(operation, result)
    callbacks = _coerce_authorizers(authorizers)
    context = expected_context

    # Re-check input claims after the engine returns.  Core revocation or a
    # grant change may happen while an in-process plugin is running; a result
    # must not be accepted merely because the input was authorized at start.
    if isinstance(request, SourceBatch):
        _authorize_sources(context, request.sources, callbacks)
    elif isinstance(request, RecallRequest):
        _authorize_memory_refs(context, request.explicit_refs, callbacks)
    elif isinstance(request, IndexEvent):
        _authorize_head(context, request.head, callbacks)

    if isinstance(request, (SourceBatch, MaintenanceInput)):
        if not isinstance(result, ProposalBatch):
            _protocol("engine result type is not handled")
        if result.request_id != context.request_id:
            _protocol("proposal batch is bound to another request")
        if result.source_watermark != request.source_watermark:
            _protocol("proposal batch watermark is not bound to the request")
        _authorize_proposals(context, result.proposals, callbacks)
    elif isinstance(request, RecallRequest):
        if not isinstance(result, CandidateBatch):
            _protocol("engine result type is not handled")
        if result.request_id != context.request_id:
            _protocol("candidate batch is bound to another request")
        _authorize_memory_refs(
            context,
            (candidate.ref for candidate in result.candidates),
            callbacks,
        )
    elif isinstance(request, IndexEvent):
        if not isinstance(result, IndexReceipt):
            _protocol("engine result type is not handled")
        if result.event_id != request.event_id:
            _protocol("index receipt is bound to another event")
    else:
        _protocol("engine request type is not handled")
    return result


def _check_request_shape(
    operation: str,
    request: BaseModel,
    expected_context: RpcContext,
) -> None:
    expected_type = _REQUEST_TYPES.get(operation)
    if expected_type is None:
        _protocol("unsupported direct engine operation")
    if not isinstance(request, expected_type):
        _protocol("engine request has an unexpected type")
    context = getattr(request, "context", None)
    if not isinstance(context, RpcContext):
        _protocol("engine request does not carry RpcContext")
    if not isinstance(expected_context, RpcContext):
        _protocol("Host expected context is invalid")
    if context != expected_context:
        _stale("engine request context does not match the active Host context")


def _check_result_shape(operation: str, result: BaseModel) -> None:
    expected_type = _RESULT_TYPES.get(operation)
    if expected_type is None:
        _protocol("unsupported direct engine operation")
    if not isinstance(result, expected_type):
        _protocol("engine result has an unexpected type")


def _coerce_authorizers(
    authorizers: PayloadAuthorizers | object | None,
) -> PayloadAuthorizers:
    if isinstance(authorizers, PayloadAuthorizers):
        return authorizers
    return PayloadAuthorizers.from_host_callbacks(authorizers)


def _callback(callbacks: object, *names: str) -> Callable[..., Any] | None:
    for name in names:
        candidate = getattr(callbacks, name, None)
        if callable(candidate):
            return cast(Callable[..., Any], candidate)
    return None


def _authorize_sources(
    context: RpcContext,
    sources: Any,
    callbacks: PayloadAuthorizers,
) -> None:
    for source in sources:
        if not isinstance(source, SourceRef):
            _protocol("engine source payload has an unexpected type")
        if (
            source.scope != context.scope
            or source.permission_epoch != context.permission_epoch
            or source.availability != "available"
        ):
            _deny("source scope, permission epoch, or availability is not authorized")
        _call_authorizer(callbacks.authorize_source, context, source, "source")


def _authorize_memory_refs(
    context: RpcContext,
    refs: Any,
    callbacks: PayloadAuthorizers,
) -> None:
    for ref in refs:
        if not isinstance(ref, MemoryVersionRef):
            _protocol("engine memory reference has an unexpected type")
        if ref.dataset_id != context.dataset_id:
            _deny("memory reference belongs to another dataset")
        _call_authorizer(
            callbacks.authorize_memory_ref,
            context,
            ref,
            "memory reference",
        )


def _authorize_head(
    context: RpcContext,
    head: MemoryHead,
    callbacks: PayloadAuthorizers,
) -> None:
    if not isinstance(head, MemoryHead):
        _protocol("engine head payload has an unexpected type")
    _check_head_claim(head)
    if head.dataset_id != context.dataset_id:
        _deny("memory head belongs to another dataset")
    if head.permission_epoch != context.permission_epoch:
        _deny("memory head belongs to another permission epoch")
    _call_authorizer(callbacks.authorize_head, context, head, "memory head")


def _authorize_proposals(
    context: RpcContext,
    proposals: Any,
    callbacks: PayloadAuthorizers,
) -> None:
    for proposal in proposals:
        if not isinstance(proposal, MemoryProposal):
            _protocol("engine proposal payload has an unexpected type")
        owner = proposal.owner
        if (
            owner.dataset_id != context.dataset_id
            or owner.owner_namespace != f"dataset:{context.dataset_id}"
            or owner.kind != "plugin_dataset"
            or proposal.base_head.dataset_id != context.dataset_id
            or proposal.proposed_version.dataset_id != context.dataset_id
            or proposal.base_head.record_id != proposal.proposed_version.record_id
        ):
            _deny("proposal ownership or dataset is not authorized")
        _check_head_claim(proposal.base_head)
        if proposal.base_head.permission_epoch != context.permission_epoch:
            _deny("proposal base head belongs to another permission epoch")
        _authorize_sources(context, proposal.source_refs, callbacks)
        _call_authorizer(callbacks.authorize_proposal, context, proposal, "proposal")


def _check_head_claim(head: MemoryHead) -> None:
    """Recheck MP-0 head invariants for trusted plugins using ``model_copy``."""

    published = head.published_version
    if published is not None and (
        published.dataset_id != head.dataset_id or published.record_id != head.record_id
    ):
        _protocol("memory head published pointer is inconsistent")
    if head.state == "published" and published is None:
        _protocol("published memory head has no exact version")


def _call_authorizer(
    callback: Callable[[RpcContext, ValueT], bool] | None,
    context: RpcContext,
    value: ValueT,
    label: str,
) -> None:
    if callback is None:
        _deny(f"Core {label} authorizer is not configured")
    try:
        decision = callback(context, value)
    except Exception as exc:
        _deny_from_exception(f"Core {label} authorizer failed", exc)
    if decision is not True:
        _deny(f"Core {label} authorizer did not authorize the value")


def _deny(reason: str) -> NoReturn:
    from operant.plugins.protocol import PermissionDeniedError

    raise PermissionDeniedError(f"plugin payload is not authorized: {reason}")


def _deny_from_exception(reason: str, cause: Exception) -> NoReturn:
    from operant.plugins.protocol import PermissionDeniedError

    raise PermissionDeniedError(f"plugin payload is not authorized: {reason}") from cause


def _stale(reason: str) -> NoReturn:
    from operant.plugins.protocol import StaleEpochError

    raise StaleEpochError(reason)


def _protocol(reason: str) -> NoReturn:
    from operant.plugins.protocol import PluginProtocolError

    raise PluginProtocolError(reason)


__all__ = [
    "HeadAuthorizer",
    "MemoryRefAuthorizer",
    "PayloadAuthorizers",
    "ProposalAuthorizer",
    "SourceAuthorizer",
    "authorize_engine_request",
    "authorize_engine_result",
]
