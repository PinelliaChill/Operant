"""Read the generated Phase 1E protocol manifest without duplicating Schema."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

PHASE1E_PROTOCOL_VERSION = "phase1e.v1"
PHASE1E_MIN_CLIENT_VERSION = "phase1e.v1"
PHASE1E_CAPABILITIES: tuple[str, ...] = (
    "workspace_project_projection",
    "workspace_file_metadata",
    "thread_projection",
    "session_command",
    "sse_replay",
    "approval_projection",
)
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ProtocolSchemaUnavailable(RuntimeError):
    """The generated public Schema digest has not been installed."""


def phase1e_protocol_metadata() -> dict[str, Any]:
    """Return protocol negotiation metadata from the generated digest artifact.

    The protocol/SDK execution line owns the Schema and its digest artifact.
    The backend only reads that artifact through this one-way integration
    point. It intentionally has no fallback constant and never hashes the
    hand-written Python API models.
    """

    digest_path = _digest_path()
    try:
        raw_digest = digest_path.read_text(encoding="utf-8").strip().split()[0]
    except (FileNotFoundError, IsADirectoryError, IndexError, UnicodeError, OSError) as exc:
        raise ProtocolSchemaUnavailable(
            "generated Phase 1E protocol digest is unavailable"
        ) from exc
    if _DIGEST_PATTERN.fullmatch(raw_digest) is None:
        raise ProtocolSchemaUnavailable("generated Phase 1E protocol digest is invalid")
    return {
        "protocol_version": PHASE1E_PROTOCOL_VERSION,
        "schema_digest": raw_digest,
        "min_client_version": PHASE1E_MIN_CLIENT_VERSION,
        "capabilities": list(PHASE1E_CAPABILITIES),
    }


def _digest_path() -> Path:
    configured = os.environ.get("OPERANT_PHASE1E_SCHEMA_DIGEST_PATH")
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            raise ProtocolSchemaUnavailable("protocol digest path must be absolute")
        return candidate
    # Source checkout path. Packaging/integration can inject an absolute path
    # through OPERANT_PHASE1E_SCHEMA_DIGEST_PATH without changing this module.
    return (
        Path(__file__).resolve().parents[3] / "sdk/protocol/schema/operant-phase1e.openapi.sha256"
    )
