from __future__ import annotations

import hashlib
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


def test_source_hard_deny_prevents_registration(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    added = tmp_path / "added"
    added.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
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
    denied = client.post(
        "/v1/setup/skill-sources",
        json={"path": str(blocked)},
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
