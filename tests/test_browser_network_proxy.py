from __future__ import annotations

import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from operant.remote.browser_proxy import BrowserNetworkProxy, _FilteringServer
from operant.remote.local_browser import BrowserTargetPolicy


def test_public_origin_cannot_resolve_to_loopback_even_with_local_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = BrowserTargetPolicy(frozenset({"https://example.com", "http://127.0.0.1:8765"}))
    original = socket.getaddrinfo

    def rebound(host: str, port: int, *args: object, **kwargs: object) -> list[tuple]:
        if host == "example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebound)
    broker = object.__new__(_FilteringServer)
    broker.policy = policy
    broker._blocked_ports = set()
    with pytest.raises(RuntimeError, match="private"):
        broker.address_for("https://example.com/")
    with pytest.raises(RuntimeError, match="private"):
        policy.check_url("https://example.com/")
    with pytest.raises(RuntimeError, match="private"):
        policy.pinned_resolvers()


def test_local_origin_cannot_resolve_to_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = BrowserTargetPolicy(frozenset({"http://localhost:8765"}))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))
        ],
    )
    with pytest.raises(RuntimeError, match="private"):
        policy.check_url("http://localhost:8765/")
    broker = object.__new__(_FilteringServer)
    broker.policy = policy
    broker._blocked_ports = set()
    with pytest.raises(RuntimeError, match="private"):
        broker.address_for("http://localhost:8765/")


def test_proxy_enforces_host_protocol_and_mutation_window() -> None:
    if os.environ.get("OPERANT_LOCAL_BROWSER_TEST") != "1":
        pytest.skip("set OPERANT_LOCAL_BROWSER_TEST=1 for loopback egress acceptance")
    posts: list[bytes] = []

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"approved page"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            posts.append(body)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    page_thread = Thread(target=server.serve_forever, daemon=True)
    page_thread.start()
    proxy = BrowserNetworkProxy(
        BrowserTargetPolicy(frozenset({f"http://127.0.0.1:{server.server_port}"}))
    )
    proxy.start()
    try:
        with httpx.Client(
            proxy=f"http://127.0.0.1:{proxy.port}", trust_env=False, timeout=3
        ) as client:
            approved = f"http://127.0.0.1:{server.server_port}/"
            assert client.get(approved).text == "approved page"
            assert client.get("http://127.0.0.2:1234/private").status_code == 403
            assert client.post(approved, content=b"blocked").status_code == 403
            assert (
                client.post(
                    approved, content=b"chunked", headers={"Transfer-Encoding": "chunked"}
                ).status_code
                == 403
            )
            assert posts == []
            proxy.grant_mutations()
            assert client.post(approved, content=b"approved").status_code == 200
            assert posts == [b"approved"]
            assert (
                client.get(
                    approved,
                    headers={"Upgrade": "websocket", "Connection": "Upgrade"},
                ).status_code
                == 403
            )
            proxy.block_port(server.server_port)
            assert client.get(approved).status_code == 403
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=3) as connection:
            connection.sendall(b"CONNECT 127.0.0.2:443 HTTP/1.1\r\nHost: 127.0.0.2\r\n\r\n")
            assert b"403" in connection.recv(1024)
    finally:
        proxy.close()
        server.shutdown()
        server.server_close()
        page_thread.join(timeout=3)
