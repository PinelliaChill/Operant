from __future__ import annotations

from pathlib import Path

import pytest

from operant.server import ServerConfigurationError, build_server_config, server_config_snapshot


def test_loopback_server_is_bounded_without_remote_gateway(tmp_path: Path) -> None:
    config = build_server_config(
        host="127.0.0.1",
        port=8765,
        ssl_certfile=None,
        ssl_keyfile=None,
        environment={"OPERANT_DB_PATH": str(tmp_path / "operant.sqlite3")},
    )
    assert server_config_snapshot(config) == {
        "host": "127.0.0.1",
        "port": 8765,
        "ssl": False,
        "ws_max_size": 512 * 1024,
        "proxy_headers": False,
        "workers": 1,
    }


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "core.example"])
def test_server_rejects_wildcard_public_and_ambiguous_hosts(host: str) -> None:
    with pytest.raises(ServerConfigurationError):
        build_server_config(
            host=host,
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            environment={},
        )


def test_gateway_limit_is_wired_to_uvicorn_and_requires_tls(tmp_path: Path) -> None:
    environment = {
        "OPERANT_DB_PATH": str(tmp_path / "operant.sqlite3"),
        "OPERANT_REMOTE_GATEWAY_TOKEN_REF": "OPERANT_GATEWAY_TOKEN",
        "OPERANT_GATEWAY_TOKEN": "x" * 32,
        "OPERANT_REMOTE_GATEWAY_ALLOWED_ORIGINS_JSON": '["https://control.example"]',
        "OPERANT_REMOTE_GATEWAY_MAX_FRAME_BYTES": "4096",
    }
    with pytest.raises(ServerConfigurationError, match="TLS/WSS"):
        build_server_config(
            host="127.0.0.1",
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            environment=environment,
        )


def test_private_network_requires_tls_and_oauth() -> None:
    with pytest.raises(ServerConfigurationError, match="TLS and OAuth"):
        build_server_config(
            host="192.168.1.20",
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            environment={},
        )


def test_desktop_bridge_has_exact_origin_and_stays_on_loopback(tmp_path: Path) -> None:
    config = build_server_config(
        host="127.0.0.1",
        port=8000,
        ssl_certfile=None,
        ssl_keyfile=None,
        desktop=True,
        environment={"OPERANT_DB_PATH": str(tmp_path / "desktop.sqlite3")},
    )
    middleware = [
        item for item in config.app.user_middleware if item.cls.__name__ == "CORSMiddleware"
    ]
    assert len(middleware) == 1
    assert middleware[0].kwargs["allow_origins"] == [
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ]
    with pytest.raises(ServerConfigurationError, match="desktop bridge"):
        build_server_config(
            host="192.168.1.20",
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            desktop=True,
            environment={},
        )
