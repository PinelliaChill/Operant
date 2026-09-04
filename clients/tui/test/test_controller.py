from __future__ import annotations

import unittest

from operant_tui.controller import (
    ClientController,
    CommandKeys,
    error_view,
    layout_for_width,
    validate_core_url,
)

from sdk.python_client.transport import Phase1EError


class FakePhase23:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def get_graph_run(self, run_id: str) -> dict[str, str]:
        self.calls.append(("get", run_id))
        return {"id": run_id}

    def list_node_runs(self, run_id: str) -> list[object]:
        self.calls.append(("nodes", run_id))
        return []

    def resume_graph_run(self, run_id: str, body: object, *, idempotency_key: str) -> object:
        self.calls.append(("resume", run_id, body, idempotency_key))
        return object()

    def cancel_graph_run(self, run_id: str, *, idempotency_key: str) -> object:
        self.calls.append(("cancel", run_id, idempotency_key))
        return object()


class ControllerTests(unittest.TestCase):
    def test_layout_breakpoints(self) -> None:
        self.assertEqual(layout_for_width(99), "narrow")
        self.assertEqual(layout_for_width(100), "medium")
        self.assertEqual(layout_for_width(140), "wide")

    def test_rejects_credentials_in_core_url(self) -> None:
        with self.assertRaises(ValueError):
            validate_core_url("https://token@example.test")

    def test_commands_delegate_to_generated_client_semantics(self) -> None:
        phase23 = FakePhase23()
        controller = ClientController(
            "http://127.0.0.1:8000",
            phase1e=object(),
            phase23=phase23,
            phase45=object(),
            phase56=object(),
        )
        controller.graph_projection(" run-1 ")
        controller.resume_graph("run-1", idempotency_key="resume-key")
        controller.cancel_graph("run-1", idempotency_key="cancel-key")
        self.assertEqual(phase23.calls[0], ("get", "run-1"))
        self.assertEqual(phase23.calls[2][2], {"allow_unknown_side_effect_replay": False})
        self.assertEqual(phase23.calls[3], ("cancel", "run-1", "cancel-key"))

    def test_cursor_expiry_is_explicit_and_does_not_retry_command(self) -> None:
        view = error_view(
            Phase1EError(
                "cursor_expired",
                "Cursor unavailable",
                retryable=False,
                recovery="refresh_and_retry",
            )
        )
        self.assertIn("重新读取完整投影", view.message)
        self.assertFalse(view.safe_to_retry)

    def test_manual_reconcile_is_never_presented_as_safe_retry(self) -> None:
        view = error_view(
            Phase1EError(
                "outcome_unknown",
                "Write outcome is unknown",
                retryable=True,
                recovery="manual_reconcile",
            )
        )
        self.assertFalse(view.safe_to_retry)
        self.assertIn("人工核对", view.projection_trust)

    def test_invalid_input_is_not_reported_as_a_transport_retry(self) -> None:
        view = error_view(ValueError("Graph Run ID is required"))
        self.assertEqual(view.code, "invalid_input")
        self.assertEqual(view.recovery, "edit_input")
        self.assertFalse(view.safe_to_retry)
        self.assertIn("未发送", view.projection_trust)

    def test_command_key_is_stable_until_success_releases_it(self) -> None:
        keys = CommandKeys()
        first = keys.get("cancel:run-1")
        self.assertEqual(first, keys.get("cancel:run-1"))
        keys.release("cancel:run-1")
        self.assertNotEqual(first, keys.get("cancel:run-1"))

    def test_empty_write_scope_is_rejected_before_transport(self) -> None:
        controller = ClientController(
            "http://127.0.0.1:8000",
            phase1e=object(),
            phase23=FakePhase23(),
            phase45=object(),
            phase56=object(),
        )
        with self.assertRaises(ValueError):
            controller.cancel_graph("  ", idempotency_key="key")


if __name__ == "__main__":
    unittest.main()
