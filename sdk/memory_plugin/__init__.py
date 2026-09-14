"""Small, dependency-free helpers for Operant memory plugins.

The public MP-0 contract remains in ``operant.contracts.b2_1``.  This package
only contains the pieces an installed stdio package needs while running with
``python -I -S``: strict-enough DTO readers, bounded JSON-RPC framing, and
deterministic package/manifest helpers.  It intentionally has no dependency on
Pydantic, Operant Core, or a filesystem location outside the package itself.
"""

from .dto import (
    CandidateReference,
    CandidateReferenceDTO,
    MemoryVersionRef,
    MemoryVersionRefDTO,
    PrivateIndexResource,
    PrivateIndexResourceDTO,
    PrivateIndexResult,
    PrivateIndexResultDTO,
    RpcContext,
    RpcContextDTO,
    SourceRef,
    SourceRefDTO,
)
from .package import (
    PLUGIN_SDK_VERSION,
    build_manifest,
    canonical_json_bytes,
    config_schema_digest,
    copy_runtime,
    metadata_digest,
    package_digest,
)
from .stdio import (
    HostClient,
    JsonRpcError,
    JsonRpcHostClient,
    StdioHostClient,
    canonical_json,
    decode_frame,
    encode_frame,
    serve,
    serve_stdio,
)

__all__ = [
    "PLUGIN_SDK_VERSION",
    "CandidateReference",
    "CandidateReferenceDTO",
    "HostClient",
    "JsonRpcError",
    "JsonRpcHostClient",
    "MemoryVersionRef",
    "MemoryVersionRefDTO",
    "PrivateIndexResource",
    "PrivateIndexResourceDTO",
    "PrivateIndexResult",
    "PrivateIndexResultDTO",
    "RpcContext",
    "RpcContextDTO",
    "SourceRef",
    "SourceRefDTO",
    "StdioHostClient",
    "build_manifest",
    "canonical_json",
    "canonical_json_bytes",
    "config_schema_digest",
    "copy_runtime",
    "decode_frame",
    "encode_frame",
    "metadata_digest",
    "package_digest",
    "serve_stdio",
    "serve",
]
