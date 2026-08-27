from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import operant.api as api_module
import operant.cli as cli
from operant.application.evaluation import EvaluationRunEvent
from operant.application.service import ApplicationService
from operant.domain.evaluation import (
    ArtifactWorkspace,
    EnvironmentSnapshot,
    EvaluationCase,
    EvaluationResult,
    EvaluationResultSnapshot,
    EvaluationResultStatus,
    EvaluationRoleSnapshot,
    EvaluationRun,
    EvaluationSuite,
    EvaluationSuiteStatus,
    EvaluationVariant,
    ExecutionSnapshot,
    MemorySnapshot,
    ModelSnapshot,
    PromptSnapshot,
    VerificationCommand,
    VerificationOutcome,
)
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider

runner = CliRunner()


def make_suite() -> EvaluationSuite:
    execution = ExecutionSnapshot(runner="docker", docker_image="python:3.13-slim")
    role = EvaluationRoleSnapshot(
        role_id="role_eval_api",
        role_version=1,
        role_name="Evaluation Coder",
        prompt=PromptSnapshot(id="prompt_eval_api", content="Repair only the evaluation fixture."),
        model=ModelSnapshot(
            model_profile_id="model_eval_api",
            provider="openai-compatible",
            model_id="eval-model-v1",
            effort="medium",
        ),
        tool_policy_fingerprint="a" * 64,
    )
    return EvaluationSuite(
        id="suite_eval_api",
        name="API and CLI evaluation suite",
        status=EvaluationSuiteStatus.READY,
        cases=(
            EvaluationCase(
                id="case_eval_api",
                name="repair fixture",
                task="Repair the isolated fixture.",
                environment=EnvironmentSnapshot(fixture_ref="fixtures/eval_api"),
                verification_commands=(VerificationCommand(argv=("pytest",)),),
                allowed_changed_paths=("calculator.py",),
                expected_changed_paths=("calculator.py",),
            ),
        ),
        variants=(
            EvaluationVariant(
                id="variant_eval_api",
                name="single coder",
                kind="session",
                session_role=role,
                memory=MemorySnapshot(enabled=False),
                execution=execution,
            ),
        ),
    )


def make_error_result(evaluation_run: EvaluationRun, suite: EvaluationSuite) -> EvaluationResult:
    case = suite.cases[0]
    variant = suite.variants[0]
    assert variant.session_role is not None
    return EvaluationResult(
        id="result_eval_api",
        run_id=evaluation_run.id,
        case_id=case.id,
        variant_id=variant.id,
        repetition=1,
        status=EvaluationResultStatus.ERROR,
        snapshot=EvaluationResultSnapshot(
            roles=(variant.session_role,),
            memory=variant.memory,
            environment=case.environment,
            execution=variant.execution,
        ),
        artifact_workspace=ArtifactWorkspace(
            local_workspace_path="/private/secret/evaluation-workspace",
            artifact_ref="evaluation/result_eval_api",
        ),
        verification=(
            VerificationOutcome(
                argv=case.verification_commands[0].argv,
                exit_code=1,
                timed_out=False,
                duration_ms=1,
            ),
        ),
    )


def make_service(tmp_path: Path) -> ApplicationService:
    service = ApplicationService(
        SQLiteStore(tmp_path / "evaluation-cli.sqlite3"),
        OpenAICompatibleProvider(),
    )
    service.initialize()
    return service


