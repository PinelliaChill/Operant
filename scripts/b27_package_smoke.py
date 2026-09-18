"""Exercise both bundled plugins using an installed wheel outside the source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

CHILD = r"""
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
os.environ['OPERANT_DB_PATH'] = str(root / 'core.sqlite3')
import operant
from operant.api import create_app
from fastapi.testclient import TestClient
app = create_app(root / 'core.sqlite3')
checks = []
with TestClient(app) as client:
    serial = 0
    def command(**body):
        global serial
        serial += 1
        response = client.post('/v1/b2-3/commands', json=body,
            headers={'Idempotency-Key': f'package-{serial}'})
        assert response.status_code == 200, (body['action'], response.status_code)
        return response.json()
    assert client.get('/v1/b2-3/management').status_code == 200
    manager = app.state.operant_service.memory_manager
    assert 'site-packages' in str(manager.catalog_root), str(manager.catalog_root)
    for plugin in ('memory-standard', 'memory-notebook'):
        workspace = root / plugin
        workspace.mkdir()
        project = command(action='project_create', name=plugin,
            workspace_path=str(workspace))['state']['projects'][-1]['project_id']
        installed = command(action='plugin_install', plugin_id=plugin,
            mode='trusted_in_process')['state']['installations']
        installation = next(i for i in installed if i['plugin_id'] == plugin)['installation_id']
        command(action='binding_select', project_id=project, installation_id=installation)
        command(action='memory_save', project_id=project,
            content='B27_PACKAGE_CHECK key=MAPLE_27', confirmed=True)
        found = command(action='memory_search', project_id=project, query='B27_PACKAGE_CHECK')
        assert 'MAPLE_27' in json.dumps(found), plugin
        command(action='plugin_uninstall', installation_id=installation, data_policy='keep')
        checks.append({'plugin': plugin, 'installed': True, 'saved_and_recalled': True,
            'keep_uninstalled': True})
    versions = ('', 'phase23', 'phase45', 'phase56', 'beta', 'b2', 'b2-3', 'b2-4', 'b2-5', 'b2-6')
    for version in versions:
        path = '/v1/protocol' + ('/' + version if version else '')
        assert client.get(path).status_code == 200, version
    print(json.dumps({'completed': True, 'module': operant.__file__,
        'catalog_root': str(manager.catalog_root), 'checks': checks,
        'protocols': list(versions)}))
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.run_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    process = subprocess.run(
        [str(args.python.absolute()), "-I", "-c", CHILD, str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    result = {
        "entry": "installed wheel / isolated Python -I / public Core commands",
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "exit_code": process.returncode,
        "database": str(root / "core.sqlite3"),
        "model_called": False,
        "completed": False,
    }
    if process.returncode == 0:
        result.update(json.loads(process.stdout.splitlines()[-1]))
    else:
        # The child uses only synthetic input and never loads credentials.
        result["failure"] = process.stderr[-4000:]
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    if not result["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
