from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from operant.domain.messages import Message, ProviderEvent, ToolDefinition
from operant.domain.models import RoleSnapshot


class ModelProvider(Protocol):
    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]: ...

    def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]: ...
