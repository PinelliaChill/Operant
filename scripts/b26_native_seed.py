"""Prepare one synthetic draft via public Core APIs for native GUI acceptance."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runroot = args.run_root.resolve()
    runroot.mkdir(parents=True, exist_ok=False)
    workspace = runroot / "workspace"
    workspace.mkdir()
    db = runroot / "core.sqlite3"
    os.environ["OPERANT_DB_PATH"] = str(db)
    from fastapi.testclient import TestClient

    from operant.api import create_app

    with TestClient(create_app(db)) as client:
        serial = 0

        def command(version="b2-6", **body):
            nonlocal serial
            serial += 1
            result = client.post(
                f"/v1/{version}/commands",
                json=body,
                headers={"Idempotency-Key": f"native-seed-{serial}"},
            )
            assert result.status_code == 200, f"seed_{body['action']}_{result.status_code}"
            return result.json()

        project = command(
            "b2-3", action="project_create", name="B26 原生联合验收", workspace_path=str(workspace)
        )["state"]["projects"][-1]["project_id"]
        installation = command(
            "b2-3", action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
        )["state"]["installations"][-1]["installation_id"]
        command("b2-3", action="binding_select", project_id=project, installation_id=installation)
        command(
            "b2-3",
            action="memory_save",
            project_id=project,
            content="合成检查步骤：先核对工作区，再运行允许的测试。",
            confirmed=True,
        )
        record = client.get(f"/v1/b2-5/projects/{project}/governance").json()["records"][0][
            "version"
        ]
        state = client.get(f"/v1/b2-6/projects/{project}/experience").json()
        proposed = command(
            action="procedure_propose",
            project_id=project,
            content="1. 核对工作区。\n2. 运行已允许的测试。",
            sources=[
                dict(
                    source_type="memory_version",
                    source_id=record["ref"]["record_id"],
                    revision=record["ref"]["version"],
                    content_digest=record["ref"]["content_digest"],
                    scope=record["scope"],
                    permission_epoch=state["remote"]["permission_epoch"],
                    availability="available",
                )
            ],
        )
        entry = next(
            e
            for e in client.get(f"/v1/b2-5/projects/{project}/governance").json()["proposals"]
            if e["proposal"]["proposal_id"] in proposed["affected_ids"]
        )
        proposal = entry["proposal"]
        command(
            "b2-5",
            action="review",
            project_id=project,
            decision="accept",
            selections=[
                dict(
                    proposal_id=proposal["proposal_id"],
                    proposal_revision=proposal["proposal_revision"],
                    proposed_version=proposal["proposed_version"],
                    base_head_revision=proposal["base_head"]["revision"],
                )
            ],
        )
        draft = command(
            action="skill_draft",
            project_id=project,
            procedure_ref=entry["version"]["ref"],
            name="native-check",
            description="合成原生验收检查步骤",
        )["state"]["skills"]["skills"][0]
    result = {
        "database": str(db),
        "workspace": str(workspace),
        "project_id": project,
        "skill_id": draft["skill_id"],
        "initial_head": draft["head"],
        "kind": "synthetic_public_api_seed",
        "model_called": False,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
