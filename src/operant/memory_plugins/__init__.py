"""Persistence primitives shared by the optional memory plugins.

The package intentionally contains no Core service or retrieval policy.  A
plugin can use :class:`MemoryLedger` for immutable versions, publication CAS,
and dataset-owned lifecycle data while the Core remains responsible for
identity and authorization.
"""

from .ledger import (
    SCHEMA_SQL,
    IdempotencyConflictError,
    LedgerConflictError,
    LedgerError,
    LedgerNotFoundError,
    LedgerValidationError,
    LegacyMigrationResult,
    MemoryLedger,
)

__all__ = [
    "SCHEMA_SQL",
    "IdempotencyConflictError",
    "LedgerConflictError",
    "LedgerError",
    "LedgerNotFoundError",
    "LedgerValidationError",
    "LegacyMigrationResult",
    "MemoryLedger",
]
