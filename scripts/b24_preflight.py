"""Bounded formal discovery; inject credentials only into child process memory."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from operant.settings import load_local_env

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
os.environ["OPERANT_DB_PATH"] = str(
    Path(tempfile.mkdtemp(prefix="operant-b24-discovery-")).resolve() / "core.sqlite3"
)
os.environ["PYTHONPATH"] = str(root / "src")
os.environ["UV_CACHE_DIR"] = "/private/tmp/operant-uv-cache"
result = {"source_root": str(root), "entry": "uv run --no-sync operant model discover"}
try:
    p = subprocess.run(
        ["uv", "run", "--no-sync", "operant", "model", "discover"],
        cwd=root,
        env=os.environ,
        capture_output=True,
        text=True,
        timeout=45,
    )
    result.update(
        exit_code=p.returncode,
        models=[x.strip() for x in p.stdout.splitlines() if x.strip()] if p.returncode == 0 else [],
        error_class="none" if p.returncode == 0 else "discovery_failed",
    )
except subprocess.TimeoutExpired:
    result.update(exit_code=None, models=[], error_class="timeout")
(args.output or root / "docs/design/b2-4/preflight.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
