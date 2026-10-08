from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import operant.api_skill_sources as source_module
from operant.api_skill_sources import install_skill_source_routes
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine, balanced_policy_bundle
from operant.application.service import ApplicationService
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.actions import CommandExecution
from operant.domain.commands import WorkspaceInitialization
from operant.domain.security import (
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
)
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.onboarding import UXRepository
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore
from operant.protocol import canonical_action_hash
from operant.skills.sources import default_skill_roots


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.values[key] = value


class _Service:
    memory_manager: Any = None

    def __init__(self, root: Path) -> None:
        self.store = SQLiteStore(root / "core.sqlite")
        self.store.initialize()


def _client(root: Path, settings: _Settings) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.state.skill_source_defaults_enabled = False
    app.state.skill_source_workspace = root.parent
    app.state.b23_skill_roots = {"configured": root}
    service = _Service(root.parent)
    app.state.phase45_action_gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(service.store),
        SQLitePhase45Repository(service.store),
        PolicyEngine(balanced_policy_bundle()),
    )
    app.state.skill_source_test_service = service
    install_skill_source_routes(app, service, settings)  # type: ignore[arg-type]
    return TestClient(app), app


def test_default_locations_include_bundle_home_and_workspace(tmp_path: Path) -> None:
    roots = default_skill_roots(tmp_path)
    assert len(roots) == 8
    assert roots["workspace-operant"] == tmp_path / ".operant" / "skills"
    assert "operant-default" in roots


def test_explicit_empty_roots_stay_empty_without_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    app.state.skill_source_defaults_enabled = False
    app.state.b23_skill_roots = {}
    service = _Service(tmp_path)

    def forbidden_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("explicit empty sources must not scan the host")

    monkeypatch.setattr(source_module, "SkillDiscovery", forbidden_scan)
    install_skill_source_routes(app, service, _Settings())  # type: ignore[arg-type]
    assert app.state.refresh_skill_sources() == []
    assert app.state.b23_skill_roots == {}


