from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from operant.contracts.b2_1 import (
    CandidateBatch,
    CandidateReference,
    DatasetOwner,
    IndexEvent,
    IndexReceipt,
    MaintenanceInput,
    MemoryHead,
    MemoryProposal,
    MemoryVersionRef,
    ProposalBatch,
    RecallRequest,
    RpcContext,
    RunScope,
    SourceBatch,
    SourceRef,
)
from operant.domain.models import utc_now
from operant.plugins.payload import (
    PayloadAuthorizers,
    authorize_engine_request,
    authorize_engine_result,
)
from operant.plugins.protocol import PluginError


@dataclass(frozen=True)
class PayloadFixture:
    context: RpcContext
    source: SourceRef
    memory_ref: MemoryVersionRef
    head: MemoryHead
    proposal: MemoryProposal
    extract: SourceBatch
    recall: RecallRequest
    maintain: MaintenanceInput
    index_event: IndexEvent
    authorizers: PayloadAuthorizers


def _scope(run_id: str = "run-1") -> RunScope:
    return RunScope(
        kind="run",
        project_id="project-1",
        workspace_id="workspace-1",
        run_id=run_id,
        writer_id=None,
    )


@pytest.fixture
def payload_fixture(tmp_path: Path) -> PayloadFixture:
    # Keep this boundary test independent from a real Core/database.  The
    # temporary directory documents that all fixtures are local to one test.
    (tmp_path / "payload-fixture").mkdir()
    scope = _scope()
    context = RpcContext(
        sdk_version="operant-memory-sdk.v1",
        request_id="request-1",
        installation_id="installation-1",
        dataset_id="dataset-1",
        scope=scope,
        deadline=utc_now() + timedelta(minutes=1),
        cancel_token="cancel-1",
        idempotency_key="idempotency-1",
        request_digest="a" * 64,
        binding_epoch=4,
        permission_epoch=7,
        lease_fencing=9,
    )
    source = SourceRef(
        source_type="item",
        source_id="source-1",
        revision=3,
        content_digest="b" * 64,
        scope=scope,
        permission_epoch=context.permission_epoch,
        availability="available",
    )
    memory_ref = MemoryVersionRef(
        dataset_id=context.dataset_id,
        record_id="record-1",
        version=2,
        content_digest="c" * 64,
    )
    head = MemoryHead(
        dataset_id=context.dataset_id,
        record_id=memory_ref.record_id,
        published_version=memory_ref,
        revision=5,
        publication_cursor="12",
        state="published",
        permission_epoch=context.permission_epoch,
    )
    owner = DatasetOwner(
        kind="plugin_dataset",
        owner_namespace=f"dataset:{context.dataset_id}",
        dataset_id=context.dataset_id,
        principal_id="plugin-principal",
    )
    proposal = MemoryProposal(
        proposal_id="proposal-1",
        proposal_revision=1,
        owner=owner,
        operation="modify",
        base_head=head,
        proposed_version=memory_ref,
        source_refs=(source,),
        extractor_version="extractor-v1",
        reason="fixture proposal",
        state="pending",
    )
    extract = SourceBatch(context=context, sources=(source,), source_watermark="12")
    recall = RecallRequest(
        context=context,
        query="fixture",
        explicit_refs=(memory_ref,),
        knowledge_cutoff="12",
        max_candidates=5,
        token_budget=100,
    )
    maintain = MaintenanceInput(
        context=context,
        job_id="job-1",
        action="reindex",
        source_watermark="12",
        budget_tokens=100,
    )
    index_event = IndexEvent(context=context, event_id="event-1", head=head)
    return PayloadFixture(
        context=context,
        source=source,
        memory_ref=memory_ref,
        head=head,
        proposal=proposal,
        extract=extract,
        recall=recall,
        maintain=maintain,
        index_event=index_event,
        authorizers=PayloadAuthorizers(
            authorize_source=lambda current, candidate: candidate == source,
            authorize_memory_ref=lambda current, candidate: candidate == memory_ref,
            authorize_head=lambda current, candidate: candidate == head,
            authorize_proposal=lambda current, candidate: candidate == proposal,
        ),
    )


