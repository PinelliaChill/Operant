from pathlib import Path

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.domain.workflow import WorkflowRun, WorkflowRunStatus
from operant.persistence.sqlite import SQLiteStore


def test_web_workbench_routes_and_local_assets(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "web.sqlite3")) as client:
        page = client.get("/web")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "新建 Session" in page.text
        assert "Model Profile" in page.text
        assert "Role Preset" in page.text
        assert "coding Workflow" in page.text
        assert "Main / Planner / Explorer / Coder / Reviewer" in page.text
        assert "已有任务与恢复" in page.text
        assert "任务级 Trace" in page.text
        assert "恢复任务" in page.text
        assert "取消任务" in page.text
        assert "/web/static/app.js" in page.text
        assert "https://cdn." not in page.text
        assert client.get("/web/").status_code == 200

        script = client.get("/web/static/app.js")
        assert script.status_code == 200
        assert "textContent" in script.text
        assert "createElement" in script.text
        assert "innerHTML" not in script.text
        assert client.get("/web/static/styles.css").status_code == 200


def test_web_workbench_uses_existing_session_and_workflow_api_contracts() -> None:
    page = Path("src/operant/web/index.html").read_text(encoding="utf-8")
    script = Path("src/operant/web/static/app.js").read_text(encoding="utf-8")

    assert "/v1/sessions" in script
    assert "/v1/workflows/coding/runs" in script
    assert "/v1/tasks" in script
    assert "/events" in script
    assert "/trace" in script
    assert "/resume" in script
    assert "/cancel" in script
    assert "/approvals/" in script
    assert "explorer_role_ids" in script
    assert "main_role_id" in script
    assert "审批请求" in page
    assert "工具调用" in page
    assert "测试" in page
    assert "diff / 结果" in page


def test_web_task_controls_use_persisted_task_endpoints(tmp_path: Path) -> None:
    database = tmp_path / "web-tasks.sqlite3"
    app = create_app(database)
    store = SQLiteStore(database)
    task = store.create_workflow_run(
        WorkflowRun(
            task="Inspect the project",
            workspace=str(tmp_path),
            planner_role_id="role_planner",
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
            status=WorkflowRunStatus.INTERRUPTED,
        )
    )

    with TestClient(app) as client:
        listed = client.get("/v1/tasks")
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == task.id
        assert client.get(f"/v1/tasks/{task.id}/events").status_code == 200
        trace = client.get(f"/v1/tasks/{task.id}/trace")
        assert trace.status_code == 200
        assert trace.json()["workflow_run_id"] == task.id
        assert client.post(f"/v1/tasks/{task.id}/cancel").json() == {"accepted": True}
