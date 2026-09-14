"""Conservatively choose CI suites; only known documentation can skip both."""

from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath

DOC_FILES = {
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "LICENSE",
}


def required_suites(paths: list[str]) -> tuple[bool, bool]:
    backend = gui = not paths
    for path in paths:
        item = PurePosixPath(path)
        if (
            path in DOC_FILES
            or (path.startswith("docs/") and item.suffix == ".md")
            or path.startswith(".github/ISSUE_TEMPLATE/")
            or path == ".github/pull_request_template.md"
        ):
            continue
        if path.startswith("clients/gui/"):
            gui = True
        elif path.startswith(("src/", "tests/", "plugins/", "clients/tui/")) or path in {
            "pyproject.toml",
            "uv.lock",
            ".python-version",
        }:
            backend = True
        else:
            # Public SDK, workflows, scripts, desktop and unknown paths need both.
            backend = gui = True
    return backend, gui


def main() -> None:
    base, head = sys.argv[1:]
    if not base or set(base) == {"0"}:
        paths: list[str] = []
    else:
        # Include both sides of renames, including code renamed to documentation.
        changed = subprocess.check_output(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head]
        )
        paths = [value.decode("utf-8", errors="replace") for value in changed.split(b"\0") if value]
    backend, gui = required_suites(paths)
    print(f"backend={str(backend).lower()}")
    print(f"gui={str(gui).lower()}")


if __name__ == "__main__":
    main()
