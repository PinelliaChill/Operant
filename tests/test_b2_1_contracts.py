from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from operant.contracts import b2_1 as c
from sdk.protocol.generate_b2_1 import generate

FIXTURE = Path("tests/fixtures/b2_1_contract/contracts.json")


def fixtures() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())  # type: ignore[no-any-return]


def test_contract_fixtures_are_typed_and_round_trip() -> None:
    for model_name, values in fixtures().items():
        model = getattr(c, model_name)
        for value in values:
            parsed = model.model_validate(value)
            assert model.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("kind", ["global", "project", "unknown", "workspace:*"])
def test_unknown_or_implicit_scope_fails_closed(kind: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(c.Scope).validate_python({"kind": kind})


def test_scope_does_not_accept_path_or_infer_missing_identity() -> None:
    scope = {"kind": "workspace", "project_id": "project-a", "workspace_id": "workspace-a"}
    with pytest.raises(ValidationError):
        TypeAdapter(c.Scope).validate_python({**scope, "path": "/tmp/same-repo"})
    del scope["workspace_id"]
    with pytest.raises(ValidationError):
        TypeAdapter(c.Scope).validate_python(scope)


def test_cursor_is_lossless_but_stays_within_sqlite_int64() -> None:
    adapter = TypeAdapter(c.Cursor)
    assert adapter.validate_python(str(2**63 - 1)) == str(2**63 - 1)
    for value in (str(2**63), "01", "-1", "1.0"):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_unknown_or_mismatched_dataset_owner_is_rejected() -> None:
    owner = fixtures()["DatasetOwner"][0]
    for bad in ({**owner, "kind": "core"}, {**owner, "dataset_id": "dataset-other"}):
        with pytest.raises(ValidationError):
            c.DatasetOwner.model_validate(bad)


def test_dataset_copy_is_distinct_from_installation_transfer() -> None:
    request = {
        "dataset_id": "data-a",
        "destination_dataset_id": "data-a",
        "expected_revision": 2,
        "from_namespace": "dataset:data-a",
        "to_namespace": "dataset:data-a",
        "destination_installation_id": "install-b",
        "authorization_grant_id": "grant-a",
        "mode": "transfer",
        "idempotency_key": "transfer-a",
    }
    assert c.DatasetTransfer.model_validate(request).dataset_id == "data-a"
    with pytest.raises(ValidationError):
        c.DatasetTransfer.model_validate({**request, "mode": "authorized_copy"})
    copied = c.DatasetTransfer.model_validate(
        {
            **request,
            "mode": "authorized_copy",
            "destination_dataset_id": "data-b",
            "to_namespace": "dataset:data-b",
        }
    )
    assert copied.destination_dataset_id != copied.dataset_id


def test_uninstall_requires_explicit_policy_and_inventory_and_has_bounded_cleanup() -> None:
    request = fixtures()["UninstallRequest"][0]
    for field in ("data_policy", "inventory_revision", "expected_binding_epoch", "idempotency_key"):
        bad = {key: value for key, value in request.items() if key != field}
        with pytest.raises(ValidationError):
            c.UninstallRequest.model_validate(bad)
    with pytest.raises(ValidationError):
        c.UninstallRequest.model_validate({**request, "data_policy": "purge_home"})
    receipt = c.LifecycleReceipt.model_validate(fixtures()["LifecycleReceipt"][0])
    assert {item.outcome for item in receipt.cleanup} >= {"blocked", "retained", "deleted"}
    assert receipt.state == "blocked"


def test_current_admission_evidence_is_required_in_both_modes() -> None:
    eligible = fixtures()["HostAdmission"][0]
    with pytest.raises(ValidationError, match="certification_invalid"):
        c.HostAdmission.model_validate({**eligible, "certification_id": None})
    with pytest.raises(ValidationError, match="isolation_unavailable"):
        c.HostAdmission.model_validate({**eligible, "mode": "isolated"})


def test_global_disable_cannot_be_overridden_in_enabled_binding() -> None:
    binding = fixtures()["MemoryEnabled"][0]
    with pytest.raises(ValidationError):
        c.MemoryEnabled.model_validate({**binding, "global_enabled": False})


def test_published_head_cannot_point_to_another_record_or_missing_version() -> None:
    head = fixtures()["MemoryHead"][0]
    for ref in (None, {**head["published_version"], "record_id": "other"}):
        with pytest.raises(ValidationError):
            c.MemoryHead.model_validate({**head, "published_version": ref})


def test_memory_pack_covers_explicit_refs_with_one_budget_and_dataset() -> None:
    pack = fixtures()["MemoryPack"][0]
    with pytest.raises(ValidationError, match="budget_exceeded"):
        c.MemoryPack.model_validate({**pack, "token_count": pack["total_token_budget"] + 1})
    foreign = copy.deepcopy(pack)
    foreign["selected"][0]["dataset_id"] = "foreign"
    with pytest.raises(ValidationError, match="permission_denied"):
        c.MemoryPack.model_validate(foreign)


def test_rpc_requires_epoch_deadline_cancel_and_idempotency() -> None:
    context = fixtures()["RpcContext"][0]
    for field in ("deadline", "cancel_token", "binding_epoch", "permission_epoch", "lease_fencing"):
        with pytest.raises(ValidationError):
            c.RpcContext.model_validate(
                {key: value for key, value in context.items() if key != field}
            )
    with pytest.raises(ValidationError):
        c.RpcContext.model_validate({**context, "sdk_version": c.APP_CONTRACT_VERSION})


def test_generation_is_deterministic_and_does_not_change_live_protocol(tmp_path: Path) -> None:
    frozen_paths = sorted(Path("sdk/protocol/schema").glob("*.openapi.*"))
    frozen_before = {str(path): path.read_bytes() for path in frozen_paths}
    first, second = tmp_path / "first", tmp_path / "second"
    assert generate(first) == generate(second)
    generated = sorted(first.rglob("*"))
    for path in generated:
        if not path.is_file():
            continue
        relative = path.relative_to(first)
        assert path.read_bytes() == (second / relative).read_bytes()
        assert path.read_bytes() == relative.read_bytes(), f"stale generated file: {relative}"
    assert {str(path): path.read_bytes() for path in frozen_paths} == frozen_before
    versions = set()
    for path in (first / "sdk/protocol/schema").glob("*.json"):
        doc = json.loads(path.read_text())
        versions.add(doc["title"])
        assert "paths" not in doc
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest() in path.with_suffix(".sha256").read_text()
        )
        for model in doc["$defs"].values():
            assert model.get("additionalProperties") is False
    assert versions == {c.APP_CONTRACT_VERSION, c.PLUGIN_SDK_VERSION}
    for path in (first / "sdk/python_client").glob("*.py"):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec and spec.loader
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_task_source_does_not_admit_assignment_message_as_board_task() -> None:
    for source_type in ("session", "workflow_run", "team_task"):
        assert c.TaskSource(source_type=source_type, source_id="same-id")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        c.TaskSource.model_validate({"source_type": "TaskAssignment", "source_id": "message-1"})
