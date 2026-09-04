from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.auth import AuthConfigurationError, OAuthConfig, oauth_config_from_env


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _config(tmp_path: Path) -> OAuthConfig:
    return OAuthConfig(
        issuer="https://issuer.example",
        client_id="operant-local",
        subject="local-user",
        redirect_uri="https://operant.example/internal/auth/callback",
        authorization_endpoint="https://issuer.example/authorize",
        token_endpoint="https://issuer.example/token",
        jwks_uri="https://issuer.example/jwks",
        token_store_path=(tmp_path / "oauth-secrets.json").absolute(),
        revocation_endpoint="https://issuer.example/revoke",
    )


def _jwt(
    private_key: rsa.RSAPrivateKey, nonce: str, *, extra_claims: dict[str, Any] | None = None
) -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": "test-key"}).encode())
    claims = _b64(
        json.dumps(
            {
                "iss": "https://issuer.example",
                "aud": "operant-local",
                "sub": "local-user",
                "nonce": nonce,
                "exp": int(time.time()) + 600,
                **(extra_claims or {}),
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(signature)}"


def _oauth_transport(private_key: rsa.RSAPrivateKey, state: dict[str, Any]) -> httpx.MockTransport:
    public_numbers = private_key.public_key().public_numbers()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["authorization_code"]
            assert body["redirect_uri"] == ["https://operant.example/internal/auth/callback"]
            assert len(body["code_verifier"][0]) >= 43
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "token_type": "Bearer",
                    "expires_in": 600,
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "id_token": _jwt(
                        private_key,
                        state["nonce"],
                        extra_claims=state.get("extra_claims"),
                    ),
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kty": "RSA",
                            "use": "sig",
                            "kid": "test-key",
                            "n": _b64(public_numbers.n.to_bytes(256, "big")),
                            "e": (
                                public_numbers.e
                                if state.get("malformed_jwk")
                                else _b64(
                                    public_numbers.e.to_bytes(
                                        (public_numbers.e.bit_length() + 7) // 8, "big"
                                    )
                                )
                            ),
                        }
                    ]
                },
            )
        if request.url.path == "/revoke":
            state["revoked"] = parse_qs(request.content.decode())["token"][0]
            return httpx.Response(200)
        raise AssertionError(f"unexpected OAuth request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def test_oauth_pkce_session_origin_and_logout(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    oauth_client = httpx.AsyncClient(transport=_oauth_transport(private_key, state))
    app = create_app(
        tmp_path / "operant.sqlite3",
        oauth_config=_config(tmp_path),
        oauth_http_client=oauth_client,
    )

    with TestClient(app, base_url="https://operant.example") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/projects").status_code == 401
        assert client.get("/internal/auth/status").json() == {"authenticated": False}

        login = client.get("/internal/auth/login", follow_redirects=False)
        assert login.status_code == 302
        assert "__Host-operant_oauth_transaction=" in login.headers["set-cookie"]
        query = parse_qs(urlsplit(login.headers["location"]).query)
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["state"][0]) >= 32
        state["nonce"] = query["nonce"][0]

        callback = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "one-time-code"},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert "HttpOnly" in callback.headers["set-cookie"]
        assert "Secure" in callback.headers["set-cookie"]
        assert "SameSite=lax" in callback.headers["set-cookie"]
        assert client.get("/v1/projects").status_code == 200

        rejected = client.post(
            "/v1/models/seed-defaults",
            headers={"Origin": "https://evil.example", "Forwarded": "host=operant.example"},
        )
        assert rejected.status_code == 403
        assert (
            client.post(
                "/internal/auth/logout", headers={"Origin": "https://operant.example"}
            ).status_code
            == 204
        )
        assert state["revoked"] == "access-secret"
        assert client.get("/v1/projects").status_code == 401

        replay = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "replayed-code"},
        )
        assert replay.status_code == 401

    stored = (tmp_path / "oauth-secrets.json").read_text(encoding="utf-8")
    assert "access-secret" not in stored
    assert "refresh-secret" not in stored
    assert json.loads(stored)["keys"] == {}
    database = (tmp_path / "operant.sqlite3").read_bytes()
    assert b"access-secret" not in database
    assert b"refresh-secret" not in database


def test_oauth_config_is_all_or_nothing_and_https_only(tmp_path: Path) -> None:
    assert oauth_config_from_env({}) is None
    with pytest.raises(AuthConfigurationError, match="incomplete OAuth configuration"):
        oauth_config_from_env({"OPERANT_OAUTH_ISSUER": "https://issuer.example"})
    with pytest.raises(AuthConfigurationError, match="issuer must be an HTTPS URL"):
        OAuthConfig(
            issuer="http://issuer.example",
            client_id="client",
            subject="local-user",
            redirect_uri="https://operant.example/internal/auth/callback",
            authorization_endpoint="https://issuer.example/authorize",
            token_endpoint="https://issuer.example/token",
            jwks_uri="https://issuer.example/jwks",
            token_store_path=(tmp_path / "tokens").absolute(),
        )


