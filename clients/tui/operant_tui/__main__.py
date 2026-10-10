from __future__ import annotations

import argparse
from pathlib import Path

from sdk.python_client.caller_pairing import PairedSkillSourceClient

from .app import OperantTui
from .controller import ClientController


def main() -> None:
    parser = argparse.ArgumentParser(description="Operant Textual TUI")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--caller-state-dir",
        type=Path,
        help="独立配对客户端的受保护配置目录（绝对路径）",
    )
    args = parser.parse_args()
    if args.caller_state_dir is not None and not args.caller_state_dir.is_absolute():
        parser.error("--caller-state-dir 必须是绝对路径")
    caller = PairedSkillSourceClient(state_dir=args.caller_state_dir)
    OperantTui(ClientController(args.core_url, paired_skill_sources=caller)).run()


if __name__ == "__main__":
    main()
