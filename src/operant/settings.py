from __future__ import annotations

import ast
import os
import re
from pathlib import Path

_ENV_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def load_local_env(path: str | Path = ".env") -> tuple[str, ...]:
    """Load a small dotenv file without executing shell syntax.

    Existing process environment values win. Only variable names are returned;
    callers must not log credential values.
    """

    env_path = Path(path)
    if not env_path.is_file():
        return ()

    loaded: list[str] = []
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ValueError(f"invalid .env assignment at line {line_number}")

        name, raw_value = line.split("=", 1)
        name = name.strip()
        raw_value = raw_value.strip()
        if _ENV_NAME.fullmatch(name) is None:
            raise ValueError(f"invalid .env variable name at line {line_number}")

        value = _parse_env_value(raw_value, line_number)
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"invalid .env value at line {line_number}")
        if name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return tuple(loaded)


def _parse_env_value(raw_value: str, line_number: int) -> str:
    if not raw_value:
        return ""
    if raw_value[0] in {'"', "'"}:
        try:
            value = ast.literal_eval(raw_value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"invalid quoted .env value at line {line_number}") from exc
        if not isinstance(value, str):
            raise ValueError(f".env value must be a string at line {line_number}")
        return value
    return raw_value.split(" #", 1)[0].rstrip()


def database_path() -> Path:
    return Path(os.environ.get("OPERANT_DB_PATH", ".operant/operant.sqlite3"))
