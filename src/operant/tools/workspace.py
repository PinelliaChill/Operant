from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from operant.domain.messages import ToolDefinition
from operant.domain.models import ToolPolicy


class ToolError(RuntimeError):
    pass


class ApprovalRequired(ToolError):
    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"approval required for {category}: {detail}")


ApprovalCallback = Callable[[str, str, str], Awaitable[bool]]


class WorkspaceTools:
    def __init__(
        self,
        root: str | Path,
        *,
        policy: ToolPolicy | None = None,
        output_limit: int = 100_000,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = policy or ToolPolicy()
        self.output_limit = output_limit

    def definitions(self) -> tuple[ToolDefinition, ...]:
        definitions = (
            ToolDefinition(
                name="read_file",
                description="Read a UTF-8 text file inside the workspace.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="search_files",
                description="Search text files in the workspace for a literal string.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "default": "."},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="apply_patch",
                description=(
                    "Replace one exact text occurrence in a workspace file. "
                    "Use empty old_text only when creating a new file."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="git_diff",
                description=(
                    "Read the current workspace Git diff without changing repository state."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "staged": {"type": "boolean", "default": False},
                    },
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="run_command",
                description="Run a command in the workspace without invoking a shell.",
                parameters={
                    "type": "object",
                    "properties": {
                        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "cwd": {"type": "string", "default": "."},
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 300,
                            "default": 60,
                        },
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
            ),
        )
        return tuple(
            definition for definition in definitions if definition.name in self.policy.allowed_tools
        )

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        approved_categories: frozenset[str] = frozenset(),
    ) -> str:
        if name not in self.policy.allowed_tools:
            raise ToolError(f"tool is not allowed by this role: {name}")
        if name == "read_file":
            result: Any = self.read_file(str(arguments["path"]))
        elif name == "search_files":
            result = self.search_files(str(arguments["query"]), str(arguments.get("path", ".")))
        elif name == "apply_patch":
            if not self.policy.workspace_write:
                raise ToolError("workspace writes are not allowed by this role")
            result = self.apply_patch(
                str(arguments["path"]),
                str(arguments["old_text"]),
                str(arguments["new_text"]),
            )
        elif name == "git_diff":
            result = await self.git_diff(staged=bool(arguments.get("staged", False)))
        elif name == "run_command":
            if not self.policy.command_execution:
                raise ToolError("command execution is not allowed by this role")
            raw_argv = arguments["argv"]
            if not isinstance(raw_argv, list) or not all(
                isinstance(item, str) for item in raw_argv
            ):
                raise ToolError("argv must be a list of strings")
            result = await self.run_command(
                raw_argv,
                cwd=str(arguments.get("cwd", ".")),
                timeout_seconds=int(arguments.get("timeout_seconds", 60)),
                approved_categories=approved_categories,
            )
        else:
            raise ToolError(f"unknown tool: {name}")
        return json.dumps(result, ensure_ascii=False)

    async def git_diff(self, *, staged: bool = False) -> dict[str, Any]:
        argv = ["git", "diff", "--no-ext-diff"]
        if staged:
            argv.append("--cached")
        argv.append("--")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return {
            "staged": staged,
            "exit_code": process.returncode,
            "diff": stdout.decode("utf-8", errors="replace")[: self.output_limit],
            "stderr": stderr.decode("utf-8", errors="replace")[: self.output_limit],
        }

    def read_file(self, path: str) -> dict[str, Any]:
        target = self._resolve(path)
        content = target.read_text(encoding="utf-8")
        if len(content) > self.output_limit:
            content = content[: self.output_limit]
            truncated = True
        else:
            truncated = False
        return {
            "path": str(target.relative_to(self.root)),
            "content": content,
            "truncated": truncated,
        }

    def search_files(self, query: str, path: str = ".") -> dict[str, Any]:
        if not query:
            raise ToolError("query must not be empty")
        start = self._resolve(path, allow_directory=True)
        matches: list[dict[str, Any]] = []
        candidates = start.rglob("*") if start.is_dir() else (start,)
        for candidate in candidates:
            if not candidate.is_file() or self._is_protected(candidate):
                continue
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(
                        {
                            "path": str(candidate.relative_to(self.root)),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= 200:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def apply_patch(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        target = self._resolve(path, may_not_exist=True)
        if old_text == "":
            if target.exists():
                raise ToolError("refusing to overwrite an existing file with an empty old_text")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_text, encoding="utf-8")
            return {"path": str(target.relative_to(self.root)), "created": True}

        if not target.is_file():
            raise ToolError(f"file does not exist: {path}")
        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ToolError(f"old_text must occur exactly once; found {occurrences}")
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return {"path": str(target.relative_to(self.root)), "created": False}

    async def run_command(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        timeout_seconds: int = 60,
        approved_categories: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        if not argv or any("\x00" in part for part in argv):
            raise ToolError("invalid command arguments")
        category = self._approval_category(argv)
        if (
            category is not None
            and category in self.policy.approval_required
            and category not in approved_categories
        ):
            raise ApprovalRequired(category, " ".join(argv))

        command_cwd = self._resolve(cwd, allow_directory=True)
        if not command_cwd.is_dir():
            raise ToolError(f"command cwd is not a directory: {cwd}")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=command_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ToolError(f"command timed out after {timeout_seconds}s") from exc
        return {
            "argv": list(argv),
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[: self.output_limit],
            "stderr": stderr.decode("utf-8", errors="replace")[: self.output_limit],
        }

    def _resolve(
        self,
        path: str,
        *,
        may_not_exist: bool = False,
        allow_directory: bool = False,
    ) -> Path:
        if "\x00" in path:
            raise ToolError("invalid path")
        candidate = (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError("path escapes the workspace")
        if self._is_protected(candidate):
            raise ToolError("path is protected")
        if not may_not_exist and not candidate.exists():
            raise ToolError(f"path does not exist: {path}")
        if candidate.is_dir() and not allow_directory:
            raise ToolError(f"path is a directory: {path}")
        return candidate

    def _is_protected(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True
        protected_names = {".git", ".env", ".env.local", "secrets.json"}
        return any(part in protected_names for part in relative.parts)

    @staticmethod
    def _approval_category(argv: Sequence[str]) -> str | None:
        executable = Path(argv[0]).name
        if executable in {"sudo", "doas"}:
            return "privileged"
        if executable in {"rm", "rmdir", "shred"}:
            return "destructive"
        if executable in {"curl", "wget", "ssh", "scp", "nc"}:
            return "network"
        if (
            executable == "git"
            and len(argv) > 1
            and argv[1]
            not in {
                "status",
                "diff",
                "log",
                "show",
            }
        ):
            return "git_write"
        return None