def test_evaluation_api_crud_sse_and_safe_results(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "evaluation-api.sqlite3"
    app = api_module.create_app(database)
    suite = make_suite()

    with TestClient(app) as client:
        created = client.post("/v1/evaluations/suites", json=suite.model_dump(mode="json"))
        assert created.status_code == 201
        assert created.json()["id"] == suite.id
        assert [item["id"] for item in client.get("/v1/evaluations/suites").json()] == [suite.id]
        assert client.get(f"/v1/evaluations/suites/{suite.id}").json()["name"] == suite.name

        store = SQLiteStore(database)
        evaluation_run = store.create_evaluation_run(
            EvaluationRun(id="run_eval_api", suite_id=suite.id)
        )
        store.append_evaluation_result(make_error_result(evaluation_run, suite))
        assert [item["id"] for item in client.get("/v1/evaluations/runs").json()] == [
            evaluation_run.id
        ]
        assert (
            client.get(f"/v1/evaluations/runs/{evaluation_run.id}").json()["id"]
            == evaluation_run.id
        )
        results = client.get(f"/v1/evaluations/runs/{evaluation_run.id}/results")
        assert results.status_code == 200
        assert (
            results.json()[0]["artifact_workspace"]["artifact_ref"] == "evaluation/result_eval_api"
        )
        assert "local_workspace_path" not in results.json()[0]["artifact_workspace"]
        assert "/private/secret" not in results.text

        calls: list[tuple[str, str]] = []

        class FakeRunner:
            def __init__(self, service: ApplicationService) -> None:
                assert service is not None

            async def run_suite(
                self,
                suite_id: str,
                *,
                artifact_root: str | Path,
            ) -> AsyncIterator[EvaluationRunEvent]:
                calls.append((suite_id, str(artifact_root)))
                yield EvaluationRunEvent(
                    evaluation_run_id="run_stream",
                    event_type="evaluation.run_started",
                    payload={"expected_results": 1},
                )

        monkeypatch.setattr(api_module, "EvaluationRunner", FakeRunner)
        artifact_root = tmp_path / "artifacts"
        streamed = client.post(
            "/v1/evaluations/runs",
            json={"suite_id": suite.id, "artifact_root": str(artifact_root)},
        )
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        assert "event: evaluation.run_started" in streamed.text
        assert calls == [(suite.id, str(artifact_root))]
        assert (
            client.post(
                "/v1/evaluations/runs",
                json={"suite_id": suite.id, "artifact_root": "relative/artifacts"},
            ).status_code
            == 422
        )

        class FailingRunner:
            def __init__(self, service: ApplicationService) -> None:
                assert service is not None

            async def run_suite(
                self,
                suite_id: str,
                *,
                artifact_root: str | Path,
            ) -> AsyncIterator[EvaluationRunEvent]:
                del suite_id, artifact_root
                if False:
                    yield EvaluationRunEvent(
                        evaluation_run_id="unused",
                        event_type="unused",
                    )
                raise RuntimeError("private provider failure text must not be streamed")

        monkeypatch.setattr(api_module, "EvaluationRunner", FailingRunner)
        failed_stream = client.post(
            "/v1/evaluations/runs",
            json={"suite_id": suite.id, "artifact_root": str(artifact_root)},
        )
        assert "event: evaluation.error" in failed_stream.text
        assert '"error_type": "runner_error"' in failed_stream.text
        assert "private provider failure text" not in failed_stream.text


def test_evaluation_cli_crud_stream_and_absolute_artifact_root(tmp_path: Path, monkeypatch) -> None:
    service = make_service(tmp_path)
    suite = make_suite()
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(json.dumps(suite.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setattr(cli, "_service", lambda: service)

    added = runner.invoke(cli.app, ["evaluation", "suite", "add", "--file", str(suite_file)])
    assert added.exit_code == 0
    assert json.loads(added.stdout)["id"] == suite.id
    listed = runner.invoke(cli.app, ["evaluation", "suite", "list"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)[0]["id"] == suite.id
    shown = runner.invoke(cli.app, ["evaluation", "suite", "show", suite.id])
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["id"] == suite.id

    evaluation_run = service.create_evaluation_run(
        EvaluationRun(id="run_eval_cli", suite_id=suite.id)
    )
    service.append_evaluation_result(make_error_result(evaluation_run, suite))
    runs = runner.invoke(cli.app, ["evaluation", "run", "list"])
    assert runs.exit_code == 0
    assert json.loads(runs.stdout)[0]["id"] == evaluation_run.id
    shown_run = runner.invoke(cli.app, ["evaluation", "run", "show", evaluation_run.id])
    assert shown_run.exit_code == 0
    assert json.loads(shown_run.stdout)["id"] == evaluation_run.id
    results = runner.invoke(cli.app, ["evaluation", "result", "list", evaluation_run.id])
    assert results.exit_code == 0
    assert "/private/secret" not in results.stdout
    assert "local_workspace_path" not in json.loads(results.stdout)[0]["artifact_workspace"]

    calls: list[tuple[str, Path]] = []

    class FakeRunner:
        def __init__(self, candidate_service: ApplicationService) -> None:
            assert candidate_service is service

        async def run_suite(
            self,
            suite_id: str,
            *,
            artifact_root: str | Path,
        ) -> AsyncIterator[EvaluationRunEvent]:
            calls.append((suite_id, Path(artifact_root)))
            yield EvaluationRunEvent(
                evaluation_run_id="run_stream_cli",
                event_type="evaluation.run_started",
                payload={"expected_results": 1},
            )

    monkeypatch.setattr(cli, "EvaluationRunner", FakeRunner)
    artifact_root = tmp_path / "artifacts"
    started = runner.invoke(
        cli.app,
        ["evaluation", "run", suite.id, "--artifact-root", str(artifact_root)],
    )
    assert started.exit_code == 0
    assert '"event_type": "evaluation.run_started"' in started.stdout
    assert calls == [(suite.id, artifact_root)]

    relative_root = runner.invoke(
        cli.app,
        ["evaluation", "run", suite.id, "--artifact-root", "relative/artifacts"],
    )
    assert relative_root.exit_code != 0
