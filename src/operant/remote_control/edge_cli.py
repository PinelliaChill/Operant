"""Launch the restricted TLS listener for a paired remote device."""

from __future__ import annotations

from pathlib import Path

import typer
import uvicorn

from operant.remote_control.edge import RemoteEdgeConfig, create_remote_edge_app

gateway_app = typer.Typer(
    no_args_is_help=True, help="只暴露配对、加密查询、WSS 和 Target 授权核验。"
)


@gateway_app.command("serve")
def serve(
    core_origin: str = typer.Option(...),
    core_ca_file: Path = typer.Option(...),
    allowed_origin: list[str] = typer.Option(..., help="设备 HTTPS Origin，可重复。"),
    tls_certfile: Path = typer.Option(...),
    tls_keyfile: Path = typer.Option(...),
    port: int = typer.Option(..., min=1, max=65535),
) -> None:
    if not tls_certfile.is_file() or not tls_keyfile.is_file():
        raise ValueError("Gateway TLS certificate and key are required")
    config = RemoteEdgeConfig(
        core_origin=core_origin,
        core_ca_file=core_ca_file,
        allowed_origins=frozenset(allowed_origin),
    )
    uvicorn.run(
        create_remote_edge_app(config),
        host="127.0.0.1",
        port=port,
        ws_max_size=config.max_bytes,
        proxy_headers=False,
        access_log=False,
        ssl_certfile=str(tls_certfile),
        ssl_keyfile=str(tls_keyfile),
    )
