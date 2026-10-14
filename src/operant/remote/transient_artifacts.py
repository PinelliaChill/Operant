"""Bounded, short-lived evidence. Pixels and clipboard text never enter SQLite."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class TransientArtifact:
    content: bytes
    media_type: str
    sha256: str
    expires_at: float


class TransientCapabilityArtifacts:
    def __init__(self, *, ttl_seconds: float = 300, max_bytes: int = 33_554_432) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self._items: dict[str, TransientArtifact] = {}
        self._lock = RLock()

    def _expire(self) -> None:
        for key, item in tuple(self._items.items()):
            if item.expires_at <= time.monotonic():
                del self._items[key]

    def expire(self) -> None:
        """Drop expired plaintext even when no reader requests another artifact."""
        with self._lock:
            self._expire()

    def put(self, job_id: str, content: bytes, media_type: str) -> None:
        if len(content) > min(self.max_bytes, 16_777_216):
            raise ValueError("capability artifact exceeds the byte limit")
        if media_type == "image/png":
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("capability screenshot must be PNG")
        elif media_type == "text/plain":
            content.decode("utf-8")
        else:
            raise ValueError("unsupported capability artifact media type")
        with self._lock:
            self._expire()
            while (
                self._items
                and sum(len(i.content) for i in self._items.values()) + len(content)
                > self.max_bytes
            ):
                del self._items[next(iter(self._items))]
            self._items[job_id] = TransientArtifact(
                content,
                media_type,
                hashlib.sha256(content).hexdigest(),
                time.monotonic() + self.ttl_seconds,
            )

    def get(self, job_id: str) -> TransientArtifact:
        with self._lock:
            self._expire()
            return self._items[job_id]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
