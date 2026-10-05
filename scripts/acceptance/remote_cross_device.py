#!/usr/bin/env python3
"""Run one H-17 device acceptance step on the second physical machine.

The official ``operant remote-device`` CLI performs all protocol operations.
This helper prepares private argument files and prints bounded evidence only.
It never starts the Core, Target, SSH tunnel, or any background process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _private(path: Path, label: str) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{label} must have 0600 permissions")


def _call(binary: str, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        [binary, "remote-device", *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=330,
        env=os.environ.copy(),
    )
    if result.returncode:
        # CLI stderr may contain sensitive inputs; only the exit code is shown.
        raise RuntimeError(f"remote-device exited with code {result.returncode}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("remote-device returned no JSON object")
    return value


def _command(
    binary: str,
    state_file: Path,
    core_origin: str,
    operation: str,
    target_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if state_file.parent.stat().st_mode & 0o077:
        raise ValueError("device state directory must have 0700 permissions")
    descriptor, name = tempfile.mkstemp(
        prefix=".remote-args-", suffix=".json", dir=state_file.parent
    )
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(arguments, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        result = _call(
            binary,
            "command",
            "--state-file",
            str(state_file),
            "--core-origin",
            core_origin,
            "--operation",
            operation,
            "--target-id",
            target_id,
            "--arguments-file",
            str(path),
        )
    finally:
        path.unlink(missing_ok=True)
    receipt = result.get("receipt")
    if result.get("type") != "host_ack" or not isinstance(receipt, dict):
        raise AssertionError("official CLI returned no durable Host Ack")
    if not isinstance(receipt.get("command_id"), str) or not receipt.get("host_acknowledged_at"):
        raise AssertionError("Host Ack lacks command ID or acknowledgement time")
    return {
        "command_id": receipt["command_id"],
        "status": receipt.get("status"),
        "action_hash": receipt.get("action_hash"),
        "result_ref": receipt.get("result_ref"),
        "error_code": receipt.get("error_code"),
        "host_ack": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="operant", help="installed official Operant CLI")
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--core-origin", default="https://127.0.0.1:18807")
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("init")
    pair = sub.add_parser("pair")
    pair.add_argument("--ticket-file", type=Path, required=True)
    pair.add_argument("--display-name", required=True)
    bind = sub.add_parser("bind")
    bind.add_argument("--remote-session-id", required=True)
    create = sub.add_parser("create")
    create.add_argument("--role-id", required=True)
    create.add_argument("--project-id", required=True)
    create.add_argument("--model-profile-id", required=True)
    run = sub.add_parser("run")
    run.add_argument("--session-id", required=True)
    run.add_argument("--message-file", type=Path, required=True)
    status = sub.add_parser("status")
    status.add_argument("--session-id", required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--session-id", required=True)
    query = sub.add_parser("query")
    query.add_argument("--session-id", required=True)
    sub.add_parser("sync")
    sub.add_parser("pending")
    sub.add_parser("retry-pending")
    args = parser.parse_args()
    state_file: Path = args.state_file
    if not state_file.is_absolute():
        parser.error("device state path must be absolute")
    if args.step != "init":
        _private(state_file, "device state")
    if args.step == "init":
        result = _call(args.binary, "init", "--state-file", str(state_file))
        evidence = {
            "step": "init",
            "signing_public_key": result.get("signing_public_key"),
            "exchange_public_key": result.get("exchange_public_key"),
        }
    elif args.step == "pair":
        _private(args.ticket_file, "pairing ticket")
        result = _call(
            args.binary,
            "pair",
            "--state-file",
            str(state_file),
            "--ticket-file",
            str(args.ticket_file),
            "--core-origin",
            args.core_origin,
            "--display-name",
            args.display_name,
        )
        evidence = {
            "step": "pair",
            "host_id": result.get("host_id"),
            "device_id": result.get("device_id"),
            "scopes": result.get("scopes"),
        }
    elif args.step == "bind":
        _call(
            args.binary,
            "bind-session",
            "--state-file",
            str(state_file),
            "--session-id",
            args.remote_session_id,
        )
        result = _call(
            args.binary,
            "sync",
            "--state-file",
            str(state_file),
            "--core-origin",
            args.core_origin,
        )
        evidence = {
            "step": "bind",
            "remote_session_id": args.remote_session_id,
            "next_cursor": result.get("next_cursor"),
            "event_count": len(result.get("items", [])),
        }
    elif args.step == "create":
        receipt = _command(
            args.binary,
            state_file,
            args.core_origin,
            "create",
            "new",
            {
                "role_id": args.role_id,
                "project_id": args.project_id,
                "model_profile_id": args.model_profile_id,
            },
        )
        evidence = {"step": "create", **receipt}
    elif args.step == "run":
        _private(args.message_file, "message")
        message = args.message_file.read_text(encoding="utf-8")
        receipt = _command(
            args.binary,
            state_file,
            args.core_origin,
            "run",
            args.session_id,
            {"message": message},
        )
        evidence = {"step": "run", "session_id": args.session_id, **receipt}
    elif args.step in {"status", "cancel"}:
        receipt = _command(
            args.binary,
            state_file,
            args.core_origin,
            args.step,
            args.session_id,
            {},
        )
        evidence = {"step": args.step, "session_id": args.session_id, **receipt}
    elif args.step == "query":
        result = _call(
            args.binary,
            "query-result",
            "--state-file",
            str(state_file),
            "--core-origin",
            args.core_origin,
            "--session-id",
            args.session_id,
        )
        items = result.get("results", [])
        if result.get("session_id") != args.session_id or not isinstance(items, list):
            raise AssertionError("encrypted Query result has wrong Session binding")
        evidence = {
            "step": "query",
            "session_id": args.session_id,
            "latest_cursor": result.get("latest_cursor"),
            "event_types": [item.get("event_type") for item in items if isinstance(item, dict)],
            "completed_content_sha256": [
                hashlib.sha256(item["content"].encode()).hexdigest()
                for item in items
                if isinstance(item, dict)
                and item.get("event_type") == "agent.completed"
                and isinstance(item.get("content"), str)
            ],
        }
    elif args.step == "sync":
        result = _call(
            args.binary,
            "sync",
            "--state-file",
            str(state_file),
            "--core-origin",
            args.core_origin,
        )
        items = result.get("items", [])
        if not isinstance(items, list):
            raise ValueError("cursor sync returned no event page")
        evidence = {
            "step": "sync",
            "next_cursor": result.get("next_cursor"),
            "events": [
                {
                    "cursor": item.get("cursor"),
                    "command_id": item.get("command_id"),
                    "status": item.get("status"),
                    "event_type": item.get("event_type"),
                }
                for item in items
                if isinstance(item, dict)
            ],
        }
    elif args.step == "pending":
        result = _call(args.binary, "pending", "--state-file", str(state_file))
        evidence = {"step": "pending", "command_id": result.get("command_id")}
    else:
        result = _call(
            args.binary,
            "retry-pending",
            "--state-file",
            str(state_file),
            "--core-origin",
            args.core_origin,
        )
        receipt = result.get("receipt")
        recovered = result.get("event")
        source = receipt if isinstance(receipt, dict) else recovered
        evidence = {
            "step": "retry-pending",
            "source": result.get("type"),
            "command_id": source.get("command_id") if isinstance(source, dict) else None,
            "status": source.get("status") if isinstance(source, dict) else None,
        }
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
