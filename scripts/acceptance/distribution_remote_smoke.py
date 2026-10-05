"""Use an installed wheel in a fresh private directory on the second machine."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import time
import urllib.request
from pathlib import Path


def _run(binary: Path, *args: str, env: dict[str, str], cwd: Path) -> str:
    result = subprocess.run(
        [str(binary), *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    assert root.is_absolute() and root.is_dir()
    venv = root / "venv"
    data = root / "data"
    data.mkdir(mode=0o700, exist_ok=False)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["OPERANT_DB_PATH"] = str(data / "operant.sqlite3")
    _run(venv / "bin" / "operant", "init", env=env, cwd=data)
    _run(venv / "bin" / "operant", "--help", env=env, cwd=data)
    _run(venv / "bin" / "operant-tui", "--help", env=env, cwd=data)
    inspect = r"""
import json
from pathlib import Path
import operant, docx, pptx, reportlab, pypdf
from operant.package_resources import default_skill_root
from operant.persistence.sqlite import SQLiteStore
p = default_skill_root()
names = sorted(x.name for x in p.iterdir() if (x / 'SKILL.md').is_file())
assert names == sorted(('grill-me','documents','presentations','pdf','skill-creator','find-skills'))
assert 'site-packages' in str(Path(operant.__file__).resolve())
print(json.dumps({'schema': SQLiteStore('operant.sqlite3').schema_version(),
                  'module': str(Path(operant.__file__).resolve()),
                  'skills': names, 'artifacts_extra': True}))
"""
    package = json.loads(
        _run(venv / "bin" / "python", "-I", "-c", inspect, env=env, cwd=data).splitlines()[-1]
    )
    assert package["schema"] == 23
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    process = subprocess.Popen(
        [str(venv / "bin" / "operant"), "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=data,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    healthy = False
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("installed Core exited before health check")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as reply:
                    healthy = reply.status == 200 and json.load(reply) == {"status": "ok"}
                if healthy:
                    break
            except OSError:
                time.sleep(0.25)
        assert healthy, "installed Core health check timed out"
    finally:
        process.terminate()
        process.wait(timeout=10)
    wheel = root / "core" / "operant_agent-0.1.0-py3-none-any.whl"
    result = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "core_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "core_cli": True,
        "tui_cli": True,
        "core_health_loopback": healthy,
        "core_stopped": process.poll() is not None,
        **package,
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
