from __future__ import annotations

import asyncio

import pytest
from cryptography.exceptions import InvalidTag

from operant.remote.sealed_input import (
    bound_sealed_input_retry,
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


@pytest.mark.asyncio
async def test_ask_continuation_reuses_one_random_envelope_only_for_exact_binding() -> None:
    fields = _fields()
    with bound_sealed_input_retry():
        first = await asyncio.to_thread(seal_browser_input, "sample text", **fields)
        second = await asyncio.to_thread(seal_browser_input, "sample text", **fields)
        assert first == second
        for changed in (
            {"target_id": "other-target"},
            {"lease_id": "other-lease"},
            {"fencing": 2},
            {"token": "other-lease-token-123456"},
            {"observation_hash": "b" * 64},
            {"selector": "#other"},
        ):
            with pytest.raises(ValueError, match="binding changed"):
                await asyncio.to_thread(seal_browser_input, "sample text", **{**fields, **changed})
        with pytest.raises(ValueError, match="binding changed"):
            await asyncio.to_thread(seal_browser_input, "other text", **fields)
    assert seal_browser_input("sample text", **fields) != first  # type: ignore[arg-type]
    with bound_sealed_input_retry():
        assert seal_browser_input("sample text", **fields) != first  # type: ignore[arg-type]
