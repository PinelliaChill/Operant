"""Real TextEdit acceptance through an isolated HTTP Core and generated SDK.

Creates and opens a unique document in the dedicated temporary workspace.
The exact ASK action is checked before each formal approval. Evidence contains
only hashes, job identifiers, statuses, and screenshot dimensions; pixel and
clipboard bytes stay in memory. This script never opens a user document.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Thread
from typing import Any

import httpx
import uvicorn

# Running this file directly makes tests/acceptance sys.path[0]. The installed
# sdk package may be from an older build; always use this checkout's generator.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from operant.api import create_app
from operant.application.security import PolicyEngine
from operant.domain.security import Capability, PolicyBundle, PolicyDecision
from operant.remote.local_computer import ComputerTargetError, ComputerTargetPolicy, MacComputer
from sdk.python_client.phase45_generated import Phase45Client
from sdk.python_client.phase56_generated import Phase56Client
from sdk.python_client.transport import Phase56Error, TransportRequest, TransportResponse

PLUGIN = "operant.macos.computer"
BUNDLE = "com.apple.TextEdit"
TEST_CLIPBOARD = "Operant task 4 clipboard verified"
TEST_TEXT = "Operant task 4 ordinary TextEdit input verified."
TERMINAL = {"succeeded", "failed", "cancelled", "manual_reconcile_required"}
PASTEBOARD_HELPER = r"""
import AppKit
import Foundation

