"""Loopback egress broker for an isolated Chrome profile.

Chrome is configured to use this process-local proxy without a direct fallback.
The broker connects only to addresses that satisfy the target allowlist.
HTTP WebSocket upgrades are denied; CONNECT tunnels are limited to approved
hosts, including when Chrome uses CONNECT for WebSocket traffic.
"""

from __future__ import annotations

import ipaddress
import selectors
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit


class EgressPolicy(Protocol):
    @property
    def allowed_hosts(self) -> frozenset[str]: ...

    def check_url(self, value: str) -> str: ...


class _FilteringServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, policy: EgressPolicy) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.policy = policy
        self._mutation_lock = threading.Lock()
        self._mutation_until = 0.0
        self._mutation_remaining = 0
        self._blocked_ports: set[int] = {self.server_address[1]}

    def block_port(self, port: int) -> None:
        self._blocked_ports.add(port)

    def grant_mutations(self, *, seconds: float = 5.0, count: int = 10) -> None:
        with self._mutation_lock:
            self._mutation_until = time.monotonic() + seconds
            self._mutation_remaining = count

    def consume_mutation(self) -> bool:
        with self._mutation_lock:
            if time.monotonic() >= self._mutation_until or self._mutation_remaining <= 0:
                return False
            self._mutation_remaining -= 1
            return True

    def address_for(self, url: str) -> tuple[str, int]:
        self.policy.check_url(url)
        parsed = urlsplit(url)
        host = parsed.hostname
        if host is None:
            raise ValueError("egress target has no host")
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if port in self._blocked_ports:
            raise ValueError("browser egress cannot reach an internal control port")
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("egress target has no address")
        for item in addresses:
            address = item[4][0]
            ip = ipaddress.ip_address(address)
            local_target = host in {"localhost", "127.0.0.1"}
            if (local_target and not ip.is_loopback) or (not local_target and not ip.is_global):
                raise ValueError("egress target resolved to a private address")
        return str(addresses[0][4][0]), port


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15

    def log_message(self, *_: object) -> None:
        pass

    @property
    def broker(self) -> _FilteringServer:
        return cast(_FilteringServer, self.server)

    def _reject(self) -> None:
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_CONNECT(self) -> None:
        try:
            target = urlsplit(f"https://{self.path}/")
            if target.hostname is None:
                raise ValueError("invalid CONNECT host")
            address, port = self.broker.address_for(
                urlunsplit(("https", target.netloc, "/", "", ""))
            )
            upstream = socket.create_connection((address, port), timeout=5)
        except (OSError, ValueError, RuntimeError):
            self._reject()
            return
        try:
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(upstream)
        except OSError:
            pass
        finally:
            upstream.close()
            self.close_connection = True

    def _relay(self, upstream: socket.socket) -> None:
        transferred = 0
        with selectors.DefaultSelector() as selector:
            selector.register(self.connection, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, self.connection)
            while transferred < 32 * 1024 * 1024:
                ready = selector.select(timeout=15)
                if not ready:
                    break
                for key, _ in ready:
                    chunk = cast(socket.socket, key.fileobj).recv(65_536)
                    if not chunk:
                        return
                    destination = cast(socket.socket, key.data)
                    destination.sendall(chunk)
                    transferred += len(chunk)

    def do_GET(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def _forward(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.scheme != "http" or parsed.hostname is None:
            self._reject()
            return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._reject()
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Proxy-Authorization"):
            self._reject()
            return
        if self.command not in {"GET", "HEAD"} and not self.broker.consume_mutation():
            self._reject()
            return
        length_header = self.headers.get("Content-Length", "0")
        if not length_header.isdigit() or int(length_header) > 1_000_000:
            self._reject()
            return
        try:
            address, port = self.broker.address_for(self.path)
            upstream = socket.create_connection((address, port), timeout=5)
        except (OSError, ValueError, RuntimeError):
            self._reject()
            return
        try:
            upstream.settimeout(15)
            request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            lines = [f"{self.command} {request_target} HTTP/1.1\r\n".encode()]
            lines.append(f"Host: {parsed.netloc}\r\n".encode())
            for key, value in self.headers.items():
                if key.lower() not in {
                    "host",
                    "connection",
                    "proxy-connection",
                    "proxy-authorization",
                }:
                    lines.append(f"{key}: {value}\r\n".encode())
            lines.append(b"Connection: close\r\n\r\n")
            upstream.sendall(b"".join(lines))
            remaining = int(length_header)
            while remaining:
                body = self.rfile.read(min(remaining, 65_536))
                if not body:
                    return
                upstream.sendall(body)
                remaining -= len(body)
            transferred = 0
            while transferred < 32 * 1024 * 1024:
                chunk = upstream.recv(65_536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
                transferred += len(chunk)
        except OSError:
            self.close_connection = True
        finally:
            upstream.close()
            self.close_connection = True


class BrowserNetworkProxy:
    def __init__(self, policy: EgressPolicy) -> None:
        self.server = _FilteringServer(policy)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def grant_mutations(self) -> None:
        self.server.grant_mutations()

    def block_port(self, port: int) -> None:
        self.server.block_port(port)

    def close(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=3)
        self.server.server_close()
