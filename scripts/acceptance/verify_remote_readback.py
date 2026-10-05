#!/usr/bin/env python3
"""Read back authoritative H-17 receipts without printing payloads or secrets.

Run this on the Host after a separate real device submitted commands. It does
not submit work, start servers, or replace the cross-device test.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _items(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [_object(item, label) for item in value]


def _get(client: httpx.Client, path: str, *, params: dict[str, Any] | None = None) -> Any:
    response = client.get(path, params=params)
    if response.status_code != 200:
        # Response bodies may contain user data. Only the status is reported.
        raise RuntimeError(f"GET {path} returned HTTP {response.status_code}")
    return response.json()


def _poll(
    client: httpx.Client,
    path: str,
    *,
    timeout: float,
    terminal: set[str],
    label: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        value = _object(_get(client, path), label)
        if value.get("status") in terminal:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{label} did not reach a terminal status")
        time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-url", required=True, help="temporary HTTPS loopback Core origin")
    parser.add_argument("--ca-cert", type=Path, required=True, help="trusted test CA/certificate")
    parser.add_argument("--command-id", action="append", default=[])
    parser.add_argument("--command-status", default="completed")
    parser.add_argument("--session-id")
    parser.add_argument(
        "--session-after-cursor",
        type=int,
        default=0,
        help="Session cursor saved before the remote run; require new model events after it",
    )
    parser.add_argument("--target-job-id")
    parser.add_argument("--target-job-status", default="succeeded")
    parser.add_argument("--host-id")
    parser.add_argument("--remote-session-id")
    parser.add_argument("--after-cursor", type=int, default=0)
    parser.add_argument("--expect-command-event-id")
    parser.add_argument("--expect-command-event-status")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if (
        not args.ca_cert.is_file()
        or args.timeout <= 0
        or args.after_cursor < 0
        or args.session_after_cursor < 0
    ):
        parser.error("CA certificate, timeout, or cursor is invalid")
    if not args.command_id and not args.session_id and not args.target_job_id:
        parser.error("supply a command, Session, or Target Job ID")
    if args.remote_session_id and not args.host_id:
        parser.error("--remote-session-id requires --host-id")
    if (args.expect_command_event_id or args.expect_command_event_status) and not args.host_id:
        parser.error("expected command event requires --host-id")
    if args.expect_command_event_status and not args.expect_command_event_id:
        parser.error("expected command status requires --expect-command-event-id")
    core_url = httpx.URL(args.core_url)
    if (
        core_url.scheme != "https"
        or core_url.host != "127.0.0.1"
        or core_url.username
        or core_url.password
        or core_url.path not in {"", "/"}
        or core_url.query
        or core_url.fragment
    ):
        parser.error("Core URL must be an HTTPS 127.0.0.1 loopback origin")

    summary: dict[str, Any] = {"commands": [], "session": None, "target_job": None}
    with httpx.Client(
        base_url=args.core_url,
        verify=str(args.ca_cert),
        trust_env=False,
        follow_redirects=False,
        timeout=10,
    ) as client:
        for command_id in args.command_id:
            receipt = _poll(
                client,
                f"/v1/remote-control/commands/{quote(command_id, safe='')}",
                timeout=args.timeout,
                terminal={
                    "completed",
                    "rejected",
                    "outcome_unknown",
                },
                label="command receipt",
            )
            if receipt.get("command_id") != command_id:
                raise AssertionError("command ID mismatch")
            if receipt.get("status") != args.command_status:
                raise AssertionError("unexpected command status")
            if receipt.get("host_acknowledged_at") is None:
                raise AssertionError("command lacks durable Host Ack")
            summary["commands"].append(
                {
                    "command_id": command_id,
                    "status": receipt["status"],
                    "action_hash": receipt.get("action_hash"),
                    "result_ref": receipt.get("result_ref"),
                    "error_code": receipt.get("error_code"),
                    "host_ack": True,
                }
            )

        if args.session_id:
            session = _object(
                _get(client, f"/v1/sessions/{quote(args.session_id, safe='')}"),
                "session",
            )
            if session.get("id") != args.session_id:
                raise AssertionError("Session ID mismatch")
            events_path = f"/v1/sessions/{quote(args.session_id, safe='')}/events"
            deadline = time.monotonic() + args.timeout
            while True:
                events = _items(
                    _get(
                        client,
                        events_path,
                        params={"after_cursor": args.session_after_cursor, "limit": 1000},
                    ),
                    "events",
                )
                types = [event.get("event_type") for event in events]
                if "agent.completed" in types or any(
                    item in types for item in ("agent.failed", "agent.cancelled", "agent.timed_out")
                ):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Session Run did not reach a terminal event")
                time.sleep(0.5)
            if "model.completed" not in types or "agent.completed" not in types:
                raise AssertionError("Session lacks successful real-model terminal events")
            cursors = [event.get("cursor") for event in events]
            if any(
                not isinstance(cursor, int) or cursor <= args.session_after_cursor
                for cursor in cursors
            ) or cursors != sorted(set(cursors)):
                raise AssertionError("Session event cursors are not strictly increasing")
            summary["session"] = {
                "session_id": args.session_id,
                "last_cursor": cursors[-1],
                "model_completed_count": types.count("model.completed"),
                "agent_completed": True,
            }

        if args.target_job_id:
            job = _poll(
                client,
                f"/v1/remote-targets/jobs/{quote(args.target_job_id, safe='')}/result",
                timeout=args.timeout,
                terminal={
                    "succeeded",
                    "failed",
                    "cancelled",
                    "manual_reconcile_required",
                },
                label="Target Job result",
            )
            if job.get("job_id") != args.target_job_id:
                raise AssertionError("Target Job ID mismatch")
            if job.get("status") != args.target_job_status:
                raise AssertionError("unexpected Target Job status")
            result = job.get("result")
            summary["target_job"] = {
                "job_id": args.target_job_id,
                "target_id": job.get("target_id"),
                "status": job["status"],
                "result_id": result.get("result_id") if isinstance(result, dict) else None,
            }

        if args.host_id:
            events: list[dict[str, Any]] = []
            cursor = args.after_cursor
            while True:
                event_page = _object(
                    _get(
                        client,
                        "/v1/remote-control/events",
                        params={
                            "host_id": args.host_id,
                            "session_id": args.remote_session_id,
                            "after_cursor": cursor,
                            "limit": 500,
                        },
                    ),
                    "Remote Control events",
                )
                page = _items(event_page.get("items"), "Remote Control events")
                if not page:
                    break
                next_cursor = event_page.get("next_cursor")
                if not isinstance(next_cursor, int) or next_cursor <= cursor:
                    raise AssertionError("Remote Control event cursor did not advance")
                events.extend(page)
                cursor = next_cursor
                if len(page) < 500:
                    break
            cursors = [item.get("cursor") for item in events]
            if any(
                not isinstance(cursor, int) or cursor <= args.after_cursor for cursor in cursors
            ):
                raise AssertionError("Remote Control event cursor did not advance correctly")
            if cursors != sorted(set(cursors)):
                raise AssertionError("Remote Control event cursors are not strictly increasing")
            if args.expect_command_event_id and not any(
                event.get("command_id") == args.expect_command_event_id
                and (
                    args.expect_command_event_status is None
                    or event.get("status") == args.expect_command_event_status
                )
                for event in events
            ):
                raise AssertionError("expected command state is absent after the saved cursor")
            summary["remote_events"] = {
                "count": len(events),
                "next_cursor": cursor,
                "states": [
                    {"command_id": item.get("command_id"), "status": item.get("status")}
                    for item in events[-20:]
                ],
            }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