def test_source_add_persists_and_delete_preserves_explicit_root(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    added = tmp_path / "added"
    added.mkdir()
    (added / "SKILL.md").write_text(
        "---\nname: added\ndescription: Added source\n---\nUse this skill.\n",
        encoding="utf-8",
    )
    settings = _Settings()
    client, app = _client(configured, settings)

    initial = client.get("/v1/setup/skill-sources")
    assert initial.status_code == 200
    assert [item["root_ref"] for item in initial.json()["items"]] == ["configured"]
    created = client.post(
        "/v1/setup/skill-sources",
        json={"path": str(added)},
        headers={"Idempotency-Key": "add-source"},
    )
    assert created.status_code == 200
    root_ref = created.json()["root_ref"]
    assert created.json()["enabled"] is True
    assert app.state.b23_skill_roots[root_ref] == added
    assert settings.values["skill_sources.v1"] == {root_ref: str(added)}
    assert {
        item["root_ref"]
        for item in SQLitePhase45Repository(
            app.state.skill_source_test_service.store
        ).list_skill_candidates()
    } == {root_ref}
    assert (
        client.post(
            "/v1/setup/skill-sources",
            json={"path": str(added)},
            headers={"Idempotency-Key": "add-source"},
        ).json()
        == created.json()
    )
    assert (
        client.post(
            "/v1/setup/skill-sources",
            json={"path": str(added)},
            headers={"Idempotency-Key": "same-directory-new-request"},
        ).json()
        == created.json()
    )
    another = tmp_path / "another"
    another.mkdir()
    assert (
        client.post(
            "/v1/setup/skill-sources",
            json={"path": str(another)},
            headers={"Idempotency-Key": "add-source"},
        ).status_code
        == 409
    )

    restarted, _ = _client(configured, settings)
    restarted_items = restarted.get("/v1/setup/skill-sources").json()["items"]
    assert {item["root_ref"] for item in restarted_items} == {
        "configured",
        root_ref,
    }
    deleted = client.delete(
        f"/v1/setup/skill-sources/{root_ref}",
        headers={"Idempotency-Key": "remove-source"},
    )
    assert deleted.status_code == 200
    assert [item["root_ref"] for item in deleted.json()["items"]] == ["configured"]
    assert app.state.b23_skill_roots == {"configured": configured}
    assert (
        client.delete(
            f"/v1/setup/skill-sources/{root_ref}",
            headers={"Idempotency-Key": "remove-source"},
        ).json()
        == deleted.json()
    )
    assert (
        SQLitePhase45Repository(app.state.skill_source_test_service.store).list_skill_candidates()
        == ()
    )


@pytest.mark.parametrize("alias_kind", ["home", "symlink"])
def test_legacy_canonical_source_receipt_replays_without_new_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias_kind: str
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    added = tmp_path / "added"
    added.mkdir()
    canonical = added.resolve()
    if alias_kind == "home":
        monkeypatch.setenv("HOME", str(tmp_path))
        requested = "~/added"
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(added, target_is_directory=True)
        requested = str(alias)
    root_ref = "user-" + hashlib.sha256(str(canonical).encode()).hexdigest()[:12]
    settings = _Settings()
    settings.values["skill_sources.v1"] = {root_ref: str(canonical)}
    client, app = _client(configured, settings)
    old_view = next(
        item
        for item in client.get("/v1/setup/skill-sources").json()["items"]
        if item["root_ref"] == root_ref
    )
    key = f"legacy-{alias_kind}"
    canonical_receipt_fingerprint = canonical_action_hash(
        {"operation": "skill_source_add", "path": str(canonical)}
    )
    gateway = app.state.phase45_action_gateway
    action, decision, _ = gateway.guard(
        tool="skill_source",
        operation="add",
        target_id=hashlib.sha256(str(canonical).encode()).hexdigest()[:32],
        arguments={"request_hash": canonical_receipt_fingerprint},
        capabilities=(Capability.WORKSPACE_WRITE,),
        idempotency_key=key,
    )
    assert decision.lease is not None
    gateway.consume(decision.lease, action)
    store = app.state.skill_source_test_service.store
    command, created = store.reserve_command_execution(
        CommandExecution(
            command_type="skill_source.change",
            idempotency_key=key,
            action_hash=canonical_receipt_fingerprint,
        )
    )
    assert created
    store.complete_command_execution(
        command.id, response_json=json.dumps(old_view), http_status=200
    )

    def forbidden_change(_key: str, _value: Any) -> None:
        raise AssertionError("completed source request must not add another source")

    def forbidden_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("completed source request must not scan sources again")

    monkeypatch.setattr(settings, "set_setting", forbidden_change)
    monkeypatch.setattr(source_module, "SkillDiscovery", forbidden_scan)
    replay = client.post(
        "/v1/setup/skill-sources",
        json={"path": requested},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200
    assert replay.json() == old_view
    assert settings.values["skill_sources.v1"] == {root_ref: str(canonical)}
    if alias_kind == "symlink":
        other = tmp_path / "other"
        other.mkdir()
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        changed = client.post(
            "/v1/setup/skill-sources",
            json={"path": requested},
            headers={"Idempotency-Key": key},
        )
        assert changed.status_code == 409


def test_unknown_legacy_source_receipt_is_not_retried(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    added = tmp_path / "added"
    added.mkdir()
    settings = _Settings()
    client, app = _client(configured, settings)
    key = "unknown-legacy-source"
    canonical_receipt_fingerprint = canonical_action_hash(
        {"operation": "skill_source_add", "path": str(added.resolve())}
    )
    command, created = app.state.skill_source_test_service.store.reserve_command_execution(
        CommandExecution(
            command_type="skill_source.change",
            idempotency_key=key,
            action_hash=canonical_receipt_fingerprint,
        )
    )
    assert created and command.response_json is None

    replay = client.post(
        "/v1/setup/skill-sources",
        json={"path": str(added)},
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "command_outcome_unknown"
    assert settings.values == {}


@pytest.mark.parametrize("reopened", [False, True])
@pytest.mark.parametrize("replacement", ["source", "ancestor"])
def test_saved_source_link_replacement_needs_explicit_new_registration(
    tmp_path: Path, reopened: bool, replacement: str
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    parent = tmp_path / "registered-parent"
    source = parent / "skills"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: original-skill\ndescription: Original root\n---\nOriginal.\n",
        encoding="utf-8",
    )
    outside_parent = tmp_path / "outside-parent"
    outside = outside_parent / "skills"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text(
        "---\nname: replacement-skill\ndescription: New target\n---\nReplacement.\n",
        encoding="utf-8",
    )
    settings = _Settings()
    client, app = _client(configured, settings)
    added = client.post(
        "/v1/setup/skill-sources",
        json={"path": str(source)},
        headers={"Idempotency-Key": "register-original"},
    )
    assert added.status_code == 200
    original_ref = added.json()["root_ref"]
    repository = SQLitePhase45Repository(app.state.skill_source_test_service.store)
    assert any(item["root_ref"] == original_ref for item in repository.list_skill_candidates())

    if replacement == "source":
        source.rename(parent / "skills-original")
        source.symlink_to(outside, target_is_directory=True)
    else:
        parent.rename(tmp_path / "registered-parent-original")
        parent.symlink_to(outside_parent, target_is_directory=True)
    if reopened:
        client, app = _client(configured, settings)
        repository = SQLitePhase45Repository(app.state.skill_source_test_service.store)

    views = client.get("/v1/setup/skill-sources").json()["items"]
    old_view = next(item for item in views if item["root_ref"] == original_ref)
    assert old_view["path"] == str(source)
    assert old_view["enabled"] is False
    assert old_view["issue"] == "目录已改变，请重新添加来源"
    assert original_ref not in app.state.b23_skill_roots
    assert not any(item["root_ref"] == original_ref for item in repository.list_skill_candidates())
    assert not any(
        item["name"] == "replacement-skill" for item in repository.list_skill_candidates()
    )

    replacement_added = client.post(
        "/v1/setup/skill-sources",
        json={"path": str(source)},
        headers={"Idempotency-Key": "register-new-target"},
    )
    assert replacement_added.status_code == 200
    new_ref = replacement_added.json()["root_ref"]
    assert new_ref != original_ref
    assert replacement_added.json()["path"] == str(outside)
    assert app.state.b23_skill_roots[new_ref] == outside
    assert {item["name"] for item in repository.list_skill_candidates()} == {"replacement-skill"}


def test_default_source_can_point_to_shared_skill_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    (shared / "SKILL.md").write_text(
        "---\nname: shared-skill\ndescription: Shared source\n---\nShared.\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    default_root = workspace / ".agents" / "skills"
    default_root.parent.mkdir(parents=True)
    default_root.symlink_to(shared, target_is_directory=True)
    monkeypatch.setattr(
        source_module,
        "default_skill_roots",
        lambda _workspace: {"workspace-agents": default_root},
    )
    service = _Service(tmp_path)
    app = FastAPI()
    app.state.skill_source_defaults_enabled = True
    app.state.skill_source_workspace = workspace
    app.state.b23_skill_roots = {}
    app.state.phase45_action_gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(service.store),
        SQLitePhase45Repository(service.store),
        PolicyEngine(balanced_policy_bundle()),
    )
    install_skill_source_routes(app, service, _Settings())  # type: ignore[arg-type]

    source = TestClient(app).get("/v1/setup/skill-sources").json()["items"][0]
    assert source["enabled"] is True
    assert source["path"] == str(shared)
    assert {
        item["name"] for item in SQLitePhase45Repository(service.store).list_skill_candidates()
    } == {"shared-skill"}


def test_source_rejects_remote_origin_and_missing_directory(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    client, _ = _client(configured, _Settings())

    assert (
        client.get(
            "/v1/setup/skill-sources", headers={"origin": "https://other.example"}
        ).status_code
        == 403
    )
    assert (
        client.post("/v1/setup/skill-sources", json={"path": str(tmp_path / "missing")}).status_code
        == 422
    )


def test_source_expands_current_home_only_after_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    added = tmp_path / "skills"
    added.mkdir()
    client, _ = _client(configured, _Settings())
    monkeypatch.setenv("HOME", str(tmp_path))

    created = client.post(
        "/v1/setup/skill-sources",
        json={"path": "~/skills"},
        headers={"Idempotency-Key": "current-home-source"},
    )
    assert created.status_code == 200
    assert created.json()["path"] == str(added)


def test_source_hard_deny_prevents_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    added = tmp_path / "added"
    added.mkdir()
    settings = _Settings()
    client, app = _client(configured, settings)
    registered = client.post(
        "/v1/setup/skill-sources",
        json={"path": str(added)},
        headers={"Idempotency-Key": "approved-source"},
    )
    assert registered.status_code == 200
    app.state.phase45_action_gateway.engine = PolicyEngine(
        PolicyBundle(
            bundle_id="deny-sources",
            version="deny-sources.v1",
            rules=(
                PolicyRule(
                    rule_id="deny-skill-source-write",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.DENY,
                    capabilities=(Capability.WORKSPACE_WRITE,),
                    tools=("skill_source",),
                    hard=True,
                    reason="source changes are disabled",
                ),
            ),
        )
    )

    def forbidden_resolution(_path: str | Path) -> Path:
        raise AssertionError("denied source must not be resolved or inspected")

    def forbidden_expansion(_path: Path) -> Path:
        raise AssertionError("denied source must not look up a home directory")

    monkeypatch.setattr(source_module, "_source_path", forbidden_resolution)
    monkeypatch.setattr(Path, "expanduser", forbidden_expansion)
    denied = client.post(
        "/v1/setup/skill-sources",
        json={"path": "~otheruser/skills"},
        headers={"Idempotency-Key": "denied-source"},
    )
    assert denied.status_code == 403
    denied_delete = client.delete(
        f"/v1/setup/skill-sources/{registered.json()['root_ref']}",
        headers={"Idempotency-Key": "denied-source-delete"},
    )
    assert denied_delete.status_code == 403
    assert settings.values["skill_sources.v1"] == {registered.json()["root_ref"]: str(added)}
    assert set(app.state.b23_skill_roots) == {"configured", registered.json()["root_ref"]}


def test_default_startup_and_workspace_change_refresh_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for workspace, name in ((first, "first-skill"), (second, "second-skill")):
        root = workspace / ".agents" / "skills"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test source\n---\nUse it.\n",
            encoding="utf-8",
        )
    broken = first / ".agents" / "skills" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text(
        "---\nname: broken\ndescription: !!python/object:os.system unsafe\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        source_module,
        "default_skill_roots",
        lambda workspace: {"workspace-agents": workspace / ".agents" / "skills"},
    )
    service = _Service(tmp_path)
    app = FastAPI()
    app.state.skill_source_defaults_enabled = True
    app.state.skill_source_workspace = first
    app.state.b23_skill_roots = {}
    app.state.phase45_action_gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(service.store),
        SQLitePhase45Repository(service.store),
        PolicyEngine(balanced_policy_bundle()),
    )
    install_skill_source_routes(app, service, _Settings())  # type: ignore[arg-type]
    repository = SQLitePhase45Repository(service.store)
    assert {item["name"] for item in repository.list_skill_candidates()} == {"first-skill"}
    initial_view = app.state.refresh_skill_sources()[0]
    assert initial_view.issue is not None and "broken" in initial_view.issue

    app.state.refresh_skill_sources(second)
    assert {item["name"] for item in repository.list_skill_candidates()} == {"second-skill"}
    manifest = second / ".agents" / "skills" / "SKILL.md"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "changed", encoding="utf-8")
    # A settings render updates source status without rescanning manifests.
    app.state.refresh_skill_sources()
    assert {item["name"] for item in repository.list_skill_candidates()} == {"second-skill"}
    root = manifest.parent
    root.rename(root.with_name("skills-hidden"))
    app.state.refresh_skill_sources()
    assert repository.list_skill_candidates() == ()
    root.with_name("skills-hidden").rename(root)
    app.state.refresh_skill_sources()
    assert {item["name"] for item in repository.list_skill_candidates()} == {"second-skill"}


def test_reopened_app_restores_registered_default_workspace_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "selected-workspace"
    root = workspace / ".agents" / "skills"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: selected-skill\ndescription: Restored source\n---\nUse it.\n",
        encoding="utf-8",
    )
    other = tmp_path / "launch-directory"
    other.mkdir()
    monkeypatch.setattr(
        source_module,
        "default_skill_roots",
        lambda selected: {"workspace-agents": selected / ".agents" / "skills"},
    )
    service = _Service(tmp_path)
    registered, _ = service.store.register_workspace(
        WorkspaceInitialization(
            workspace_ref=str(workspace),
            workspace_hash=hashlib.sha256(str(workspace).encode()).hexdigest(),
            readable=True,
            writable=True,
        )
    )
    UXRepository(service.store).set_setting("default_workspace_id", registered.id)

    for _ in range(2):
        reopened = _Service(tmp_path)
        app = FastAPI()
        app.state.skill_source_defaults_enabled = True
        app.state.skill_source_workspace = other
        app.state.b23_skill_roots = {}
        app.state.phase45_action_gateway = Phase45ActionGateway(
            SQLiteSecurityRepository(reopened.store),
            SQLitePhase45Repository(reopened.store),
            PolicyEngine(balanced_policy_bundle()),
        )
        install_skill_source_routes(
            app,
            reopened,
            UXRepository(reopened.store),  # type: ignore[arg-type]
        )
        assert app.state.b23_skill_roots == {"workspace-agents": root}
        assert {
            item["name"] for item in SQLitePhase45Repository(reopened.store).list_skill_candidates()
        } == {"selected-skill"}


@pytest.mark.asyncio
async def test_registered_directory_link_can_be_installed(tmp_path: Path) -> None:
    linked_root = tmp_path / "linked-root"
    real_root = tmp_path / "real-root"
    skill = real_root / "example"
    linked_root.mkdir()
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: example\ndescription: linked directory\n---\nRead this skill.\n",
        encoding="utf-8",
    )
    (linked_root / "example").symlink_to(skill, target_is_directory=True)
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), object())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service, skill_roots={"linked": linked_root, "real": real_root})
    try:
        await manager.execute(ManagementCommand(action="skill_discover"))
        candidates = SQLitePhase45Repository(manager.store).list_skill_candidates()
        assert len(candidates) == 1
        assert candidates[0]["root_ref"] == "linked"
        result = await manager.execute(
            ManagementCommand(action="skill_install", package_ref=candidates[0]["candidate_id"])
        )
        assert len(result.state.skills) == 1
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_missing_configured_root_discovers_empty(tmp_path: Path) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), object())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service, skill_roots={"missing": tmp_path / "absent"})
    try:
        await manager.execute(ManagementCommand(action="skill_discover"))
        assert SQLitePhase45Repository(manager.store).list_skill_candidates() == ()
    finally:
        await manager.close()
        service.close()