def test_authorizes_all_four_engine_operations(payload_fixture: PayloadFixture) -> None:
    fixture = payload_fixture
    for operation, request in (
        ("extract", fixture.extract),
        ("recall", fixture.recall),
        ("maintain", fixture.maintain),
        ("on_index_event", fixture.index_event),
    ):
        assert (
            authorize_engine_request(
                operation,
                request,
                fixture.context,
                fixture.authorizers,
            )
            is request
        )

    assert (
        authorize_engine_result(
            "extract",
            fixture.extract,
            ProposalBatch(
                request_id=fixture.context.request_id,
                proposals=(fixture.proposal,),
                source_watermark=fixture.extract.source_watermark,
            ),
            fixture.context,
            fixture.authorizers,
        ).proposals[0]
        == fixture.proposal
    )
    assert (
        authorize_engine_result(
            "recall",
            fixture.recall,
            CandidateBatch(
                request_id=fixture.context.request_id,
                candidates=(CandidateReference(ref=fixture.memory_ref, score=0.9),),
            ),
            fixture.context,
            fixture.authorizers,
        )
        .candidates[0]
        .ref
        == fixture.memory_ref
    )
    assert (
        authorize_engine_result(
            "maintain",
            fixture.maintain,
            ProposalBatch(
                request_id=fixture.context.request_id,
                proposals=(fixture.proposal,),
                source_watermark=fixture.maintain.source_watermark,
            ),
            fixture.context,
            fixture.authorizers,
        ).proposals[0]
        == fixture.proposal
    )
    receipt = IndexReceipt(
        event_id=fixture.index_event.event_id,
        index_generation="generation-1",
        applied_cursor="12",
        outcome="applied",
    )
    assert (
        authorize_engine_result(
            "on_index_event",
            fixture.index_event,
            receipt,
            fixture.context,
            fixture.authorizers,
        )
        is receipt
    )


@pytest.mark.parametrize(
    ("operation", "request_name", "authorizer"),
    [
        ("extract", "scope", "source"),
        ("extract", "epoch", "source"),
        ("recall", "dataset", "memory_ref"),
        ("on_index_event", "dataset", "head"),
        ("on_index_event", "epoch", "head"),
    ],
)
def test_input_payload_cross_boundary_is_denied(
    payload_fixture: PayloadFixture,
    operation: str,
    request_name: str,
    authorizer: str,
) -> None:
    fixture = payload_fixture
    if operation == "extract":
        updated_source = fixture.source.model_copy(
            update={
                "scope": _scope("other-run") if request_name == "scope" else fixture.source.scope,
                "permission_epoch": fixture.context.permission_epoch + 1,
            }
        )
        request = fixture.extract.model_copy(update={"sources": (updated_source,)})
    elif operation == "recall":
        request = fixture.recall.model_copy(
            update={
                "explicit_refs": (
                    fixture.memory_ref.model_copy(update={"dataset_id": "dataset-foreign"}),
                )
            }
        )
    else:
        foreign_ref = fixture.memory_ref.model_copy(update={"dataset_id": "dataset-foreign"})
        head_update: dict[str, object] = {
            "permission_epoch": fixture.context.permission_epoch + 1,
        }
        if request_name == "dataset":
            head_update.update(
                {
                    "dataset_id": "dataset-foreign",
                    "record_id": foreign_ref.record_id,
                    "published_version": foreign_ref,
                }
            )
        updated_head = fixture.head.model_copy(update=head_update)
        request = fixture.index_event.model_copy(update={"head": updated_head})

    calls: list[str] = []
    original = getattr(fixture.authorizers, f"authorize_{authorizer}")

    def recording_authorizer(current: RpcContext, candidate: object) -> bool:
        calls.append(authorizer)
        return bool(original(current, candidate))

    callbacks = fixture.authorizers.__class__(
        authorize_source=recording_authorizer
        if authorizer == "source"
        else fixture.authorizers.authorize_source,
        authorize_memory_ref=recording_authorizer
        if authorizer == "memory_ref"
        else fixture.authorizers.authorize_memory_ref,
        authorize_head=recording_authorizer
        if authorizer == "head"
        else fixture.authorizers.authorize_head,
        authorize_proposal=fixture.authorizers.authorize_proposal,
    )
    with pytest.raises(PluginError) as error:
        authorize_engine_request(operation, request, fixture.context, callbacks)
    assert error.value.code == "permission_denied"
    # Local scope/epoch/dataset checks run before Core is asked to authorize
    # the claimed value.
    assert calls == []


