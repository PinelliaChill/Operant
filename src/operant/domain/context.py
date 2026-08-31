from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.domain.messages import Message, ToolDefinition
from operant.domain.models import new_id, utc_now


class ContextReferenceType(str, Enum):
    THREAD = "thread"
    ITEM = "item"
    ARTIFACT = "artifact"
    MEMORY = "memory"


class ReferenceIncludeMode(str, Enum):
    INLINE = "inline"
    METADATA = "metadata"


class ContextSourceType(str, Enum):
    SESSION = "session"
    AGENT = "agent"
    THREAD = "thread"
    ITEM = "item"
    ARTIFACT = "artifact"
    MEMORY = "memory"
    TOOL_SCHEMA = "tool_schema"
    COMPACTION = "compaction"


class PromptBlockType(str, Enum):
    ROLE_INSTRUCTIONS = "role_instructions"
    TOOL_SCHEMA = "tool_schema"
    EXPLICIT_REFERENCES = "explicit_references"
    COMPACTION = "compaction"
    CONVERSATION = "conversation"


PHASE1B_PROMPT_LAYOUT_VERSION = "phase1b.v1"
PHASE1B_PROMPT_BLOCK_ORDER = (
    PromptBlockType.ROLE_INSTRUCTIONS,
    PromptBlockType.TOOL_SCHEMA,
    PromptBlockType.EXPLICIT_REFERENCES,
    PromptBlockType.COMPACTION,
    PromptBlockType.CONVERSATION,
)
PHASE1B_REQUIRED_PROMPT_BLOCK_TYPES = frozenset(
    {
        PromptBlockType.ROLE_INSTRUCTIONS,
        PromptBlockType.TOOL_SCHEMA,
        PromptBlockType.CONVERSATION,
    }
)


class ContextVisibility(str, Enum):
    ROLE_PRIVATE = "role_private"
    SESSION = "session"


