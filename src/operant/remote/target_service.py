"""Private, lease-bound HTTP Target for a trusted remote workspace."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException, Request, Response

from operant.domain.remote_execution import (
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
)
from operant.protocol import redact_public_text
from operant.tools.execution import is_protected_workspace_name

_TARGET_PRIVATE_NAMES = frozenset({".ssh", ".aws", ".config", ".kube", ".codex"})


def _private_component(name: str) -> bool:
    return is_protected_workspace_name(name) or name.lower() in _TARGET_PRIVATE_NAMES


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class TargetServiceConfig:
    target_id: str
    lease_id: str
    lease_token: str
    lease_fencing: int
    lease_expires_at: datetime
    bearer_token: str
    signing_private_key: bytes
    workspace: Path
    ledger_path: Path
    allowed_argv: tuple[tuple[str, ...], ...]
    core_origin: str
    core_ca_file: Path
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        if not 1 <= len(self.target_id) <= 200 or not 1 <= len(self.lease_id) <= 200:
            raise ValueError("invalid Target or Lease ID")
        if not 16 <= len(self.lease_token) <= 300 or self.lease_fencing < 1:
            raise ValueError("invalid Target Lease binding")
        if not 32 <= len(self.bearer_token) <= 512:
            raise ValueError("Target bearer token is invalid")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("Target Lease expiry must have a timezone")
        if len(self.signing_private_key) != 32:
            raise ValueError("Target signing key is invalid")
        if not self.workspace.is_absolute() or not self.workspace.is_dir():
            raise ValueError("Target workspace must be an existing absolute directory")
        if not self.ledger_path.is_absolute():
            raise ValueError("Target ledger path must be absolute")
        core = httpx.URL(self.core_origin)
        if (
            core.scheme != "https"
            or core.host not in {"127.0.0.1", "::1", "localhost"}
            or core.port is None
            or core.path not in {"", "/"}
            or core.query
            or core.fragment
            or core.userinfo
            or not self.core_ca_file.is_absolute()
            or not self.core_ca_file.is_file()
        ):
            raise ValueError("Target Core authority must be an HTTPS loopback origin with a CA")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("Target command deadline is invalid")
        for argv in self.allowed_argv:
            if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
                raise ValueError("allowed executable must be an existing absolute file")
            if any(not item or len(item) > 1000 for item in argv):
                raise ValueError("allowed command arguments are invalid")


class TargetService:
    def __init__(self, config: TargetServiceConfig) -> None:
        self.config = config
        self.workspace = config.workspace.resolve(strict=True)
        self._active: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()
        config.ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if config.ledger_path.is_symlink():
            raise ValueError("Target ledger cannot be a symlink")
        descriptor = os.open(config.ledger_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        if config.ledger_path.stat().st_mode & 0o077:
            raise ValueError("Target ledger must be a private 0600 file")
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                "status TEXT NOT NULL, result_json TEXT, job_json TEXT, "
                "cancellation_requested INTEGER NOT NULL DEFAULT 0)"
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "job_json" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN job_json TEXT")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.config.ledger_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def authorize(self, request: Request) -> None:
        headers = request.headers
        bindings = (
            headers.get("x-operant-target-id") == self.config.target_id,
            headers.get("x-operant-lease-id") == self.config.lease_id,
            headers.get("x-operant-lease-fencing") == str(self.config.lease_fencing),
            hmac.compare_digest(headers.get("x-operant-lease-token", ""), self.config.lease_token),
            hmac.compare_digest(
                headers.get("authorization", ""), f"Bearer {self.config.bearer_token}"
            ),
        )
        if not all(bindings):
            raise HTTPException(status_code=403, detail="Target Lease or identity is invalid")

    def signed_response(self, body: dict[str, Any]) -> Response:
        content = _canonical(body)
        signature = Ed25519PrivateKey.from_private_bytes(self.config.signing_private_key).sign(
            hashlib.sha256(content).digest()
        )
        response = Response(content, media_type="application/json")
        response.headers["x-operant-target-signature"] = _b64(signature)
        response.headers["Cache-Control"] = "no-store"
        return response

    def verify_core_job(self, job: RemoteExecutionJob, *, purpose: str = "execute") -> None:
        payload = {
            "lease_id": self.config.lease_id,
            "token": self.config.lease_token,
            "fencing": self.config.lease_fencing,
            "workspace_ref": str(self.workspace),
            "job": job.model_dump(mode="json"),
            "purpose": purpose,
        }
        try:
            with httpx.Client(
                verify=str(self.config.core_ca_file),
                trust_env=False,
                follow_redirects=False,
                timeout=10,
            ) as client:
                response = client.post(
                    f"{self.config.core_origin.rstrip('/')}/v1/remote-targets/"
                    f"{self.config.target_id}/leases/verify",
                    json=payload,
                )
            verified = response.json()
            if (
                response.status_code != 200
                or not isinstance(verified, dict)
                or verified.get("valid") is not True
                or verified.get("job_id") != job.job_id
            ):
                raise ValueError("Core rejected the Target Job")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                status_code=403, detail="Core authorization is unavailable"
            ) from exc

    def execute(self, job: RemoteExecutionJob) -> RemoteExecutionResult:
        if (
            job.target_id != self.config.target_id
            or job.lease_id != self.config.lease_id
            or job.lease_fencing != self.config.lease_fencing
            or job.capability not in {RemoteCapability.TARGET_READ, RemoteCapability.TARGET_EXEC}
            or (
                job.capability is RemoteCapability.TARGET_EXEC
                and job.idempotency is not RemoteActionIdempotency.NON_IDEMPOTENT
            )
        ):
            raise HTTPException(status_code=403, detail="Target Job is outside bound Lease")
        self.verify_core_job(job)
        digest = hashlib.sha256(_canonical(job.model_dump(mode="json"))).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    raise HTTPException(status_code=409, detail="Target Job replay has changed")
                if row["status"] == "done" and row["result_json"] is not None:
                    return RemoteExecutionResult.model_validate_json(row["result_json"])
                # A prior process may have reached the side effect; never replay it.
                result = self._result(
                    job, RemoteJobStatus.MANUAL_RECONCILE_REQUIRED, "remote.outcome_unknown"
                )
                db.execute(
                    "UPDATE jobs SET status='unknown', result_json=? WHERE job_id=?",
                    (result.model_dump_json(), job.job_id),
                )
                return result
            db.execute(
                "INSERT INTO jobs(job_id,digest,status,job_json) VALUES(?,?,'running',?)",
                (job.job_id, digest, job.model_dump_json()),
            )
        result = self._perform(job)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT status,cancellation_requested FROM jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            if current is None or current["status"] == "unknown":
                return self._result(
                    job, RemoteJobStatus.MANUAL_RECONCILE_REQUIRED, "remote.outcome_unknown"
                )
            if current["cancellation_requested"]:
                result = self._result(job, RemoteJobStatus.CANCELLED, "remote.cancelled")
            db.execute(
                "UPDATE jobs SET status='done', result_json=? WHERE job_id=?",
                (result.model_dump_json(), job.job_id),
            )
        return result

    def _result(
        self,
        job: RemoteExecutionJob,
        status: RemoteJobStatus,
        error_code: str | None = None,
        *,
        postcondition: dict[str, Any] | None = None,
    ) -> RemoteExecutionResult:
        return RemoteExecutionResult(
            job_id=job.job_id,
            result_idempotency_key=f"remote-target:{job.job_id}",
            status=status,
            postcondition=postcondition or {},
            error_code=error_code,
        )

    def _perform(self, job: RemoteExecutionJob) -> RemoteExecutionResult:
        if job.capability is RemoteCapability.TARGET_READ and job.operation == "read_text":
            path = job.arguments.get("path")
            if (
                not isinstance(path, str)
                or not path
                or Path(path).is_absolute()
                or set(job.arguments) != {"path"}
                or any(part in {".", ".."} or _private_component(part) for part in Path(path).parts)
            ):
                return self._result(job, RemoteJobStatus.FAILED, "remote.invalid_path")
            target = (self.workspace / path).resolve(strict=False)
            if (
                not target.is_relative_to(self.workspace)
                or any(
                    _private_component(part) for part in target.relative_to(self.workspace).parts
                )
                or not target.is_file()
            ):
                return self._result(job, RemoteJobStatus.FAILED, "remote.path_denied")
            try:
                with target.open("rb") as source:
                    raw = source.read(8193)
                if len(raw) > 8192:
                    raise ValueError("file too large")
                content = raw.decode("utf-8")
            except (OSError, UnicodeError, ValueError):
                return self._result(job, RemoteJobStatus.FAILED, "remote.read_failed")
            return self._result(
                job,
                RemoteJobStatus.SUCCEEDED,
                postcondition={"content": redact_public_text(content, max_chars=8192)},
            )
        if job.capability is not RemoteCapability.TARGET_EXEC or job.operation != "run_allowlisted":
            return self._result(job, RemoteJobStatus.FAILED, "remote.operation_denied")
        argv = job.arguments.get("argv")
        if (
            set(job.arguments) != {"argv"}
            or not isinstance(argv, list)
            or not all(isinstance(item, str) for item in argv)
            or tuple(argv) not in self.config.allowed_argv
        ):
            return self._result(job, RemoteJobStatus.FAILED, "remote.argv_denied")
        try:
            process = subprocess.Popen(
                cast(list[str], argv),
                cwd=self.workspace,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            with self._lock:
                self._active[job.job_id] = process
            try:
                output = self._collect_bounded(process)
                if output is None:
                    return self._result(
                        job,
                        RemoteJobStatus.MANUAL_RECONCILE_REQUIRED,
                        "remote.output_or_timeout_unknown",
                    )
                stdout, stderr = output
            finally:
                with self._lock:
                    self._active.pop(job.job_id, None)
        except OSError:
            return self._result(job, RemoteJobStatus.FAILED, "remote.launch_failed")
        return self._result(
            job,
            RemoteJobStatus.SUCCEEDED if process.returncode == 0 else RemoteJobStatus.FAILED,
            None if process.returncode == 0 else "remote.exit_nonzero",
            postcondition={
                "exit_code": process.returncode,
                "stdout": redact_public_text(
                    stdout.decode("utf-8", errors="replace"), max_chars=8192
                ),
                "stderr": redact_public_text(
                    stderr.decode("utf-8", errors="replace"), max_chars=8192
                ),
            },
        )

    def _collect_bounded(self, process: subprocess.Popen[bytes]) -> tuple[bytes, bytes] | None:
        import time

        deadline = time.monotonic() + self.config.timeout_seconds
        stdout_stream, stderr_stream = process.stdout, process.stderr
        if stdout_stream is None or stderr_stream is None:
            return None
        streams = (stdout_stream, stderr_stream)
        output = [bytearray(), bytearray()]
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate(streams):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                for key, _ in selector.select(timeout=min(remaining, 0.2)):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    bucket = output[cast(int, key.data)]
                    if len(bucket) + len(chunk) > 8192:
                        remaining = -1
                        break
                    bucket.extend(chunk)
                if remaining < 0:
                    break
            else:
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
                else:
                    return bytes(output[0]), bytes(output[1])
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        for stream in streams:
            stream.close()
        process.wait(timeout=5)
        return None

    def cancel(self, job_id: str) -> dict[str, str]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,job_json FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                return {"job_id": job_id, "status": "not_found"}
            if row["job_json"] is None:
                raise HTTPException(status_code=403, detail="Target Job identity is unavailable")
            self.verify_core_job(
                RemoteExecutionJob.model_validate_json(row["job_json"]), purpose="cancel"
            )
            db.execute("UPDATE jobs SET cancellation_requested=1 WHERE job_id=?", (job_id,))
        with self._lock:
            process = self._active.get(job_id)
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        return {"job_id": job_id, "status": "cancel_requested"}


def create_target_app(config: TargetServiceConfig) -> FastAPI:
    service = TargetService(config)
    app = FastAPI(title="Operant Private Target")
    app.state.target_service = service

    @app.post("/v1/target/jobs/execute")
    def execute(job: RemoteExecutionJob, request: Request) -> Response:
        service.authorize(request)
        result = service.execute(job)
        return service.signed_response({"result": result.model_dump(mode="json")})

    @app.post("/v1/target/jobs/cancel")
    def cancel(body: dict[str, Any], request: Request) -> Response:
        service.authorize(request)
        if (
            set(body) != {"job_id", "lease_id", "fencing"}
            or body["lease_id"] != config.lease_id
            or body["fencing"] != config.lease_fencing
            or not isinstance(body["job_id"], str)
        ):
            raise HTTPException(status_code=403, detail="Target cancellation binding changed")
        return service.signed_response(service.cancel(body["job_id"]))

    return app
