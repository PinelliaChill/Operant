from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_TEST_FAILURE_SUMMARY_CHARS = 12_000
_TEST_MODULES = frozenset({"pytest", "unittest"})
_TEST_EXECUTABLES = frozenset({"pytest", "py.test", "unittest"})
_FAILURE_MARKERS = (
    "FAILED",
    "ERROR",
    "AssertionError",
    "Traceback",
    "short test summary info",
)


def is_test_command(argv: Sequence[str]) -> bool:
    executable_names = {Path(part).name for part in argv}
    if executable_names.intersection(_TEST_EXECUTABLES):
        return True
    return any(
        argument == "-m" and index + 1 < len(argv) and argv[index + 1] in _TEST_MODULES
        for index, argument in enumerate(argv)
    )


def test_failure_feedback(result: Mapping[str, Any]) -> dict[str, Any] | None:
    argv = result.get("argv")
    exit_code = result.get("exit_code")
    if (
        not isinstance(argv, list)
        or not all(isinstance(part, str) for part in argv)
        or not isinstance(exit_code, int)
        or exit_code == 0
        or not is_test_command(argv)
    ):
        return None

    stdout = str(result.get("stdout", ""))
    stderr = str(result.get("stderr", ""))
    summary = _failure_summary(stdout, stderr)
    normalized = _normalize_for_signature(summary)
    signature_input = "\0".join([*argv, str(exit_code), normalized])
    signature = hashlib.sha256(signature_input.encode("utf-8")).hexdigest()[:16]
    return {
        "command": argv,
        "exit_code": exit_code,
        "summary": summary,
        "signature": signature,
        "stdout_truncated": bool(result.get("stdout_truncated", False)),
        "stderr_truncated": bool(result.get("stderr_truncated", False)),
    }


def _failure_summary(stdout: str, stderr: str) -> str:
    lines = [*stdout.splitlines(), *stderr.splitlines()]
    selected = [
        line[:500]
        for line in lines
        if line.startswith("E ") or any(marker in line for marker in _FAILURE_MARKERS)
    ]
    if not selected:
        selected = [line[:500] for line in lines[-40:]]
    summary = "\n".join(selected)
    return summary[-MAX_TEST_FAILURE_SUMMARY_CHARS:]


def _normalize_for_signature(summary: str) -> str:
    without_hex = re.sub(r"0x[0-9a-fA-F]+", "<hex>", summary)
    without_tmp_paths = re.sub(r"/private/tmp/[^\s:]+", "<tmp>", without_hex)
    return re.sub(r"\b\d+\b", "<number>", without_tmp_paths)


@dataclass
class NoProgressDetector:
    max_consecutive_test_failures: int
    _last_signature: str | None = None
    _count: int = 0

    def observe(self, feedback: Mapping[str, Any]) -> bool:
        signature = feedback.get("signature")
        if not isinstance(signature, str):
            return False
        if signature == self._last_signature:
            self._count += 1
        else:
            self._last_signature = signature
            self._count = 1
        return self._count >= self.max_consecutive_test_failures

    @property
    def count(self) -> int:
        return self._count
