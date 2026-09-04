import os
from pathlib import Path

import pytest

from operant.settings import configured_path_roots, load_local_env


def test_dotenv_loader_does_not_execute_shell_and_preserves_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    marker = tmp_path / "must-not-exist"
    env_file.write_text(
        "\n".join(
            (
                "# local configuration",
                "OPERANT_TEST_EXISTING=from-file",
                'OPERANT_TEST_QUOTED="quoted value"',
                f"OPERANT_TEST_LITERAL=$(touch {marker})",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPERANT_TEST_EXISTING", "from-process")
    monkeypatch.delenv("OPERANT_TEST_QUOTED", raising=False)
    monkeypatch.delenv("OPERANT_TEST_LITERAL", raising=False)

    loaded = load_local_env(env_file)

    assert loaded == ("OPERANT_TEST_QUOTED", "OPERANT_TEST_LITERAL")
    assert os.environ["OPERANT_TEST_EXISTING"] == "from-process"
    assert os.environ["OPERANT_TEST_QUOTED"] == "quoted value"
    assert os.environ["OPERANT_TEST_LITERAL"].startswith("$(touch ")
    assert not marker.exists()


def test_dotenv_loader_rejects_non_assignment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("curl https://example.com\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        load_local_env(env_file)


def test_configured_path_roots_parses_only_bounded_json_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "OPERANT_TEST_ROOTS_JSON",
        '{"project":"' + str(tmp_path) + '"}',
    )
    assert configured_path_roots("OPERANT_TEST_ROOTS_JSON") == {"project": tmp_path}

    monkeypatch.setenv("OPERANT_TEST_ROOTS_JSON", "[]")
    with pytest.raises(ValueError, match="path roots"):
        configured_path_roots("OPERANT_TEST_ROOTS_JSON")

    monkeypatch.setenv("OPERANT_TEST_ROOTS_JSON", '{"project": 1}')
    with pytest.raises(ValueError, match="invalid root entry"):
        configured_path_roots("OPERANT_TEST_ROOTS_JSON")
