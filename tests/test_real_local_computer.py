"""Opt-in macOS accessibility acceptance against an isolated temporary App."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from operant.remote.local_computer import ComputerTargetError, ComputerTargetPolicy, MacComputer


def test_real_frontmost_app_button_click(tmp_path: Path) -> None:
    if os.environ.get("OPERANT_LOCAL_COMPUTER_TEST") != "1":
        pytest.skip("set OPERANT_LOCAL_COMPUTER_TEST=1 for real macOS accessibility acceptance")
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("macOS and swiftc are required")
    source = Path(__file__).parent / "fixtures" / "local_computer"
    contents = tmp_path / "OperantAcceptance.app" / "Contents"
    executable = contents / "MacOS" / "OperantAcceptance"
    executable.parent.mkdir(parents=True)
    shutil.copyfile(source / "Info.plist", contents / "Info.plist")
    compile_env = {
        **os.environ,
        "SWIFT_MODULECACHE_PATH": str(tmp_path / "swift-cache"),
        "CLANG_MODULE_CACHE_PATH": str(tmp_path / "clang-cache"),
    }
    subprocess.run(
        ["swiftc", str(source / "Acceptance.swift"), "-o", str(executable)],
        env=compile_env,
        check=True,
        timeout=120,
        capture_output=True,
    )
    process = subprocess.Popen(
        [str(executable)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env={
            "HOME": os.environ.get("HOME", str(Path.home())),
            "TMPDIR": os.environ.get("TMPDIR", "/private/tmp"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        },
    )
    try:
        computer = MacComputer(ComputerTargetPolicy(frozenset({"dev.operant.acceptance"})))
        deadline = time.monotonic() + 5
        while True:
            try:
                observation = computer.observe()
                break
            except ComputerTargetError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        assert observation["bundle_id"] == "dev.operant.acceptance"
        assert "Run Check" in observation["buttons"]
        clicked = computer.click_button(expected=observation, button_name="Run Check")
        assert clicked["window_title"] == "Operant Acceptance Clicked"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        shutil.rmtree(contents.parent)
