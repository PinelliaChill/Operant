from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from operant.remote_control.edge import RemoteEdgeConfig, create_remote_edge_app


def _config(**changes: object) -> RemoteEdgeConfig:
    values = {
        "core_origin": "https://127.0.0.1:18805",
        "core_ca_file": Path(certifi.where()),
        "allowed_origins": frozenset({"https://device.example"}),
        **changes,
    }
    return RemoteEdgeConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:18805",
        "https://example.com:443",
        "https://127.0.0.1:18805/v1",
        "https://127.0.0.1:18805/?route=admin",
        "https://user:password@127.0.0.1:18805",
        "https://127.0.0.1:18805/#admin",
        "https://127.0.0.1",
    ],
)
def test_edge_rejects_ambiguous_or_nonlocal_upstream(origin: str) -> None:
    with pytest.raises(ValueError, match="Core origin"):
        _config(core_origin=origin)


def test_edge_verifies_ca_and_hostname() -> None:
    context = _config().ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    with pytest.raises(ValueError, match="byte limit"):
        _config(max_bytes=1024 * 1024)
    with pytest.raises(ValueError, match="HTTPS origin"):
        _config(allowed_origins=frozenset({"https://device.example/path"}))


def test_edge_blocks_management_and_docs_without_contacting_core() -> None:
    calls: list[str] = []

    def core(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    app = create_remote_edge_app(_config(), transport=httpx.MockTransport(core))
    with TestClient(app) as client:
        for path in (
            "/v1/sessions",
            "/v1/remote-control/hosts/enable",
            "/v1/remote-control/sessions",
            "/v1/remote-control/devices/device_1/revoke",
            "/v1/remote-targets",
            "/web",
            "/docs",
            "/openapi.json",
        ):
            assert client.post(path, json={}).status_code == 404
            assert client.get(path).status_code == 404
        assert client.get("/v1/remote-control/devices/pair").status_code == 405
    assert calls == []


def test_edge_forwards_only_exact_paths_and_safe_headers() -> None:
    calls: list[httpx.Request] = []

    def core(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"encrypted": "bounded"}, headers={"Set-Cookie": "bad=1"})

    app = create_remote_edge_app(_config(), transport=httpx.MockTransport(core))
    with TestClient(app) as client:
        for path in ("/v1/remote-control/devices/pair", "/v1/remote-control/session-query"):
            response = client.post(
                path,
                json={"ciphertext": "test"},
                headers={
                    "Authorization": "Bearer should-not-reach-management",
                    "Cookie": "local_auth=should-not-reach-core",
                    "X-Forwarded-Host": "evil.example",
                    "Idempotency-Key": "exact-key",
                },
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
        assert client.post(f"{path}?path=/v1/sessions", json={}).status_code == 400
        assert client.post(f"{path}/", json={}).status_code == 404
    assert len(calls) == 2
    for request in calls:
        assert request.url.host == "127.0.0.1" and request.url.port == 18805
        assert request.headers["idempotency-key"] == "exact-key"
        for header in ("authorization", "cookie", "x-forwarded-host"):
            assert header not in request.headers


@pytest.mark.parametrize("kind", ["redirect", "oversized", "unavailable"])
def test_edge_rejects_redirect_and_unbounded_or_unavailable_core(kind: str) -> None:
    def core(request: httpx.Request) -> httpx.Response:
        if kind == "redirect":
            return httpx.Response(307, headers={"Location": "https://evil.example"})
        if kind == "oversized":
            return httpx.Response(200, content=b"x" * (512 * 1024 + 1))
        raise httpx.ConnectError("unavailable", request=request)

    app = create_remote_edge_app(_config(), transport=httpx.MockTransport(core))
    with TestClient(app) as client:
        response = client.post("/v1/remote-control/session-query", json={})
    assert response.status_code == 502
    assert not response.content


def test_edge_request_limit_prevents_forwarding() -> None:
    def core(_: httpx.Request) -> httpx.Response:
        pytest.fail("oversized request reached Core")

    app = create_remote_edge_app(_config(), transport=httpx.MockTransport(core))
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/remote-control/session-query",
                content=b"x" * (512 * 1024 + 1),
                headers={"Content-Type": "application/json"},
            ).status_code
            == 413
        )


def test_edge_worker_verification_cannot_reach_other_target_operations() -> None:
    paths: list[str] = []

    def core(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"valid": True})

    app = create_remote_edge_app(_config(), transport=httpx.MockTransport(core))
    with TestClient(app) as client:
        path = "/v1/remote-targets/target_123/leases"
        assert client.post(f"{path}/verify", json={"job_id": "job_1"}).status_code == 200
        for blocked in (path, f"{path}/release", f"{path}/renew", f"{path}/verify/extra"):
            assert client.post(blocked, json={}).status_code == 404
        assert client.post("/v1/remote-targets/../leases/verify", json={}).status_code == 404
        assert client.post("/v1/remote-targets/bad.id/leases/verify", json={}).status_code == 400
    assert paths == [f"{path}/verify"]


@pytest.mark.parametrize(
    "headers,subprotocols",
    [
        (
            {"Origin": "https://evil.example", "Authorization": f"Bearer {'t' * 32}"},
            ["operant.remote.v1"],
        ),
        ({"Origin": "https://device.example"}, ["operant.remote.v1"]),
        ({"Origin": "https://device.example", "Authorization": f"Bearer {'t' * 32}"}, []),
    ],
)
def test_edge_rejects_invalid_websocket_handshake(
    headers: dict[str, str], subprotocols: list[str]
) -> None:
    app = create_remote_edge_app(_config())
    with (
        TestClient(app, base_url="https://127.0.0.1:18807") as client,
        pytest.raises(WebSocketDisconnect) as error,
        client.websocket_connect(
            "wss://127.0.0.1:18807/v1/remote-control/gateway",
            headers=headers,
            subprotocols=subprotocols,
        ),
    ):
        pass
    assert error.value.code == 1008
