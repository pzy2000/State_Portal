"""Strict lifecycle cleanup for detached macOS Unity Hub wrappers."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


UNITY_HUB_EXECUTABLE = "/Applications/Unity Hub.app/Contents/MacOS/Unity Hub"


def _matching_detached_unity_hub_pids(
    request_path: Path,
    report_path: Path,
) -> list[int]:
    """Return only detached Hub wrappers belonging to one exact cold probe."""

    if sys.platform != "darwin":
        return []
    try:
        process = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        output, _ = process.communicate(timeout=2)
    except (OSError, subprocess.SubprocessError) as error:
        try:
            process.kill()
        except (NameError, OSError):
            pass
        raise RuntimeError(
            f"Cannot audit detached Unity Hub wrappers: {error}"
        ) from error
    request_marker = str(request_path)
    report_marker = str(report_path)
    matches: list[int] = []
    for raw in output.splitlines():
        fields = raw.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        pid_text, ppid_text, command = fields
        if (
            ppid_text != "1"
            or not command.startswith(UNITY_HUB_EXECUTABLE)
            or request_marker not in command
            or report_marker not in command
        ):
            continue
        try:
            matches.append(int(pid_text))
        except ValueError:
            continue
    return sorted(set(matches))


def cleanup_detached_unity_hub_processes(
    request_path: Path,
    report_path: Path,
    *,
    spawn_grace_s: float = 0.5,
    terminate_grace_s: float = 2.0,
) -> list[int]:
    """Reap only Hub wrappers carrying this probe's unique artifact paths."""

    if sys.platform != "darwin":
        return []
    spawn_deadline = time.monotonic() + max(0.0, spawn_grace_s)
    matches: list[int] = []
    while True:
        matches = _matching_detached_unity_hub_pids(request_path, report_path)
        if matches or time.monotonic() >= spawn_deadline:
            break
        time.sleep(0.05)
    if not matches:
        return []

    for pid in matches:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    terminate_deadline = time.monotonic() + max(0.0, terminate_grace_s)
    remaining = matches
    while remaining and time.monotonic() < terminate_deadline:
        time.sleep(0.05)
        current = set(
            _matching_detached_unity_hub_pids(request_path, report_path)
        )
        remaining = [pid for pid in remaining if pid in current]
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if remaining:
        kill_deadline = time.monotonic() + max(0.0, terminate_grace_s)
        while remaining and time.monotonic() < kill_deadline:
            time.sleep(0.05)
            current = set(
                _matching_detached_unity_hub_pids(request_path, report_path)
            )
            remaining = [pid for pid in remaining if pid in current]
    if remaining:
        raise RuntimeError(
            "Detached Unity Hub wrapper survived exact Unity probe cleanup: "
            + ", ".join(str(pid) for pid in remaining)
        )
    return matches
