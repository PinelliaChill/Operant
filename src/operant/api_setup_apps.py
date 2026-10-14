"""Read local application names for explicit computer-control target selection."""

from __future__ import annotations

import os
import plistlib
import re
import stat
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from operant.api_model_connections import _trusted_local
from operant.contracts.onboarding import LocalApplication, LocalApplicationList

_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,199}$")
_MANIFEST_LIMIT = 1_048_576


def _manifest(app: Path) -> dict[str, object]:
    # Open each component without following links, including Contents. A malformed
    # bundle must not read outside its directory or block the application listing.
    flags = os.O_RDONLY | os.O_NOFOLLOW
    with ExitStack() as stack:
        app_fd = os.open(app, flags | os.O_DIRECTORY)
        stack.callback(os.close, app_fd)
        contents_fd = os.open("Contents", flags | os.O_DIRECTORY, dir_fd=app_fd)
        stack.callback(os.close, contents_fd)
        manifest_fd = os.open("Info.plist", flags | os.O_NONBLOCK, dir_fd=contents_fd)
        stack.callback(os.close, manifest_fd)
        details = os.fstat(manifest_fd)
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MANIFEST_LIMIT:
            return {}
        # A concurrent replacement/growth also remains bounded.
        body = os.read(manifest_fd, _MANIFEST_LIMIT + 1)
        if len(body) > _MANIFEST_LIMIT:
            return {}
        data = plistlib.loads(body)
        return data if isinstance(data, dict) else {}


def local_applications(roots: tuple[Path, ...]) -> list[LocalApplication]:
    found: dict[str, LocalApplication] = {}
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        # Scan only installed bundles and one Utilities folder, never user documents.
        for directory in (root, root / "Utilities"):
            if directory.is_symlink():
                continue
            try:
                entries = tuple(directory.iterdir())[:1000]
            except OSError:
                continue
            for app in entries:
                if app.suffix != ".app" or app.is_symlink():
                    continue
                try:
                    data = _manifest(app)
                    bundle_id = data.get("CFBundleIdentifier")
                    name = data.get("CFBundleDisplayName") or data.get("CFBundleName") or app.stem
                    if (
                        isinstance(bundle_id, str)
                        and _BUNDLE_ID.fullmatch(bundle_id)
                        and isinstance(name, str)
                        and 0 < len(name) <= 200
                        and not any(ord(char) < 32 for char in name)
                    ):
                        found.setdefault(
                            bundle_id, LocalApplication(bundle_id=bundle_id, name=name)
                        )
                except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
                    continue
    return sorted(found.values(), key=lambda item: (item.name.casefold(), item.bundle_id))


def install_setup_app_routes(
    app: FastAPI, *, local_authorizer: Callable[[Request], bool] | None = None
) -> None:
    @app.get(
        "/v1/setup/local-apps",
        operation_id="listLocalApplications",
        response_model=LocalApplicationList,
    )
    def list_apps(request: Request) -> LocalApplicationList:
        if not _trusted_local(request, local_authorizer):
            raise HTTPException(
                status_code=403, detail="local application listing requires a local client"
            )
        roots = (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications")
        # Linux/Windows do not pretend to provide macOS application targets.
        return LocalApplicationList(
            items=local_applications(roots) if sys.platform == "darwin" else []
        )
