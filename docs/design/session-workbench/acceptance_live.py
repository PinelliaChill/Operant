"""Isolated, credential-safe preparation for the session workbench live check.

Run with this worktree's ``src`` on PYTHONPATH. The operator supplies an absolute
scratch directory under the system temporary directory and an existing .env path.
The .env is read only inside this process; no secret value is printed or copied.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from operant.application.service import ApplicationService
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider, ProviderError
from operant.settings import load_local_env

MARKER = "operant-session-workbench-acceptance-v1"
SECRET_REF = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
MODEL_ID = "gpt-6-luna"
FIXTURE_TEXT = "验收夹具：甲组检查 2+3，乙组检查 7-4。只需分别报告结果和来源。\n"
AUDIT_TEXT = "验收审计表：逐行核对右侧算式，报告错误行号。\n" + "".join(
    f"{index:02d}. {index}+{index % 7}={index + index % 7 + (1 if index in {23, 61} else 0)}\n"
    for index in range(1, 81)
)


def scratch_path(raw: str) -> Path:
    supplied = Path(raw).expanduser()
    if not supplied.is_absolute():
        raise ValueError("scratch path must be absolute")
    scratch = supplied.resolve(strict=False)
    temporary = Path(tempfile.gettempdir()).resolve()
    if scratch == temporary or temporary not in scratch.parents:
        raise ValueError("scratch path must be below the system temporary directory")
    return scratch


def prepared_scratch(raw: str) -> Path:
    scratch = scratch_path(raw)
    marker = scratch / "acceptance.marker"
    if not marker.is_file() or marker.read_text(encoding="utf-8") != MARKER + "\n":
        raise ValueError("scratch directory has not been prepared by this script")
    return scratch


def prepare(raw: str) -> None:
    scratch = scratch_path(raw)
    if scratch.exists():
        if not scratch.is_dir() or any(scratch.iterdir()):
            prepared_scratch(raw)
    else:
        scratch.mkdir(parents=True, mode=0o700)
    marker = scratch / "acceptance.marker"
    marker.write_text(MARKER + "\n", encoding="utf-8")
    workspace = scratch / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    fixture = workspace / "brief.txt"
    if not fixture.exists():
        fixture.write_text(FIXTURE_TEXT, encoding="utf-8")
    audit = workspace / "audit.txt"
    if not audit.exists():
        audit.write_text(AUDIT_TEXT, encoding="utf-8")
    print(
        json.dumps(
            {
                "scratch": str(scratch),
                "database": str(scratch / "core.sqlite3"),
                "workspace": str(workspace),
            },
            ensure_ascii=False,
        )
    )


async def discover(raw: str, env_file: str, secret_ref: str) -> None:
    scratch = prepared_scratch(raw)
    source = Path(env_file).expanduser()
    if not source.is_absolute() or not source.is_file():
        raise ValueError("env-file must be an existing absolute file")
    if SECRET_REF.fullmatch(secret_ref) is None:
        raise ValueError("secret-ref must be an environment variable name")
    os.environ["OPERANT_DB_PATH"] = str(scratch / "core.sqlite3")
    load_local_env(source)
    base_url = os.environ.get("OPERANT_BASE_URL")
    if not base_url:
        raise ValueError("OPERANT_BASE_URL is unavailable")
    service = ApplicationService(SQLiteStore(scratch / "core.sqlite3"), OpenAICompatibleProvider())
    service.initialize()
    model_ids = await service.discover_models(base_url=base_url, secret_ref=secret_ref)
    print(json.dumps({"model_ids": model_ids}, ensure_ascii=False))


def acceptance_environment(raw: str, env_file: str, secret_ref: str) -> tuple[Path, str]:
    scratch = prepared_scratch(raw)
    source = Path(env_file).expanduser()
    if not source.is_absolute() or not source.is_file():
        raise ValueError("env-file must be an existing absolute file")
    if SECRET_REF.fullmatch(secret_ref) is None:
        raise ValueError("secret-ref must be an environment variable name")
    os.environ["OPERANT_DB_PATH"] = str(scratch / "core.sqlite3")
    load_local_env(source)
    if not os.environ.get(secret_ref):
        raise ValueError("the named credential is unavailable")
    base_url = os.environ.get("OPERANT_BASE_URL")
    if not base_url:
        raise ValueError("the model endpoint is unavailable")
    return scratch, base_url


async def seed(raw: str, env_file: str, secret_ref: str) -> None:
    scratch, base_url = acceptance_environment(raw, env_file, secret_ref)
    workspace = scratch / "workspace"
    if (
        not workspace.is_dir()
        or not (workspace / "brief.txt").is_file()
        or not (workspace / "audit.txt").is_file()
        or (workspace / "brief.txt").read_text() != FIXTURE_TEXT
        or (workspace / "audit.txt").read_text() != AUDIT_TEXT
    ):
        raise ValueError("isolated workspace fixture changed; prepare a fresh scratch directory")
    service = ApplicationService(
        SQLiteStore(scratch / "core.sqlite3"),
        OpenAICompatibleProvider(),
        artifact_root=scratch / "artifacts",
    )
    service.initialize()
    manifest = scratch / "seed.json"
    if manifest.exists():
        result = json.loads(manifest.read_text(encoding="utf-8"))
        profile = service.get_model_profile(result["model_profile_id"])
        service.get_role(result["parent_role_id"])
        service.get_role(result["child_role_id"])
        if profile.model_id != MODEL_ID or profile.base_url != base_url:
            raise ValueError("existing acceptance seed uses another model connection")
        print(json.dumps(result, ensure_ascii=False))
        return
    if service.list_model_profiles() or service.list_roles():
        raise ValueError("acceptance registry is partly seeded; reconcile before retrying")
    discovered = await service.discover_models(base_url=base_url, secret_ref=secret_ref)
    if MODEL_ID not in discovered:
        raise ValueError("required exact model ID was not discovered")
    profile = service.add_model_profile(
        ModelProfile(
            name="acceptance-gpt-6-luna",
            model_id=MODEL_ID,
            base_url=base_url,
            secret_ref=secret_ref,
            supported_efforts=(Effort.LOW,),
            default_effort=Effort.LOW,
        )
    )
    parent_role = service.create_role(
        RolePreset(
            name="acceptance-parent-readonly",
            system_prompt=(
                "你是受限验收主 Agent。只在当前绝对工作区读文件；任务需要分工时，"
                "用 delegate_agent 创建明确职责的子 Agent，用 send_agent_message 定向沟通，"
                "用 wait_for_agent 等待并报告可核对结果。不要执行命令、写文件或猜测工具结果。"
            ),
            model_profile_id=profile.id,
            effort=Effort.LOW,
            tool_policy=ToolPolicy(
                allowed_tools=(
                    "read_file",
                    "delegate_agent",
                    "send_agent_message",
                    "wait_for_agent",
                ),
                workspace_write=False,
                command_execution=False,
            ),
            budget=Budget(
                max_turns=12, timeout_seconds=120, max_output_tokens=512, max_tool_calls=12
            ),
        )
    )
    child_role = service.create_role(
        RolePreset(
            name="acceptance-child-readonly",
            system_prompt=(
                "你是受限验收子 Agent。仅处理被分派的一项任务；"
                "如需来源，读取当前工作区的 brief.txt。"
                "只向明确收件人发送必要消息，简短报告计算和文件依据。不要执行命令或写文件。"
            ),
            model_profile_id=profile.id,
            effort=Effort.LOW,
            tool_policy=ToolPolicy(
                allowed_tools=("read_file", "send_agent_message"),
                workspace_write=False,
                command_execution=False,
            ),
            budget=Budget(max_turns=6, timeout_seconds=60, max_output_tokens=256, max_tool_calls=4),
        )
    )
    initialization, _ = service.initialize_workspace(workspace.resolve(strict=True))
    result = {
        "model_id": MODEL_ID,
        "model_profile_id": profile.id,
        "parent_role_id": parent_role.id,
        "child_role_id": child_role.id,
        "workspace_id": initialization.id,
        "workspace": str(workspace.resolve(strict=True)),
        "effective_reference_tool": (
            "read_context_reference when read_file and explicit reference are present"
        ),
    }
    with manifest.open("x", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(result, ensure_ascii=False))


def serve(raw: str, env_file: str, secret_ref: str, port: int) -> None:
    if port < 1024 or port > 65535:
        raise ValueError("port must be between 1024 and 65535")
    scratch, _base_url = acceptance_environment(raw, env_file, secret_ref)
    if not (scratch / "seed.json").is_file():
        raise ValueError("run seed before starting the acceptance Core")
    import uvicorn

    from operant.api import create_app

    app = create_app(
        scratch / "core.sqlite3",
        artifact_root=scratch / "artifacts",
        phase45_skill_roots={},
        phase45_mcp_workspace_roots={},
        phase56_multiwriter_roots={},
    )
    print(
        json.dumps(
            {"core_url": f"http://127.0.0.1:{port}", "database": str(scratch / "core.sqlite3")},
            ensure_ascii=False,
        ),
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")


async def snapshot(core_url: str, thread_ids: list[str], session_ids: list[str]) -> None:
    parsed = urlsplit(core_url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("core-url must be a plain HTTP loopback origin")

    async with httpx.AsyncClient(base_url=core_url.rstrip("/"), timeout=10) as client:

        async def read(path: str) -> tuple[int, object | None]:
            response = await client.get(path)
            if response.status_code != 200:
                return response.status_code, None
            return response.status_code, response.json()

        health, _ = await read("/healthz")
        report: dict[str, object] = {"health_http_status": health, "threads": [], "sessions": []}
        threads = report["threads"]
        sessions = report["sessions"]
        assert isinstance(threads, list) and isinstance(sessions, list)
        for thread_id in thread_ids:
            encoded = quote(thread_id, safe="")
            code, body = await read(f"/v1/threads/{encoded}")
            items_code, items = await read(f"/v1/threads/{encoded}/items?limit=1000")
            children_code, children = await read(f"/v1/workbench/threads/{encoded}/children")
            messages_code, messages = await read(f"/v1/workbench/threads/{encoded}/messages")
            item_list = items if isinstance(items, list) else []
            child_list = children if isinstance(children, list) else []
            message_list = messages if isinstance(messages, list) else []
            threads.append(
                {
                    "id": thread_id,
                    "http_status": code,
                    "parent_thread_id": body.get("parent_thread_id")
                    if isinstance(body, dict)
                    else None,
                    "status": body.get("status") if isinstance(body, dict) else None,
                    "items_http_status": items_code,
                    "item_types": [
                        item["payload"].get("type")
                        for item in item_list
                        if isinstance(item, dict) and isinstance(item.get("payload"), dict)
                    ],
                    "children_http_status": children_code,
                    "children": [
                        {
                            "thread_id": child.get("thread_id"),
                            "status": child.get("status"),
                            "recovery": child.get("recovery"),
                        }
                        for child in child_list
                        if isinstance(child, dict)
                    ],
                    "messages_http_status": messages_code,
                    "directed_messages": [
                        {
                            "message_id": message.get("message_id"),
                            "sender_thread_id": message.get("sender_thread_id"),
                            "recipient_thread_id": message.get("recipient_thread_id"),
                            "reply_to": message.get("reply_to"),
                            "cursor": message.get("cursor"),
                        }
                        for message in message_list
                        if isinstance(message, dict)
                    ],
                }
            )
        for session_id in session_ids:
            encoded = quote(session_id, safe="")
            code, body = await read(f"/v1/sessions/{encoded}")
            events_code, events = await read(f"/v1/sessions/{encoded}/events?limit=1000")
            event_list = events if isinstance(events, list) else []
            role_snapshot = body.get("role_snapshot") if isinstance(body, dict) else None
            sessions.append(
                {
                    "id": session_id,
                    "http_status": code,
                    "status": body.get("status") if isinstance(body, dict) else None,
                    "events_http_status": events_code,
                    "event_types": [
                        event.get("event_type") for event in event_list if isinstance(event, dict)
                    ],
                    "model_id": (
                        role_snapshot.get("model_id") if isinstance(role_snapshot, dict) else None
                    ),
                    "tool_policy": (
                        role_snapshot.get("tool_policy")
                        if isinstance(role_snapshot, dict)
                        else None
                    ),
                    "budget": (
                        role_snapshot.get("budget") if isinstance(role_snapshot, dict) else None
                    ),
                }
            )
    print(json.dumps(report, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "discover", "seed", "serve", "snapshot"))
    parser.add_argument("--scratch")
    parser.add_argument("--env-file")
    parser.add_argument("--secret-ref", default="OPERANT_API_KEY")
    parser.add_argument("--core-url", default="http://127.0.0.1:8000")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--thread-id", action="append", default=[])
    parser.add_argument("--session-id", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            if args.scratch is None:
                raise ValueError("prepare requires --scratch")
            prepare(args.scratch)
        elif args.action == "discover":
            if args.scratch is None or args.env_file is None:
                raise ValueError("discover requires --scratch and --env-file")
            asyncio.run(discover(args.scratch, args.env_file, args.secret_ref))
        elif args.action == "seed":
            if args.scratch is None or args.env_file is None:
                raise ValueError("seed requires --scratch and --env-file")
            asyncio.run(seed(args.scratch, args.env_file, args.secret_ref))
        elif args.action == "serve":
            if args.scratch is None or args.env_file is None:
                raise ValueError("serve requires --scratch and --env-file")
            serve(args.scratch, args.env_file, args.secret_ref, args.port)
        else:
            asyncio.run(snapshot(args.core_url, args.thread_id, args.session_id))
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        parser.exit(2, f"acceptance preflight failed: {type(exc).__name__}\n")
    except (ValueError, OSError, ProviderError) as exc:
        # ProviderError is sanitized by the production provider. Never print
        # HTTP headers, the endpoint URL, or the environment file contents.
        message = str(exc) if type(exc) is ValueError else type(exc).__name__
        parser.exit(2, f"acceptance preflight failed: {message}\n")


if __name__ == "__main__":
    main()