def test_maintain_context_dataset_mismatch_is_stale(
    payload_fixture: PayloadFixture,
) -> None:
    fixture = payload_fixture
    request = fixture.maintain.model_copy(
        update={"context": fixture.context.model_copy(update={"dataset_id": "dataset-foreign"})}
    )
    with pytest.raises(PluginError) as error:
        authorize_engine_request("maintain", request, fixture.context, fixture.authorizers)
    assert error.value.code == "stale_epoch"


@pytest.mark.parametrize("operation", ["extract", "recall", "maintain", "on_index_event"])
def test_missing_authorizer_fails_closed_for_nonempty_payload(
    payload_fixture: PayloadFixture,
    operation: str,
) -> None:
    fixture = payload_fixture
    if operation == "extract":
        request = fixture.extract
    elif operation == "recall":
        request = fixture.recall
    elif operation == "maintain":
        request = fixture.maintain
    else:
        request = fixture.index_event
    if operation == "maintain":
        # MaintenanceInput carries no source/head claim.  Its complete
        # context is already fenced by PluginHost._assert_active.
        assert (
            authorize_engine_request(operation, request, fixture.context, PayloadAuthorizers())
            is request
        )
        return
    with pytest.raises(PluginError) as error:
        authorize_engine_request(operation, request, fixture.context, PayloadAuthorizers())
    assert error.value.code == "permission_denied"


@pytest.mark.parametrize(
    "operation",
    ["extract", "recall", "maintain", "on_index_event"],
)
def test_result_correlation_and_payload_authority(
    payload_fixture: PayloadFixture,
    operation: str,
) -> None:
    fixture = payload_fixture
    if operation == "extract":
        request = fixture.extract
        foreign_ref = fixture.memory_ref.model_copy(update={"dataset_id": "dataset-foreign"})
        foreign_head = fixture.head.model_copy(
            update={
                "dataset_id": "dataset-foreign",
                "record_id": foreign_ref.record_id,
                "published_version": foreign_ref,
            }
        )
        foreign_owner = DatasetOwner(
            kind="plugin_dataset",
            owner_namespace="dataset:dataset-foreign",
            dataset_id="dataset-foreign",
            principal_id="plugin-principal",
        )
        foreign_proposal = MemoryProposal(
            proposal_id=fixture.proposal.proposal_id,
            proposal_revision=fixture.proposal.proposal_revision,
            owner=foreign_owner,
            operation=fixture.proposal.operation,
            base_head=foreign_head,
            proposed_version=foreign_ref,
            source_refs=(fixture.source,),
            extractor_version=fixture.proposal.extractor_version,
            reason=fixture.proposal.reason,
            state=fixture.proposal.state,
        )
        result: object = ProposalBatch(
            request_id=fixture.context.request_id,
            proposals=(foreign_proposal,),
            source_watermark=request.source_watermark,
        )
    elif operation == "maintain":
        request = fixture.maintain
        result = ProposalBatch(
            request_id=fixture.context.request_id,
            proposals=(
                fixture.proposal.model_copy(
                    update={
                        "base_head": fixture.proposal.base_head.model_copy(
                            update={"permission_epoch": fixture.context.permission_epoch + 1}
                        )
                    }
                ),
            ),
            source_watermark=request.source_watermark,
        )
    elif operation == "recall":
        request = fixture.recall
        result = CandidateBatch(
            request_id=fixture.context.request_id,
            candidates=(
                CandidateReference(
                    ref=fixture.memory_ref.model_copy(update={"dataset_id": "dataset-foreign"}),
                    score=1,
                ),
            ),
        )
    else:
        request = fixture.index_event
        result = IndexReceipt(
            event_id="event-foreign",
            index_generation="generation-1",
            applied_cursor="12",
            outcome="applied",
        )
    with pytest.raises(PluginError) as error:
        authorize_engine_result(
            operation,
            request,
            result,  # type: ignore[arg-type]
            fixture.context,
            fixture.authorizers,
        )
    assert error.value.code in {"permission_denied", "protocol_mismatch"}


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ((), set()),
        (("on_index_event",), set()),
        (("maintain",), {"read_source"}),
        (("extract",), {"read_source", "model"}),
        (("recall",), {"read_source", "search", "model"}),
    ],
)
def test_manifest_capabilities_bound_nested_host_operations(capabilities, expected):
    from operant.plugins.payload import allowed_host_operations

    assert allowed_host_operations(capabilities) == expected
