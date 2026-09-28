from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from operant.remote.sealed_input import (
    open_browser_input,
    seal_browser_input,
    valid_sealed_browser_input,
)


def _fields() -> dict[str, str | int]:
    return {
        "token": "test-lease-token-123456",
        "target_id": "browser-test",
        "lease_id": "lease-test",
        "fencing": 1,
        "observation_hash": "a" * 64,
        "selector": "#query",
        "idempotency_key": "fill-once",
    }


def test_sealed_input_round_trip_and_context_binding() -> None:
    fields = _fields()
    sealed = seal_browser_input("sample text", **fields)  # type: ignore[arg-type]
    assert valid_sealed_browser_input(sealed)
    assert "sample text" not in sealed
    assert open_browser_input(sealed, **fields) == "sample text"  # type: ignore[arg-type]
    with pytest.raises(InvalidTag):
        open_browser_input(sealed, **{**fields, "selector": "#other"})  # type: ignore[arg-type]
    with pytest.raises(InvalidTag):
        open_browser_input(sealed, **{**fields, "idempotency_key": "another"})  # type: ignore[arg-type]
    with pytest.raises(InvalidTag):
        open_browser_input(sealed, **{**fields, "token": "another-lease-token-123456"})  # type: ignore[arg-type]


def test_sealed_input_rejects_plaintext_and_malformed_envelopes() -> None:
    assert not valid_sealed_browser_input("plain text")
    assert not valid_sealed_browser_input("operant-browser-input.v1:bad data")
    with pytest.raises(ValueError, match="invalid"):
        open_browser_input("plain text", **_fields())  # type: ignore[arg-type]
