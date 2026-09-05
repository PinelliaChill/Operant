from __future__ import annotations

import argparse

from .app import OperantTui
from .controller import ClientController


def main() -> None:
    parser = argparse.ArgumentParser(description="Operant Textual TUI")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    OperantTui(ClientController(args.core_url)).run()


if __name__ == "__main__":
    main()
