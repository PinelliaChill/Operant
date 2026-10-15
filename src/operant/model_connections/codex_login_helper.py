"""Own one isolated Codex login child until Core consumes or abandons it.

The parent pipe closes even if Core crashes. This process then stops its own
child process group and removes only the temporary home passed by Core.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)


def main() -> int:
    cli, home_name, parent_fd_text, marker_name = sys.argv[1:]
    home = Path(home_name)
    marker = Path(marker_name)
    parent_fd = int(parent_fd_text)
    child: subprocess.Popen[bytes] | None = None

    def stop_on_signal(_number: int, _frame: object) -> None:
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        child = subprocess.Popen(
            [cli, "login"],
            cwd=home,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Core owns this helper's new session, so a forced group stop also
            # reaches the CLI if the helper itself becomes unresponsive.
            start_new_session=False,
            close_fds=True,
        )
        deadline = time.monotonic() + 600
        while True:
            if child.poll() is not None:
                # Only the bounded exit code crosses this pipe. CLI output is
                # never stored because it may contain an authorization URL.
                sys.stdout.write(f"{child.returncode}\n")
                sys.stdout.flush()
                # Core must read auth.json before this home is removed.
                os.read(parent_fd, 1)
                return 0
            if time.monotonic() >= deadline:
                _stop(child)
                sys.stdout.write("timeout\n")
                sys.stdout.flush()
                os.read(parent_fd, 1)
                return 0
            ready, _, _ = select.select([parent_fd], [], [], 0.25)
            if ready and not os.read(parent_fd, 1):
                _stop(child)
                return 0
    finally:
        if child is not None:
            _stop(child)
        shutil.rmtree(home, ignore_errors=True)
        # A small private tombstone lets a restarted Core distinguish an
        # abandoned, already-reaped login from an unknown live subprocess.
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, b"stopped\n")
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