class ContextWatermarkState(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    EMERGENCY = "emergency"
    UNKNOWN = "unknown"


class CompactionSourceType(str, Enum):
    """Cursor namespace covered by an append-only Compaction."""

    CONTEXT_REVISIONS = "context_revisions"
    THREAD_ITEMS = "thread_items"


class ReferenceRequest(BaseModel):
    """One deliberately small, typed reference accepted by Phase 1B."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref_type: ContextReferenceType
    target_id: str = Field(min_length=1, max_length=300)
    user_text: str | None = Field(default=None, max_length=500)
    include_mode: ReferenceIncludeMode = ReferenceIncludeMode.INLINE
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class ContextSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: ContextSourceType
    source_id: str = Field(min_length=1, max_length=300)
    cursor: int | None = Field(default=None, ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReferenceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("refbind"))
    revision_id: str
    position: int = Field(ge=1)
    ref_type: ContextReferenceType
    user_text: str | None = Field(default=None, max_length=500)
    resolved_target: str = Field(min_length=1, max_length=300)
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    include_mode: ReferenceIncludeMode
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    visibility: ContextVisibility = ContextVisibility.SESSION
    resolved_at: datetime = Field(default_factory=utc_now)


class PromptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("ctxblock"))
    revision_id: str
    position: int = Field(ge=1)
    block_type: PromptBlockType
    content: str = Field(max_length=2_000_000)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_refs: tuple[ContextSourceRef, ...] = ()
    stable_until: datetime | None = None
    visibility: ContextVisibility = ContextVisibility.ROLE_PRIVATE
    token_estimate: int | None = Field(default=None, ge=0)
    cache_eligible: bool = False


class PromptLayout(BaseModel):
    """Versioned physical block order, independent from Provider cache support."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = PHASE1B_PROMPT_LAYOUT_VERSION
    block_order: tuple[PromptBlockType, ...] = PHASE1B_PROMPT_BLOCK_ORDER

    @model_validator(mode="after")
    def validate_frozen_phase1b_layout(self) -> PromptLayout:
        if not self.block_order:
            raise ValueError("PromptLayout must contain Prompt Blocks")
        if not PHASE1B_REQUIRED_PROMPT_BLOCK_TYPES.issubset(self.block_order):
            raise ValueError("PromptLayout is missing required base Prompt Blocks")
        if self.version != PHASE1B_PROMPT_LAYOUT_VERSION:
            raise ValueError("Phase 1B only supports PromptLayout phase1b.v1")
        if self.block_order != PHASE1B_PROMPT_BLOCK_ORDER:
            raise ValueError("Phase 1B PromptLayout block order is frozen")
        return self


class ContextWatermarkPolicy(BaseModel):
    """Configurable ratios; the Runtime never embeds fixed 60/80 thresholds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    yellow_at: float = Field(default=0.70, gt=0, lt=1)
    red_at: float = Field(default=0.85, gt=0, lt=1)
    emergency_at: float = Field(default=0.95, gt=0, le=1)
    safety_margin_ratio: float = Field(default=0.05, gt=0, lt=0.5)
    minimum_safety_margin_tokens: int = Field(default=512, ge=1)
    retained_recent_messages: int = Field(default=4, ge=2, le=50)
    thread_reference_ratio: float = Field(default=0.50, gt=0, lt=1)
    tool_result_artifact_ratio: float = Field(default=0.10, gt=0, lt=1)
    minimum_tool_result_artifact_tokens: int = Field(default=1_024, ge=1)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> ContextWatermarkPolicy:
        if not self.yellow_at < self.red_at < self.emergency_at:
            raise ValueError("Context Watermark thresholds must be strictly increasing")
        return self


class ContextWatermark(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ContextWatermarkState
    context_window: int | None = Field(default=None, ge=1)
    reserved_output_tokens: int | None = Field(default=None, ge=1)
    tool_schema_token_estimate: int | None = Field(default=None, ge=0)
    safety_margin_tokens: int | None = Field(default=None, ge=1)
    available_input_tokens: int | None = Field(default=None, ge=0)
    pre_compaction_token_estimate: int | None = Field(default=None, ge=0)
    input_token_estimate: int | None = Field(default=None, ge=0)
    estimation_method: str | None = Field(default=None, max_length=100)


class CompactionMessageCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(pattern=r"^(system|user|assistant|tool)$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str = Field(default="", max_length=240)


class CompactionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_goal: str = ""
    constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    open_tasks: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    workspace_state: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    approval_state: tuple[str, ...] = ()
    external_side_effects: tuple[str, ...] = ()
    manual_reconcile_items: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    covered_messages: tuple[CompactionMessageCoverage, ...] = ()


def compaction_coverage_hash(summary: CompactionSummary) -> str:
    """Commit to covered message identities without retaining their full body."""

    canonical = json.dumps(
        [
            {"role": item.role, "content_hash": item.content_hash}
            for item in summary.covered_messages
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Compaction(BaseModel):
    """Append-only derived summary; it never replaces Canonical History."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("compaction"))
    cursor: int | None = Field(default=None, ge=1)
    session_id: str
    agent_id: str
    thread_id: str | None = None
    source_type: CompactionSourceType
    source_cursor_start: int = Field(ge=1)
    source_cursor_end: int = Field(ge=1)
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: CompactionSummary
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    covered_item_refs: tuple[ContextSourceRef, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_coverage(self) -> Compaction:
        if self.source_cursor_end < self.source_cursor_start:
            raise ValueError("compaction source cursor range is invalid")
        if self.source_type is CompactionSourceType.THREAD_ITEMS and self.thread_id is None:
            raise ValueError("thread item compaction requires a thread_id")
        if self.source_type is CompactionSourceType.CONTEXT_REVISIONS and self.covered_item_refs:
            raise ValueError("ContextRevision compaction cannot claim Thread Item evidence")
        if self.source_type is CompactionSourceType.THREAD_ITEMS:
            if not self.covered_item_refs:
                raise ValueError("thread item compaction requires exact Item evidence")
            if any(
                ref.source_type is not ContextSourceType.ITEM or ref.cursor is None
                for ref in self.covered_item_refs
            ):
                raise ValueError("thread item compaction evidence must use Item cursors")
            item_ids = [ref.source_id for ref in self.covered_item_refs]
            item_cursors = [ref.cursor for ref in self.covered_item_refs if ref.cursor is not None]
            if len(item_ids) != len(set(item_ids)) or len(item_cursors) != len(set(item_cursors)):
                raise ValueError("thread item compaction evidence must be unique")
            if item_cursors != sorted(item_cursors):
                raise ValueError("thread item compaction evidence must follow items.sequence")
            if (
                item_cursors[0] != self.source_cursor_start
                or item_cursors[-1] != self.source_cursor_end
            ):
                raise ValueError("thread item compaction range must match its exact evidence")
        return self


def deterministic_compaction_id(compaction: Compaction) -> str:
    """Derive a stable identity from the complete immutable Compaction evidence."""

    canonical = json.dumps(
        {
            "session_id": compaction.session_id,
            "agent_id": compaction.agent_id,
            "thread_id": compaction.thread_id,
            "source_type": compaction.source_type.value,
            "source_cursor_start": compaction.source_cursor_start,
            "source_cursor_end": compaction.source_cursor_end,
            "source_snapshot_hash": compaction.source_snapshot_hash,
            "summary": compaction.summary.model_dump(mode="json"),
            "content_hash": compaction.content_hash,
            "covered_item_refs": [
                ref.model_dump(mode="json") for ref in compaction.covered_item_refs
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"compaction_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class ToolResultStub(BaseModel):
    """Recoverable replacement for a large, safely persisted Tool Result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=300)
    tool_call_id: str = Field(min_length=1, max_length=300)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_size: int = Field(ge=0)
    stored_size: int = Field(ge=0)
    summary: str = Field(max_length=500)
    fetch_capability: str = Field(
        default="context_reference_inline",
        pattern=r"^context_reference_inline$",
    )


class ContextRevision(BaseModel):
    """Immutable evidence of the exact messages and tools sent for one request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("context"))
    cursor: int | None = Field(default=None, ge=1)
    session_id: str
    agent_id: str
    thread_id: str | None = None
    # The run workspace is frozen with the revision so project Memory scope
    # can be checked without consulting mutable/current request state.
    workspace_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    request_ordinal: int = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_layout: PromptLayout
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...]
    blocks: tuple[PromptBlock, ...]
    reference_bindings: tuple[ReferenceBinding, ...] = ()
    tool_result_stubs: tuple[ToolResultStub, ...] = ()
    watermark: ContextWatermark
    compaction_id: str | None = None
    message_ids: tuple[str, ...] = ()
    source_item_ids: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    compaction_refs: tuple[str, ...] = ()
    # A revision keeps the exact typed source identities and hashes used at
    # composition time.  Readback validates these frozen snapshots instead of
    # re-hashing mutable Thread or current Memory rows.
    source_snapshots: tuple[ContextSourceRef, ...] = ()
    token_estimate: int = Field(ge=0)
    # This optional range is exclusively the canonical ``items.sequence``
    # namespace. Other typed sources keep their own cursor on ContextSourceRef.
    source_cursor_start: int | None = Field(default=None, ge=1)
    source_cursor_end: int | None = Field(default=None, ge=1)
    source_cursor_namespace: Literal["items.sequence"] | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def populate_deterministic_provenance(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        populated = dict(data)
        messages = tuple(
            message if isinstance(message, Message) else Message.model_validate(message)
            for message in populated.get("messages", ())
        )
        blocks = tuple(
            block if isinstance(block, PromptBlock) else PromptBlock.model_validate(block)
            for block in populated.get("blocks", ())
        )
        # The block list is the canonical immutable evidence.  Recompute this
        # convenience field even when a caller started from ``model_dump`` and
        # replaced blocks; otherwise stale snapshots could make a legitimate
        # model reconstruction fail before the Store gets a chance to apply
        # its current-source checks.
        snapshots: list[ContextSourceRef] = []
        seen_snapshots: set[tuple[ContextSourceType, str, int | None, str]] = set()
        for block in blocks:
            for source in block.source_refs:
                key = (
                    source.source_type,
                    source.source_id,
                    source.cursor,
                    source.content_hash,
                )
                if key in seen_snapshots:
                    continue
                seen_snapshots.add(key)
                snapshots.append(source)
        populated["source_snapshots"] = tuple(snapshots)
        agent_id = populated.get("agent_id")
        request_ordinal = populated.get("request_ordinal")
        if messages and agent_id is not None and request_ordinal is not None:
            expected_ids = tuple(
                context_message_id(
                    str(agent_id),
                    int(request_ordinal),
                    position,
                    message,
                )
                for position, message in enumerate(messages, start=1)
            )
            populated.setdefault("message_ids", expected_ids)
        watermark_value = populated.get("watermark")
        if "token_estimate" not in populated and watermark_value is not None:
            watermark = (
                watermark_value
                if isinstance(watermark_value, ContextWatermark)
                else ContextWatermark.model_validate(watermark_value)
            )
            populated["token_estimate"] = watermark.input_token_estimate
        return populated

    @model_validator(mode="after")
    def validate_snapshot(self) -> ContextRevision:
        if not self.messages:
            raise ValueError("ContextRevision must contain model input messages")
        if not self.blocks:
            raise ValueError("ContextRevision must contain Prompt Blocks")
        expected_message_ids = tuple(
            context_message_id(self.agent_id, self.request_ordinal, position, message)
            for position, message in enumerate(self.messages, start=1)
        )
        if self.message_ids != expected_message_ids:
            raise ValueError("ContextRevision message_ids do not match its safe messages")
        if self.token_estimate != self.watermark.input_token_estimate:
            raise ValueError("ContextRevision token_estimate must match its Watermark")
        if tuple(block.position for block in self.blocks) != tuple(range(1, len(self.blocks) + 1)):
            raise ValueError("Prompt blocks must have contiguous positions")
        if any(block.revision_id != self.id for block in self.blocks):
            raise ValueError("Prompt blocks must belong to their ContextRevision")
        actual_block_types = tuple(block.block_type for block in self.blocks)
        if len(actual_block_types) != len(set(actual_block_types)):
            raise ValueError("ContextRevision Prompt Block types must be unique")
        expected_block_types = tuple(
            block_type
            for block_type in self.prompt_layout.block_order
            if block_type in actual_block_types
        )
        if actual_block_types != expected_block_types:
            raise ValueError("Prompt blocks do not follow the frozen PromptLayout")
        if not PHASE1B_REQUIRED_PROMPT_BLOCK_TYPES.issubset(actual_block_types):
            raise ValueError("ContextRevision is missing required base Prompt Blocks")
        if tuple(binding.position for binding in self.reference_bindings) != tuple(
            range(1, len(self.reference_bindings) + 1)
        ):
            raise ValueError("Reference bindings must have contiguous positions")
        if any(binding.revision_id != self.id for binding in self.reference_bindings):
            raise ValueError("Reference bindings must belong to their ContextRevision")
        if len(self.tool_result_stubs) != len(
            {stub.tool_call_id for stub in self.tool_result_stubs}
        ):
            raise ValueError("Tool Result Stubs must have unique tool_call_id values")
        for values, label in (
            (self.source_item_ids, "source_item_ids"),
            (self.artifact_refs, "artifact_refs"),
            (self.memory_refs, "memory_refs"),
            (self.compaction_refs, "compaction_refs"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"ContextRevision {label} must be unique")
        expected_compaction_refs = () if self.compaction_id is None else (self.compaction_id,)
        if self.compaction_refs != expected_compaction_refs:
            raise ValueError("ContextRevision compaction_refs do not match compaction_id")
        compaction_blocks = [
            block for block in self.blocks if block.block_type is PromptBlockType.COMPACTION
        ]
        compaction_sources = [
            source
            for block in self.blocks
            for source in block.source_refs
            if source.source_type is ContextSourceType.COMPACTION
        ]
        if self.compaction_id is None:
            if compaction_blocks or compaction_sources:
                raise ValueError("ContextRevision has detached Compaction Prompt evidence")
        elif (
            len(compaction_blocks) != 1
            or len(compaction_sources) != 1
            or compaction_sources[0].source_id != self.compaction_id
        ):
            raise ValueError("ContextRevision Compaction Prompt evidence is incomplete")
        expected_snapshots: list[ContextSourceRef] = []
        seen_snapshots: set[tuple[ContextSourceType, str, int | None, str]] = set()
        for block in self.blocks:
            for source in block.source_refs:
                key = (source.source_type, source.source_id, source.cursor, source.content_hash)
                if key in seen_snapshots:
                    continue
                seen_snapshots.add(key)
                expected_snapshots.append(source)
        if self.source_snapshots != tuple(expected_snapshots):
            raise ValueError("ContextRevision source snapshots do not match Prompt evidence")
        block_artifact_sources = {
            (source.source_id, source.content_hash)
            for block in self.blocks
            for source in block.source_refs
            if source.source_type is ContextSourceType.ARTIFACT
        }
        if any(
            (stub.artifact_id, stub.content_hash) not in block_artifact_sources
            for stub in self.tool_result_stubs
        ):
            raise ValueError("Tool Result Stub requires a typed Artifact Prompt source")
        if (self.source_cursor_start is None) != (self.source_cursor_end is None):
            raise ValueError("ContextRevision source cursor range must be complete")
        if (
            self.source_cursor_start is not None
            and self.source_cursor_end is not None
            and self.source_cursor_end < self.source_cursor_start
        ):
            raise ValueError("ContextRevision source cursor range is invalid")
        if self.thread_id is None and self.source_cursor_start is not None:
            raise ValueError("ContextRevision without a thread cannot use item cursors")
        expected_namespace = "items.sequence" if self.source_cursor_start is not None else None
        if self.source_cursor_namespace != expected_namespace:
            raise ValueError("ContextRevision cursor namespace does not match its Item range")
        if self.thread_id is None and self.source_item_ids:
            raise ValueError("ContextRevision without a thread cannot claim source Items")
        return self

    @property
    def prompt_layout_version(self) -> str:
        return self.prompt_layout.version

    @property
    def agent_instance_id(self) -> str:
        return self.agent_id


def context_message_id(
    agent_id: str,
    request_ordinal: int,
    position: int,
    message: Message,
) -> str:
    canonical = json.dumps(
        message.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = (
        f"{agent_id}:{request_ordinal}:{position}:{hashlib.sha256(canonical.encode()).hexdigest()}"
    )
    return f"ctxmsg_{hashlib.sha256(identity.encode()).hexdigest()}"
