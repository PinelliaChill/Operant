from __future__ import annotations

from typing import Any

from operant.domain.models import AgentInstance, Session
from operant.persistence.sqlite import SQLiteStore


class AgentFactory:
    """Create sessions and agents from persisted, immutable role snapshots."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create_session(
        self,
        role_id: str,
        *,
        model_profile_id: str | None = None,
        effort: str | None = None,
        budget_overrides: dict[str, Any] | None = None,
    ) -> Session:
        return self.store.create_session(
            role_id,
            model_profile_id=model_profile_id,
            effort=effort,
            budget_overrides=budget_overrides,
        )

    def create_agent(self, session_id: str) -> AgentInstance:
        return self.store.create_agent(session_id)
