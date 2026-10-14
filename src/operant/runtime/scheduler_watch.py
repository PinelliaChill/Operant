"""Local file metadata and Git HEAD observations for the durable Scheduler."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from operant.application.scheduler import SchedulerConflictError
from operant.domain.scheduler import RunRequest, ScheduleDefinition, SchedulerLease
from operant.persistence.scheduler import SQLiteSchedulerStore


class WatchProbeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _checked_path(schedule: ScheduleDefinition) -> tuple[Path, Path]:
    root_value = schedule.workflow_input.get("workspace_or_target")
    if not isinstance(root_value, str) or not schedule.watch_path:
        raise WatchProbeError("watch.workspace_invalid")
    root = Path(root_value)
    target = Path(schedule.watch_path)
    if (
        not root.is_absolute()
        or not target.is_absolute()
        or ".." in root.parts
        or ".." in target.parts
        or root == Path(root.anchor)
        or target == root
        or not target.is_relative_to(root)
    ):
        raise WatchProbeError("watch.path_outside_workspace")
    # lstat each existing component. A symlink anywhere in the path could move
    # the observation outside the pinned workspace after schedule creation.
    for path in reversed(target.parents):
        if path == Path(path.anchor):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise WatchProbeError("watch.symlink_rejected")
    try:
        if stat.S_ISLNK(target.lstat().st_mode):
            raise WatchProbeError("watch.symlink_rejected")
    except FileNotFoundError:
        pass
    if not root.is_dir():
        raise WatchProbeError("watch.workspace_missing")
    return root, target


def _file_fingerprint(target: Path) -> str:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return hashlib.sha256(b"missing").hexdigest()
    if not stat.S_ISREG(info.st_mode):
        raise WatchProbeError("watch.file_not_regular")
    # Only stat metadata is observed; file contents (including credentials) are never read.
    payload = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _git_head_fingerprint(root: Path, target: Path) -> str:
    if not target.is_dir():
        raise WatchProbeError("watch.git_directory_missing")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(target),
                "rev-parse",
                "--show-toplevel",
                "--verify",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WatchProbeError("watch.git_unavailable") from exc
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or len(lines) != 2:
        raise WatchProbeError("watch.git_head_unavailable")
    git_root = Path(lines[0])
    if not git_root.is_relative_to(root):
        raise WatchProbeError("watch.git_root_outside_workspace")
    head = lines[1]
    if len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        raise WatchProbeError("watch.git_head_invalid")
    return hashlib.sha256(head.encode()).hexdigest()


def probe(schedule: ScheduleDefinition) -> str:
    root, target = _checked_path(schedule)
    if schedule.hook_event_type == "file.changed":
        return _file_fingerprint(target)
    if schedule.hook_event_type == "git.head.changed":
        return _git_head_fingerprint(root, target)
    raise WatchProbeError("watch.unsupported_kind")


class SchedulerWatchService:
    def __init__(self, store: SQLiteSchedulerStore) -> None:
        self.store = store
        self._next_index = 0

    def materialize_all(
        self,
        *,
        leader_lease: SchedulerLease,
        now: datetime,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[RunRequest, ...]:
        del now  # Each fenced commit uses the time after its own probe.
        queued: list[RunRequest] = []
        watch_ids = [
            schedule_id
            for schedule_id in self.store.list_enabled_schedule_ids()
            if self.store.get_schedule(schedule_id).hook_event_type
            in {"file.changed", "git.head.changed"}
        ]
        if not watch_ids:
            self._next_index = 0
            return ()
        count = min(len(watch_ids), 8)
        selected = [
            watch_ids[(self._next_index + offset) % len(watch_ids)] for offset in range(count)
        ]
        self._next_index = (self._next_index + count) % len(watch_ids)
        for schedule_id in selected:
            if should_stop is not None and should_stop():
                break
            schedule = self.store.get_schedule(schedule_id)
            try:
                fingerprint, error_code = probe(schedule), None
            except WatchProbeError as exc:
                fingerprint, error_code = None, exc.code
            except OSError:
                fingerprint, error_code = None, "watch.filesystem_error"
            if should_stop is not None and should_stop():
                break
            try:
                request = self.store.observe_watch(
                    schedule,
                    fingerprint=fingerprint,
                    error_code=error_code,
                    leader_lease=leader_lease,
                    observed_at=datetime.now(timezone.utc),
                )
            except SchedulerConflictError:
                # A concurrent pause/version change won the race; no request is emitted.
                continue
            if request is not None:
                queued.append(request)
        return tuple(queued)
