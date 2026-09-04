from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from operant.domain.remote_execution import RemoteExecutionJob, RemoteExecutionResult
from operant.remote.connector import ConnectorOutcome, RemoteOutcomeUnknown


@dataclass(frozen=True)
class HttpRemoteTargetConfig:
    endpoint: str
    bearer_token: str
    identity_public_key: str
    timeout_seconds: float = 30.0
    max_response_bytes: int = 16 * 1024 * 1024 + 64 * 1024
    allow_loopback_http: bool = False

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if (
            url.host is None
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("remote target endpoint must be an origin without embedded data")
        if url.scheme != "https" and not (
            self.allow_loopback_http
            and url.scheme == "http"
            and url.host in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError("remote target connector requires HTTPS")
        if not 32 <= len(self.bearer_token) <= 512 or any(
            character.isspace() for character in self.bearer_token
        ):
            raise ValueError("remote target bearer token must be a bounded secret")
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("remote target timeout is out of bounds")
        if not 1024 <= self.max_response_bytes <= 64 * 1024 * 1024:
            raise ValueError("remote target response limit is out of bounds")
        _decode_public_key(self.identity_public_key)


class HttpRemoteTargetConnector:
    """HTTPS connector with target identity, lease fencing and unknown-outcome safety."""

    def __init__(
        self,
        *,
        target_id: str,
        lease_id: str,
        lease_token: str,
        lease_fencing: int,
        config: HttpRemoteTargetConfig,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not 1 <= len(target_id) <= 200 or not 1 <= len(lease_id) <= 200:
            raise ValueError("remote target and lease IDs must be bounded")
        if not 16 <= len(lease_token) <= 300 or lease_fencing < 1:
            raise ValueError("remote target connector requires a valid lease binding")
        self.target_id = target_id
        self.lease_id = lease_id
        self.lease_token = lease_token
        self.lease_fencing = lease_fencing
        self.config = config
        self.transport = transport

    def execute(self, job: RemoteExecutionJob) -> ConnectorOutcome:
        if (
            job.target_id != self.target_id
            or job.lease_id != self.lease_id
            or job.lease_fencing != self.lease_fencing
        ):
            raise ValueError("remote job does not match connector lease fencing")
        payload = job.model_dump(mode="json")
        with httpx.Client(
            base_url=self.config.endpoint,
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
            headers={
                "authorization": f"Bearer {self.config.bearer_token}",
                "idempotency-key": job.idempotency_key,
                "x-operant-target-id": self.target_id,
                "x-operant-lease-id": self.lease_id,
                "x-operant-lease-token": self.lease_token,
                "x-operant-lease-fencing": str(self.lease_fencing),
            },
        ) as client:
            try:
                response = client.post("/v1/target/jobs/execute", json=payload)
            except httpx.HTTPError as exc:
                # Once handed to the transport the target may have executed the
                # action. The Controller decides whether this becomes failed or
                # manual_reconcile_required from the job idempotency class.
                raise RemoteOutcomeUnknown("remote target outcome is unknown") from exc
        if response.status_code >= 500:
            raise RemoteOutcomeUnknown("remote target returned an indeterminate server failure")
        response.raise_for_status()
        self._verify_response(response)
        try:
            body = response.json()
            result = RemoteExecutionResult.model_validate(body["result"])
            encoded_artifact = body.get("artifact_base64")
            artifact = (
                None
                if encoded_artifact is None
                else base64.b64decode(encoded_artifact, validate=True)
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise RemoteOutcomeUnknown("remote target returned an invalid signed result") from exc
        if result.job_id != job.job_id:
            raise RemoteOutcomeUnknown("remote target result is bound to another job")
        return ConnectorOutcome(result=result, artifact_bytes=artifact)

    def cancel(self, job_id: str) -> None:
        body = {"job_id": job_id, "lease_id": self.lease_id, "fencing": self.lease_fencing}
        with httpx.Client(
            base_url=self.config.endpoint,
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
            headers={
                "authorization": f"Bearer {self.config.bearer_token}",
                "idempotency-key": f"cancel:{self.target_id}:{self.lease_id}:{job_id}",
                "x-operant-target-id": self.target_id,
                "x-operant-lease-id": self.lease_id,
                "x-operant-lease-token": self.lease_token,
                "x-operant-lease-fencing": str(self.lease_fencing),
            },
        ) as client:
            try:
                response = client.post("/v1/target/jobs/cancel", json=body)
            except httpx.HTTPError as exc:
                raise RemoteOutcomeUnknown("remote cancellation outcome is unknown") from exc
        if response.status_code >= 500:
            raise RemoteOutcomeUnknown("remote cancellation outcome is unknown")
        response.raise_for_status()
        self._verify_response(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise RemoteOutcomeUnknown("remote cancellation returned an invalid receipt") from exc
        if not isinstance(body, dict) or body.get("job_id") != job_id:
            raise RemoteOutcomeUnknown("remote cancellation receipt is bound to another job")

    def _verify_response(self, response: httpx.Response) -> None:
        if len(response.content) > self.config.max_response_bytes:
            raise RemoteOutcomeUnknown("remote target response exceeded the configured limit")
        signature = response.headers.get("x-operant-target-signature")
        if signature is None:
            raise RemoteOutcomeUnknown("remote target response is unsigned")
        _verify_signature(self.config.identity_public_key, response.content, signature)


def _decode_public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("remote target identity public key is invalid") from exc


def _verify_signature(public_key: str, body: bytes, signature: str) -> None:
    try:
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        _decode_public_key(public_key).verify(raw, hashlib.sha256(body).digest())
    except (ValueError, InvalidSignature) as exc:
        raise RemoteOutcomeUnknown("remote target response signature is invalid") from exc
