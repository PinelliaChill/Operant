from __future__ import annotations

from operant.domain.commands import (
    PHASE1D_SLASH_REGISTRY_VERSION,
    SlashCommandDefinition,
    SlashCommandKind,
    SlashCommandResolution,
)

_PHASE1D_COMMANDS = (
    SlashCommandDefinition(
        canonical_name="/init",
        aliases=("/初始化",),
        command_kind=SlashCommandKind.WORKSPACE_INIT,
        endpoint="/v1/commands/workspace/init",
    ),
    SlashCommandDefinition(
        canonical_name="/review",
        aliases=("/审查",),
        command_kind=SlashCommandKind.REVIEW,
        endpoint="/v1/commands/review",
        execution_mode="sse",
    ),
    SlashCommandDefinition(
        canonical_name="/clear-context",
        aliases=("/清空上下文",),
        command_kind=SlashCommandKind.CONTEXT_CLEAR,
        endpoint="/v1/commands/context/clear",
    ),
    SlashCommandDefinition(
        canonical_name="/compact-context",
        aliases=("/压缩上下文",),
        command_kind=SlashCommandKind.CONTEXT_COMPACT,
        endpoint="/v1/commands/context/compact",
    ),
)


class SlashCommandRegistry:
    """Frozen Phase 1D alias-to-command resolver."""

    version = PHASE1D_SLASH_REGISTRY_VERSION

    def __init__(self) -> None:
        self._commands = _PHASE1D_COMMANDS
        self._aliases = {
            alias.casefold(): definition
            for definition in self._commands
            for alias in (definition.canonical_name, *definition.aliases)
        }

    def list_commands(self) -> tuple[SlashCommandDefinition, ...]:
        return self._commands

    def resolve(self, text: str, *, registry_version: str | None = None) -> SlashCommandResolution:
        if registry_version is not None and registry_version != self.version:
            raise ValueError("unsupported Slash Command Registry version")
        normalized = text.strip()
        if not normalized or len(normalized) > 2_100:
            raise ValueError("slash command text is empty or too long")
        token, separator, arguments = normalized.partition(" ")
        definition = self._aliases.get(token.casefold())
        if definition is None:
            raise LookupError("slash command is not registered")
        return SlashCommandResolution(
            canonical_name=definition.canonical_name,
            command_kind=definition.command_kind,
            arguments=arguments.strip() if separator else "",
        )
