import os
from pathlib import Path

import pytest

from operant.settings import load_local_env


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