let board = AppKit.NSPasteboard.general
let maxBytes = 8 * 1024 * 1024
guard CommandLine.arguments.count == 2 else { exit(2) }
if CommandLine.arguments[1] == "snapshot" {
    var items: [[String: Data]] = []
    for original in board.pasteboardItems ?? [] {
        var types: [String: Data] = [:]
        for type in original.types {
            guard let data = original.data(forType: type) else { exit(3) }
            types[type.rawValue] = data
        }
        items.append(types)
    }
    guard let encoded = try? PropertyListSerialization.data(
        fromPropertyList: items, format: .binary, options: 0
    ), encoded.count <= maxBytes else { exit(4) }
    FileHandle.standardOutput.write(encoded)
} else if CommandLine.arguments[1] == "restore" {
    let encoded = FileHandle.standardInput.readDataToEndOfFile()
    guard encoded.count <= maxBytes,
          let decoded = try? PropertyListSerialization.propertyList(
              from: encoded, options: [], format: nil
          ) as? [[String: Data]] else { exit(5) }
    var items: [AppKit.NSPasteboardItem] = []
    for original in decoded {
        let item = AppKit.NSPasteboardItem()
        for (name, data) in original {
            guard item.setData(data, forType: AppKit.NSPasteboard.PasteboardType(name)) else {
                exit(6)
            }
        }
        items.append(item)
    }
    board.clearContents()
    if !items.isEmpty && !board.writeObjects(items) { exit(7) }
    let observed = board.pasteboardItems ?? []
    guard observed.count == decoded.count else { exit(8) }
    for (index, original) in decoded.enumerated() {
        let actual = observed[index]
        guard Set(actual.types.map { $0.rawValue }) == Set(original.keys) else { exit(8) }
        for (name, data) in original {
            guard actual.data(forType: AppKit.NSPasteboard.PasteboardType(name)) == data else {
                exit(8)
            }
        }
    }
} else {
    exit(2)
}
"""


@contextmanager
def running_core(app: Any) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("temporary Core did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def transport_for(client: httpx.Client) -> Callable[[TransportRequest], TransportResponse]:
    def transport(request: TransportRequest) -> TransportResponse:
        response = client.request(
            request.method, request.url, headers=dict(request.headers), content=request.body
        )
        return TransportResponse(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    return transport


class ExactApproval:
    def __init__(self, app: Any, phase45: Phase45Client, evidence: dict[str, Any]) -> None:
        self.app = app
        self.phase45 = phase45
        self.evidence = evidence

    def call(
        self,
        execute: Callable[[], dict[str, Any]],
        *,
        tool: str,
        operation: str,
        target: str,
        key: str,
        capabilities: set[Capability],
    ) -> dict[str, Any]:
        try:
            execute()
        except Phase56Error as exc:
            detail = exc.detail
            if not isinstance(detail, dict) or detail.get("code") != "approval_required":
                raise
            approval_id = detail.get("approval_id")
            action_hash = detail.get("action_hash")
            if not isinstance(approval_id, str) or not isinstance(action_hash, str):
                raise AssertionError("ASK did not identify a durable approval") from exc
        else:
            raise AssertionError(f"{operation} did not require the expected ASK approval")
        approval = self.phase45.get_phase45_approval(approval_id)
        action = self.app.state.phase45_action_gateway.repository.get_security_action(action_hash)
        if (
            approval["status"] != "pending"
            or approval["action_hash"] != action_hash
            or action.tool != tool
            or action.operation != operation
            or action.workspace_id != target
            or action.idempotency_key != key
            or set(action.requested_capabilities) != capabilities
        ):
            raise AssertionError("ASK action differs from the exact temporary target and scope")
        self.phase45.decide_phase45_approval(
            approval_id,
            {"approved": True, "reason_code": "authorized-temporary-textedit-acceptance"},
            idempotency_key=f"decide:{key}",
        )
        result = execute()  # Same key and action, once after exact approval only.
        if self.phase45.get_phase45_approval(approval_id)["status"] != "consumed":
            raise AssertionError("approved capability was not consumed")
        self.evidence["approvals"].append(
            {"tool": tool, "operation": operation, "action_hash": action_hash}
        )
        return result


def wait_result(client: Phase56Client, job_id: str, operation: str) -> dict[str, Any]:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        result = client.get_remote_target_job_result(job_id)
        if result["status"] in TERMINAL:
            if result["operation"] != operation or result["status"] != "succeeded":
                # Unknown outcomes are never replayed, including after a timeout.
                raise AssertionError(f"{operation} did not complete successfully")
            return result
        time.sleep(0.1)
    raise TimeoutError(f"{operation} has no terminal result; do not retry")


def evidence_job(evidence: dict[str, Any], stage: str, result: dict[str, Any]) -> None:
    evidence["jobs"].append(
        {"stage": stage, "job_id": result["job_id"], "status": result["status"]}
    )


def checked_observation(result: dict[str, Any], expected_title: str) -> dict[str, Any]:
    observation = result.get("observation")
    if not isinstance(observation, dict) or not isinstance(observation.get("body"), dict):
        raise AssertionError("formal result lacks a bound observation")
    body = observation["body"]
    if (
        body.get("bundle_id") != BUNDLE
        or body.get("window_title") != expected_title
        or body.get("fields") != ["AXTextArea:1"]
    ):
        raise AssertionError("target App, document, or ordinary text area changed")
    return observation


def checked_artifact(
    client: Phase56Client,
    approve: ExactApproval,
    evidence: dict[str, Any],
    result: dict[str, Any],
    media_type: str,
) -> bytes:
    job_id = result["job_id"]
    target = result["target_id"]
    artifact = approve.call(
        lambda: client.get_local_control_artifact(job_id),
        tool="local_control",
        operation="read_artifact",
        target=target,
        key=f"artifact-read:{job_id}",
        capabilities={
            Capability.COMPUTER_SCREENSHOT
            if media_type == "image/png"
            else Capability.COMPUTER_CLIPBOARD_READ
        },
    )
    raw = base64.b64decode(artifact["base64"], validate=True)
    digest = hashlib.sha256(raw).hexdigest()
    if (
        artifact["media_type"] != media_type
        or digest != artifact["sha256"]
        or digest != result["result"]["artifact_sha256"]
        or result["result"]["artifact_ref"] != f"local-capability:{job_id}"
    ):
        raise AssertionError("artifact content does not match the acknowledged Core result")
    record: dict[str, Any] = {"job_id": job_id, "media_type": media_type, "sha256": digest}
    if media_type == "image/png":
        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 24:
            raise AssertionError("window artifact is not PNG")
        width, height = struct.unpack(">II", raw[16:24])
        if not 100 <= width <= 10_000 or not 100 <= height <= 10_000:
            raise AssertionError("window artifact dimensions are implausible")
        record["dimensions"] = [width, height]
    evidence["artifacts"].append(record)
    return raw


def preflight(expected_title: str, document: Path) -> None:
    if sys.platform != "darwin" or document.exists():
        raise RuntimeError("real TextEdit acceptance requires a new temporary document")
    if (
        not document.name.startswith("Operant-Task4-acceptance-")
        or not document.name.endswith(".txt")
        or (document.parent.name != "workspace" or document.parent.parent.name != "beta-task4")
    ):
        raise ValueError("document is outside the dedicated temporary acceptance workspace")
    if document.name != expected_title or not document.parent.is_dir():
        raise ValueError("temporary document title or workspace is invalid")
    document.write_text("Operant task 4 temporary TextEdit document.\n", encoding="utf-8")
    opened = subprocess.run(
        ["/usr/bin/open", "-a", "/System/Applications/TextEdit.app", str(document)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if opened.returncode != 0:
        raise RuntimeError("dedicated temporary TextEdit document did not open")
    computer = MacComputer(ComputerTargetPolicy(frozenset({BUNDLE})), target_bundle_id=BUNDLE)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            observed = computer.observe()
        except ComputerTargetError:
            time.sleep(0.2)
            continue
        if observed.get("window_title") == expected_title and observed.get("fields") == [
            "AXTextArea:1"
        ]:
            return
        time.sleep(0.2)
    raise RuntimeError("front TextEdit document or ordinary text area does not match")


@contextmanager
def clipboard_snapshot() -> Iterator[tuple[Path, bytes]]:
    """Preserve all available pasteboard item types entirely in process memory."""
    with tempfile.TemporaryDirectory(prefix="operant-task4-pasteboard-") as directory:
        source = Path(directory) / "pasteboard.swift"
        helper = Path(directory) / "pasteboard"
        source.write_text(PASTEBOARD_HELPER, encoding="utf-8")
        helper_env = {
            "HOME": str(Path.home()),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": directory,
            "SWIFT_MODULECACHE_PATH": str(Path(directory) / "swift-cache"),
            "CLANG_MODULE_CACHE_PATH": str(Path(directory) / "clang-cache"),
        }
        built = subprocess.run(
            ["/usr/bin/swiftc", str(source), "-o", str(helper)],
            capture_output=True,
            timeout=90,
            check=False,
            env=helper_env,
        )
        if built.returncode != 0:
            raise RuntimeError("full-format clipboard backup helper did not compile")
        captured = subprocess.run(
            [str(helper), "snapshot"],
            capture_output=True,
            timeout=10,
            check=False,
            env=helper_env,
        )
        if captured.returncode != 0 or len(captured.stdout) > 8 * 1024 * 1024:
            raise RuntimeError("clipboard cannot be fully backed up; no write was attempted")
        yield helper, captured.stdout


def restore_clipboard(helper: Path, snapshot: bytes) -> None:
    restored = subprocess.run(
        [str(helper), "restore"],
        input=snapshot,
        capture_output=True,
        timeout=10,
        check=False,
        env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    if restored.returncode != 0:
        raise RuntimeError("full-format clipboard restoration failed")


def run(evidence_dir: Path, workspace: Path) -> dict[str, Any]:
    if not evidence_dir.is_absolute() or evidence_dir.exists():
        raise ValueError("evidence directory must be a fresh absolute path")
    if evidence_dir.parent.name != "evidence" or evidence_dir.parent.parent.name != "beta-task4":
        raise ValueError("evidence must remain in the dedicated ignored beta-task4 directory")
    if (
        not workspace.is_absolute()
        or workspace.name != "workspace"
        or workspace.parent.name != "beta-task4"
        or not workspace.is_dir()
    ):
        raise ValueError("workspace must be the dedicated absolute beta-task4 workspace")
    document = workspace / f"Operant-Task4-acceptance-{uuid.uuid4().hex[:10]}.txt"
    expected_title = document.name
    evidence_dir.mkdir(parents=True, mode=0o700)
    evidence: dict[str, Any] = {
        "status": "failed",
        "entry": "isolated HTTP Core + generated Phase56/Phase45 SDK",
        "policy_default": "ask",
        "bundle_id": BUNDLE,
        "document_title": expected_title,
        "jobs": [],
        "approvals": [],
        "artifacts": [],
        "core_module": str(Path(sys.modules[create_app.__module__].__file__ or "").resolve()),
        "sdk_module": str(Path(sys.modules[Phase56Client.__module__].__file__ or "").resolve()),
    }
    session_id: str | None = None
    stack = ExitStack()
    clipboard_helper: Path | None = None
    original_clipboard: bytes | None = None
    clipboard_dirty = False
    try:
        evidence["stage"] = "clipboard_backup"
        clipboard_helper, original_clipboard = stack.enter_context(clipboard_snapshot())
        evidence["stage"] = "textedit_preflight"
        preflight(expected_title, document)
        evidence["stage"] = "create_core"
        app = create_app(
            evidence_dir / "core.sqlite3",
            artifact_root=evidence_dir / "artifacts",
            phase45_policy_engine=PolicyEngine(
                PolicyBundle(
                    bundle_id="beta-task4-real-textedit",
                    version="acceptance.v1",
                    default_decision=PolicyDecision.ASK,
                    rules=(),
                )
            ),
            phase56_local_authorizer=lambda request: (
                request.client is not None and request.client.host in {"127.0.0.1", "::1"}
            ),
        )
        with (
            running_core(app) as origin,
            httpx.Client(base_url=origin, trust_env=False, timeout=15) as http,
        ):
            evidence["stage"] = "phase56_negotiate"
            metadata_response = http.get("/v1/protocol/phase56")
            metadata = metadata_response.json()
            evidence["phase56_metadata"] = {
                "http_status": metadata_response.status_code,
                "protocol_version": metadata.get("protocol_version"),
                "schema_digest": metadata.get("schema_digest"),
                "min_client_version": metadata.get("min_client_version"),
                "capabilities_count": len(metadata.get("capabilities", [])),
            }
            transport = transport_for(http)
            client = Phase56Client(origin, transport=transport)
            phase45 = Phase45Client(origin, transport=transport)
            client.negotiate_protocol()
            evidence["stage"] = "phase45_negotiate"
            phase45.negotiate_protocol()
            evidence["stage"] = "install_plugin"
            approve = ExactApproval(app, phase45, evidence)

            def key(stage: str) -> str:
                return f"task4-textedit:{stage}:{uuid.uuid4().hex}"

            def management(
                stage: str,
                execute: Callable[[], dict[str, Any]],
                *,
                operation: str,
                target: str,
                approval_key: str,
                computer_focus: bool = False,
            ) -> dict[str, Any]:
                evidence["stage"] = stage
                outcome = approve.call(
                    execute,
                    tool="local_control",
                    operation=operation,
                    target=target,
                    key=approval_key,
                    capabilities={Capability.REMOTE_TARGET_EXEC, Capability.COMPUTER_INPUT}
                    if computer_focus
                    else {Capability.REMOTE_TARGET_EXEC},
                )
                evidence[stage] = "ok"
                return outcome

            install_key = key("install")
            management(
                "install",
                lambda: client.install_local_capability_plugin(
                    {
                        "plugin_id": PLUGIN,
                        "allowed_targets": [BUNDLE],
                        "idempotency_key": install_key,
                    },
                    idempotency_key=install_key,
                ),
                operation="install",
                target=PLUGIN,
                approval_key=install_key,
            )
            enable_key = key("enable")
            management(
                "enable",
                lambda: client.set_local_capability_plugin_enabled(
                    PLUGIN,
                    {"enabled": True, "idempotency_key": enable_key},
                    idempotency_key=enable_key,
                ),
                operation="set_enabled",
                target=PLUGIN,
                approval_key=enable_key,
            )
            open_key = key("open")
            opened = management(
                "open",
                lambda: client.open_local_control_session(
                    {
                        "plugin_id": PLUGIN,
                        "computer_bundle_id": BUNDLE,
                        "idempotency_key": open_key,
                    },
                    idempotency_key=open_key,
                ),
                operation="open",
                target=PLUGIN,
                approval_key=open_key,
                computer_focus=True,
            )
            session_id = opened["session_id"]
            if opened["state"] != "active" or opened["computer_bundle_id"] != BUNDLE:
                raise AssertionError("computer session did not bind the chosen App")
            evidence["session_id"] = session_id

            def observe(stage: str) -> dict[str, Any]:
                evidence["stage"] = stage
                request_key = key(stage)
                queued = approve.call(
                    lambda: client.observe_local_control_session(
                        session_id,
                        {"idempotency_key": request_key},
                        idempotency_key=request_key,
                    ),
                    tool="remote_target_job",
                    operation="observe_computer",
                    target=session_id,
                    key=request_key,
                    capabilities={
                        Capability.REMOTE_TARGET_EXEC,
                        Capability.COMPUTER_OBSERVE,
                        Capability.COMPUTER_INPUT,
                    },
                )
                result = wait_result(client, queued["job_id"], "observe_computer")
                evidence_job(evidence, stage, result)
                return checked_observation(result, expected_title)

            def act(
                stage: str,
                operation: str,
                observation_hash: str,
                arguments: dict[str, Any],
                capability: Capability,
            ) -> dict[str, Any]:
                evidence["stage"] = stage
                request_key = key(stage)
                queued = approve.call(
                    lambda: client.act_local_control_session(
                        session_id,
                        {
                            "operation": operation,
                            "observation_hash": observation_hash,
                            "arguments": arguments,
                            "idempotency_key": request_key,
                        },
                        idempotency_key=request_key,
                    ),
                    tool="remote_target_job",
                    operation=operation,
                    target=session_id,
                    key=request_key,
                    capabilities={
                        Capability.REMOTE_TARGET_EXEC,
                        capability,
                        Capability.COMPUTER_INPUT,
                    },
                )
                result = wait_result(client, queued["job_id"], operation)
                evidence_job(evidence, stage, result)
                return result

            initial = observe("observe_before_input")
            initial_hash = initial["observation_hash"]
            typed = act(
                "ordinary_text_input",
                "type_text",
                initial_hash,
                {"element_name": "AXTextArea:1", "value": TEST_TEXT},
                Capability.COMPUTER_INPUT,
            )
            if typed["result"]["postcondition"]["post_observation_hash"] == initial_hash:
                raise AssertionError("ordinary TextEdit field did not change")
            current = observe("observe_after_input")
            act(
                "fixed_key",
                "press_key",
                current["observation_hash"],
                {"key": "ArrowLeft"},
                Capability.COMPUTER_INPUT,
            )
            current = observe("observe_after_key")
            screenshot = act(
                "window_screenshot",
                "capture_window",
                current["observation_hash"],
                {},
                Capability.COMPUTER_SCREENSHOT,
            )
            checked_artifact(client, approve, evidence, screenshot, "image/png")
            current = observe("observe_before_clipboard")
            clipboard_dirty = True
            act(
                "clipboard_write",
                "write_clipboard",
                current["observation_hash"],
                {"value": TEST_CLIPBOARD},
                Capability.COMPUTER_CLIPBOARD_WRITE,
            )
            current = observe("observe_after_clipboard_write")
            read = act(
                "clipboard_read",
                "read_clipboard",
                current["observation_hash"],
                {},
                Capability.COMPUTER_CLIPBOARD_READ,
            )
            if (
                checked_artifact(client, approve, evidence, read, "text/plain")
                != TEST_CLIPBOARD.encode()
            ):
                raise AssertionError("clipboard artifact did not match the synthetic text")
            close_key = key("close")
            closed = management(
                "close",
                lambda: client.close_local_control_session(
                    session_id, {"idempotency_key": close_key}, idempotency_key=close_key
                ),
                operation="closed",
                target=session_id,
                approval_key=close_key,
            )
            if closed["state"] != "closed":
                raise AssertionError("local session was not closed")
            session_id = None
        db_files = list(evidence_dir.glob("core.sqlite3*"))
        evidence["db_plaintext_absent"] = all(
            TEST_TEXT.encode() not in path.read_bytes()
            and TEST_CLIPBOARD.encode() not in path.read_bytes()
            and b"\x89PNG\r\n\x1a\n" not in path.read_bytes()
            for path in db_files
        )
        if not evidence["db_plaintext_absent"]:
            raise AssertionError("Core database contains raw input or screenshot bytes")
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        if type(exc).__name__ == "ProtocolNegotiationError":
            # Generated SDK negotiation messages contain only version/digest metadata.
            evidence["protocol_error"] = str(exc)
        # A failed/unknown job is not replayed. Core shutdown revokes the lease.
    finally:
        if clipboard_dirty and clipboard_helper is not None and original_clipboard is not None:
            try:
                restore_clipboard(clipboard_helper, original_clipboard)
                evidence["clipboard_all_formats_restored"] = True
            except Exception as exc:
                evidence["clipboard_all_formats_restored"] = False
                evidence["clipboard_restore_error_type"] = type(exc).__name__
                evidence["status"] = "failed"
        stack.close()
        if session_id is not None:
            evidence["unclosed_session"] = session_id
        (evidence_dir / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    outcome = run(args.evidence_dir, args.workspace)
    print(f"{outcome['status']}: {args.evidence_dir / 'result.json'}")
    return 0 if outcome["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
