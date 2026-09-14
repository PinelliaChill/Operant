"""Record only exception classes and HTTP status for bounded acceptance calls."""

from __future__ import annotations

from typing import Any


def attach_provider_diagnostics(provider: Any) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    original = provider.stream

    async def stream(**kwargs: Any):
        try:
            async for event in original(**kwargs):
                yield event
        except Exception as error:
            classes = []
            statuses = []
            current: BaseException | None = error
            seen: set[int] = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                classes.append(type(current).__name__)
                status = getattr(getattr(current, "response", None), "status_code", None)
                if isinstance(status, int):
                    statuses.append(status)
                current = current.__cause__ or current.__context__
            errors.append({"classes": classes, "http_status": statuses})
            raise

    provider.stream = stream
    return errors
