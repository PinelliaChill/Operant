from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from operant.domain.remote_control import EncryptedRemoteCommand, RelayEnvelope


class HostCommandSink(Protocol):
    def submit_command(self, command: EncryptedRemoteCommand) -> Any: ...


@dataclass(frozen=True)
class HostConnectorConfig:
    base_url: str
    bearer_token: str
    route_ref: str
    recipient_ref: str
    poll_interval_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    request_timeout_seconds: float = 10.0
    max_batch: int = 32
    max_response_bytes: int = 48 * 1024 * 1024
    allow_loopback_http: bool = False

    def __post_init__(self) -> None:
        url = httpx.URL(self.base_url)
        if (
            url.host is None
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Host Connector URL must be an origin without embedded data")
        if url.scheme != "https" and not (
            self.allow_loopback_http
            and url.scheme == "http"
            and url.host in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError("Host Connector requires HTTPS outside explicit loopback tests")
        if not 32 <= len(self.bearer_token) <= 512 or any(
            character.isspace() for character in self.bearer_token
        ):
            raise ValueError("Host Connector bearer token must be a bounded secret")
        if not 1 <= len(self.route_ref) <= 300 or not 1 <= len(self.recipient_ref) <= 300:
            raise ValueError("Host Connector route and recipient refs must be bounded")
        if not 0.05 <= self.poll_interval_seconds <= 60:
            raise ValueError("Host Connector poll interval is out of bounds")
        if not self.poll_interval_seconds <= self.max_backoff_seconds <= 300:
            raise ValueError("Host Connector backoff is out of bounds")
        if not 1 <= self.max_batch <= 50:
            raise ValueError("Host Connector batch size is out of bounds")
        if not 1024 <= self.max_response_bytes <= 64 * 1024 * 1024:
            raise ValueError("Host Connector response limit is out of bounds")


class RelayHostConnector:
    """Pull opaque Relay envelopes and submit them to the authoritative Host.

    Relay acknowledgement is issued only after the Host service has returned a
    durable command receipt. The acknowledgement remains a Relay delivery fact
    and is never exposed as the Host receipt itself.
    """

    def __init__(
        self,
        config: HostConnectorConfig,
        sink: HostCommandSink,
        *,
        decrypt_envelope: Callable[[RelayEnvelope], EncryptedRemoteCommand],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.decrypt_envelope = decrypt_envelope
        self.transport = transport
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        delay = self.config.poll_interval_seconds
        headers = {
            "authorization": f"Bearer {self.config.bearer_token}",
            "accept": "application/json",
        }
        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.config.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            while not self._stopped.is_set():
                try:
                    processed = await self.poll_once(client)
                except (httpx.HTTPError, ValueError, KeyError):
                    await self._wait(delay)
                    delay = min(self.config.max_backoff_seconds, delay * 2)
                    continue
                delay = self.config.poll_interval_seconds
                if processed == 0:
                    await self._wait(delay)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        response = await client.get(
            "/v1/relay/envelopes",
            params={
                "route_ref": self.config.route_ref,
                "recipient_ref": self.config.recipient_ref,
                "limit": self.config.max_batch,
            },
        )
        response.raise_for_status()
        if len(response.content) > self.config.max_response_bytes:
            raise ValueError("Relay response exceeded the configured limit")
        body = response.json()
        raw_items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(raw_items, list):
            raise ValueError("Relay response has no bounded envelope list")
        if len(raw_items) > self.config.max_batch:
            raise ValueError("Relay returned more envelopes than requested")
        processed = 0
        for raw in raw_items:
            envelope = RelayEnvelope.model_validate(raw)
            if (
                envelope.route_ref != self.config.route_ref
                or envelope.recipient_ref != self.config.recipient_ref
            ):
                raise ValueError("Relay envelope binding does not match this Host Connector")
            if envelope.expires_at <= datetime.now(timezone.utc):
                continue
            # The encrypted command is the only accepted payload. Signature,
            # nonce, session, scope and TTL are rechecked by the Host service.
            command = self.decrypt_envelope(envelope)
            receipt = await asyncio.to_thread(self.sink.submit_command, command)
            if getattr(receipt, "command_id", None) != command.command_id or not callable(
                getattr(receipt, "model_dump", None)
            ):
                raise ValueError("Host did not return a durable receipt for the submitted command")
            acknowledgement = await client.post(
                f"/v1/relay/envelopes/{envelope.envelope_id}/acknowledge",
                json={"recipient_ref": self.config.recipient_ref},
            )
            acknowledgement.raise_for_status()
            processed += 1
        return processed

    async def _wait(self, delay: float) -> None:
        jittered = delay * random.uniform(0.8, 1.2)
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=jittered)
        except TimeoutError:
            return
