from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from pydantic import ValidationError

from operant.application.service import ApplicationService
from operant.domain.models import Effort, ModelProfile, RolePreset
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


class RegistryTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "operant.sqlite3"
        self.store = SQLiteStore(self.database)
        self.store.initialize()
        self.profile = self.store.add_model_profile(
            ModelProfile(
                name="planner-model",
                model_id="relay-model-id",
                base_url="https://relay.example.com/v1",
                secret_ref="OPERANT_API_KEY",
                supported_efforts=(Effort.LOW, Effort.MEDIUM),
            )
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_role_updates_create_versions_without_changing_old_session_snapshot(self) -> None:
        role = self.store.create_role(
            RolePreset(
                name="Planner",
                system_prompt="Create a concise implementation plan.",
                model_profile_id=self.profile.id,
                effort=Effort.MEDIUM,
            )
        )
        session = self.store.create_session(role.id)

        updated = self.store.update_role(
            role.id,
            system_prompt="Create a test-first implementation plan.",
            effort=Effort.LOW,
        )

        reopened_store = SQLiteStore(self.database)
        old_session = reopened_store.get_session(session.id)

        self.assertEqual(updated.version, 2)
        self.assertEqual(self.store.get_role(role.id).version, 2)
        self.assertEqual(self.store.get_role(role.id, version=1).system_prompt, role.system_prompt)
        self.assertEqual(old_session.role_snapshot.role_version, 1)
        self.assertEqual(old_session.role_snapshot.system_prompt, role.system_prompt)
        self.assertEqual(old_session.role_snapshot.effort, Effort.MEDIUM)
        with self.assertRaises(ValidationError):
            old_session.role_snapshot.overrides.effort_overridden = True

    def test_unsupported_effort_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "is not supported"):
            self.store.create_role(
                RolePreset(
                    name="Coder",
                    system_prompt="Modify code and run tests.",
                    model_profile_id=self.profile.id,
                    effort=Effort.HIGH,
                )
            )

    def test_secret_values_and_credentials_cannot_enter_model_profile(self) -> None:
        with self.assertRaises(ValidationError):
            ModelProfile(
                name="unsafe",
                model_id="model",
                base_url="https://user:secret@relay.example.com/v1",
                secret_ref="OPERANT_API_KEY",
            )

        with self.assertRaises(ValidationError):
            ModelProfile(
                name="unsafe",
                model_id="model",
                base_url="https://relay.example.com/v1?api_key=secret",
                secret_ref="actual secret value",
            )

    def test_role_copy_and_deactivation_are_versioned(self) -> None:
        role = self.store.create_role(
            RolePreset(
                name="Reviewer",
                system_prompt="Review changes without modifying files.",
                model_profile_id=self.profile.id,
            )
        )

        copied = self.store.copy_role(role.id, name="Security Reviewer")
        inactive = self.store.deactivate_role(role.id)

        self.assertNotEqual(copied.id, role.id)
        self.assertEqual(copied.version, 1)
        self.assertEqual(inactive.version, 2)
        self.assertEqual(inactive.status.value, "inactive")

    def test_model_profile_crud_and_session_model_override(self) -> None:
        alternative = self.store.add_model_profile(
            ModelProfile(
                name="coder-model",
                model_id="coder-exact-id",
                base_url="https://relay.example.com/v1",
                secret_ref="OPERANT_API_KEY",
            )
        )
        renamed = self.store.update_model_profile(alternative.id, name="coding-model")
        role = self.store.create_role(
            RolePreset(
                name="Coder",
                system_prompt="Modify code.",
                model_profile_id=self.profile.id,
                effort=Effort.LOW,
            )
        )

        session = self.store.create_session(
            role.id,
            model_profile_id=renamed.id,
            effort=Effort.HIGH.value,
            budget_overrides={"timeout_seconds": 42},
        )

        self.assertEqual(session.role_snapshot.model_profile_id, renamed.id)
        self.assertEqual(session.role_snapshot.model_id, "coder-exact-id")
        self.assertTrue(session.role_snapshot.overrides.model_profile_overridden)
        self.assertTrue(session.role_snapshot.overrides.effort_overridden)
        self.assertEqual(session.role_snapshot.overrides.budget_fields, ("timeout_seconds",))
        self.assertFalse(self.store.deactivate_model_profile(renamed.id).enabled)

    def test_default_roles_are_idempotent(self) -> None:
        service = ApplicationService(self.store, OpenAICompatibleProvider())
        default_profile = service.add_model_profile(
            ModelProfile(
                name="default-model",
                model_id="default-exact-id",
                base_url="https://relay.example.com/v1",
                secret_ref="OPERANT_API_KEY",
            )
        )
        first = service.seed_default_roles(
            planner_model_profile_id=default_profile.id,
            coder_model_profile_id=default_profile.id,
            reviewer_model_profile_id=default_profile.id,
        )
        second = service.seed_default_roles(
            planner_model_profile_id=default_profile.id,
            coder_model_profile_id=default_profile.id,
            reviewer_model_profile_id=default_profile.id,
        )

        self.assertEqual(
            [role.id for role in first],
            ["role_main", "role_planner", "role_explorer", "role_coder", "role_reviewer"],
        )
        self.assertEqual([role.id for role in first], [role.id for role in second])
        self.assertEqual(len(self.store.list_roles()), 5)