def test_id_token_signature_and_callback_shape_fail_closed(tmp_path: Path) -> None:
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state: dict[str, str] = {}
    oauth_client = httpx.AsyncClient(transport=_oauth_transport(wrong_key, state))
    app = create_app(
        tmp_path / "operant.sqlite3",
        oauth_config=_config(tmp_path),
        oauth_http_client=oauth_client,
    )
    with TestClient(app, base_url="https://operant.example") as client:
        login = client.get("/internal/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        # A callback with unexpected parameters is rejected before any token exchange.
        invalid = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code", "token": "implicit"},
        )
        assert invalid.status_code == 400

        wrong_redirect_host = client.get(
            "https://attacker.example/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
        )
        assert wrong_redirect_host.status_code == 400

        # The rejected callback did not consume state; a signed token with the wrong nonce fails.
        state["nonce"] = "wrong-nonce"
        failed = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
        )
        assert failed.status_code == 401
        assert client.get("/v1/projects").status_code == 401


def test_callback_requires_initiating_browser_and_malformed_jwk_fails_closed(
    tmp_path: Path,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state: dict[str, Any] = {"malformed_jwk": False}
    oauth_client = httpx.AsyncClient(transport=_oauth_transport(private_key, state))
    app = create_app(
        tmp_path / "operant.sqlite3",
        oauth_config=_config(tmp_path),
        oauth_http_client=oauth_client,
    )
    with TestClient(app, base_url="https://operant.example") as client:
        login = client.get("/internal/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        browser_binding = client.cookies["__Host-operant_oauth_transaction"]
        client.cookies.clear()
        unbound = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
        )
        assert unbound.status_code == 401

        # A browser-binding failure must not let a party holding only state consume the
        # initiating browser's transaction.
        client.cookies.set("__Host-operant_oauth_transaction", browser_binding)
        rebound = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
            follow_redirects=False,
        )
        assert rebound.status_code == 303
        assert (
            client.post(
                "/internal/auth/logout", headers={"Origin": "https://operant.example"}
            ).status_code
            == 204
        )

        login = client.get("/internal/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        state["malformed_jwk"] = True
        malformed = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
        )
        assert malformed.status_code == 401
        assert client.get("/v1/projects").status_code == 401


def test_token_store_file_is_owner_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state = {"nonce": "unused"}
    app = create_app(
        tmp_path / "operant.sqlite3",
        oauth_config=config,
        oauth_http_client=httpx.AsyncClient(transport=_oauth_transport(key, state)),
    )
    with TestClient(app, base_url="https://operant.example"):
        assert config.token_store_path.stat().st_mode & 0o777 == 0o600


def test_oauth_endpoints_and_redirect_are_strict(tmp_path: Path) -> None:
    values = {
        "issuer": "https://issuer.example",
        "client_id": "client",
        "subject": "local-user",
        "redirect_uri": "https://operant.example/internal/auth/callback",
        "authorization_endpoint": "https://issuer.example/authorize",
        "token_endpoint": "https://issuer.example/token",
        "jwks_uri": "https://issuer.example/jwks",
        "token_store_path": (tmp_path / "tokens").absolute(),
    }
    with pytest.raises(AuthConfigurationError, match="redirect_uri path"):
        OAuthConfig(**{**values, "redirect_uri": "https://operant.example/callback"})
    with pytest.raises(AuthConfigurationError, match="must not contain a query"):
        OAuthConfig(**{**values, "token_endpoint": "https://issuer.example/token?debug=1"})
    assert (
        OAuthConfig(
            **{**values, "redirect_uri": "https://operant.example:443/internal/auth/callback"}
        ).browser_origin
        == "https://operant.example"
    )


def test_callback_rejects_duplicate_parameters_without_consuming_state(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state: dict[str, Any] = {}
    app = create_app(
        tmp_path / "operant.sqlite3",
        oauth_config=_config(tmp_path),
        oauth_http_client=httpx.AsyncClient(transport=_oauth_transport(private_key, state)),
    )
    with TestClient(app, base_url="https://operant.example") as client:
        login = client.get("/internal/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        duplicated = client.get(
            "/internal/auth/callback",
            params=[("state", query["state"][0]), ("state", "other"), ("code", "code")],
        )
        assert duplicated.status_code == 400
        accepted = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303


@pytest.mark.parametrize(
    "extra_claims",
    [
        {"nbf": int(time.time()) + 600},
        {"iat": int(time.time()) + 600},
        {"exp": float("inf")},
    ],
)
def test_id_token_rejects_invalid_time_claims(tmp_path: Path, extra_claims: dict[str, Any]) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state: dict[str, Any] = {"extra_claims": extra_claims}
    app = create_app(
        tmp_path / "operant.sqlite3",
        oauth_config=_config(tmp_path),
        oauth_http_client=httpx.AsyncClient(transport=_oauth_transport(private_key, state)),
    )
    with TestClient(app, base_url="https://operant.example") as client:
        login = client.get("/internal/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        callback = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
        )
        assert callback.status_code == 401


def test_id_token_rejects_weak_rsa_key(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    state: dict[str, Any] = {}
    app = create_app(
        tmp_path / "operant.sqlite3",
        oauth_config=_config(tmp_path),
        oauth_http_client=httpx.AsyncClient(transport=_oauth_transport(private_key, state)),
    )
    with TestClient(app, base_url="https://operant.example") as client:
        login = client.get("/internal/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        state["nonce"] = query["nonce"][0]
        callback = client.get(
            "/internal/auth/callback",
            params={"state": query["state"][0], "code": "code"},
        )
        assert callback.status_code == 401
