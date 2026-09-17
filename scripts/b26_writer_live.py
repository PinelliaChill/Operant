"""Bounded real-model Writer promotion on a fresh synthetic Git workspace.

This script is an acceptance harness, not a test fixture.  It loads the local
dotenv only into this process, verifies the exact discovered model, and writes
sanitized evidence without provider responses or credential values.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _source_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "src/operant").rglob("*.py"))
    }


def _load_writer_helper(root: Path) -> Any:
    path = root / "tests/test_b26_writer_integration.py"
    spec = importlib.util.spec_from_file_location("b26_writer_integration_helper", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("writer_helper_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    helper = getattr(module, "run_writer_chain", None)
    if not callable(helper):
        raise RuntimeError("writer_helper_unavailable")
    return helper


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--model-id",
        default="gpt-5.6-luna",
        choices=("gpt-5.6-luna",),
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    runroot = args.run_root.expanduser()
    if not runroot.is_absolute():
        raise SystemExit("--run-root must be absolute")
    runroot = runroot.resolve()
    runroot.mkdir(parents=True, exist_ok=False)

    from operant.settings import load_local_env

    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    base_url = os.environ.get("OPERANT_BASE_URL")
    if not base_url:
        raise SystemExit("OPERANT_BASE_URL is required")

    database = runroot / "core.sqlite3"
    result: dict[str, Any] = {
        "phase": "b26_real_writer_joint_acceptance",
        "model_id": args.model_id,
        "run_root": str(runroot),
        "database": str(database),
        "source_hashes": _source_hashes(root),
        "checks": {},
        "completed": False,
    }
    checks = result["checks"]
    os.environ["OPERANT_DB_PATH"] = str(runroot / "import.sqlite3")
    try:
        discovery = subprocess.run(
            ["uv", "run", "--frozen", "operant", "model", "discover"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=50,
        )
        discovered = {line.strip() for line in discovery.stdout.splitlines()}
        if discovery.returncode != 0 or args.model_id not in discovered:
            raise RuntimeError("discovery_failed")
        checks["discovery"] = {
            "entry": "uv run --frozen operant model discover",
            "selected": args.model_id,
        }

        # Importing operant.api creates its module-level app. Keep that import
        # on a separate fresh file; the helper owns the actual acceptance DB.
        os.environ["OPERANT_DB_PATH"] = str(runroot / "import.sqlite3")
        helper = _load_writer_helper(root)
        os.environ["OPERANT_DB_PATH"] = str(database)
        from operant.domain.models import Budget, ModelProfile
        from operant.providers.openai_compatible import OpenAICompatibleProvider

        profile = ModelProfile(
            name="B2-6 Writer real model",
            model_id=args.model_id,
            base_url=base_url,
            secret_ref="OPERANT_API_KEY",
            context_window=32768,
        )
        chain = asyncio.run(
            helper(
                runroot,
                model_profile=profile,
                provider=OpenAICompatibleProvider(timeout_seconds=120),
                role_budget=Budget(max_turns=2, max_output_tokens=1024, timeout_seconds=120),
            )
        )
        checks["writer_chain"] = chain
        result["completed"] = True
    except Exception as exc:
        result["error_class"] = type(exc).__name__
        if isinstance(exc, RuntimeError) and str(exc) in {
            "discovery_failed",
            "writer_helper_unavailable",
        }:
            result["failure_code"] = str(exc)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "completed": result["completed"],
                    "checks": list(checks),
                    "error_class": result.get("error_class"),
                    "failure_code": result.get("failure_code"),
                },
                ensure_ascii=False,
            )
        )
    if not result["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
