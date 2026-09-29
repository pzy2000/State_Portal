"""Keep a headed macOS C1 process group minimized/hidden without touching pixels.

The native C1 backends capture application-owned framebuffers and dispatch events
through private in-process queues. Their windows therefore never need to be
frontmost. This guard hides every AppKit application in the launched process group
through shutdown; it also audits unavoidable Unity startup windows from their
first visible sample until they have been minimized/hidden.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class BackgroundWindowGuardError(RuntimeError):
    """The macOS process-group background contract could not be established."""


class MacOSBackgroundWindowGuard:
    schema_version = 1
    mode = "macos_process_group_hide_v1"

    def __init__(self, report_dir: str | Path) -> None:
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.status_path = self.report_dir / "c1-background-window-guard.json"
        self.stdout_path = self.report_dir / "c1-background-window-guard.stdout.log"
        self.stderr_path = self.report_dir / "c1-background-window-guard.stderr.log"
        self._process: subprocess.Popen[str] | None = None
        self._stdout_handle: Any | None = None
        self._stderr_handle: Any | None = None
        self._target_process_group: int | None = None

    @property
    def enabled(self) -> bool:
        return sys.platform == "darwin"

    @property
    def active(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _binary(self) -> Path:
        source = Path(__file__).with_name("c1_background_window_guard.swift")
        if not source.is_file():
            raise BackgroundWindowGuardError(
                f"background window guard source is missing: {source}"
            )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:20]
        cache = Path(tempfile.gettempdir()) / "warptest-c1-background-window-guard"
        cache.mkdir(parents=True, exist_ok=True)
        binary = cache / f"guard-{digest}"
        lock_path = cache / f"guard-{digest}.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if binary.is_file() and os.access(binary, os.X_OK):
                return binary
            temporary = cache / f".{binary.name}.{os.getpid()}.tmp"
            temporary.unlink(missing_ok=True)
            completed = subprocess.run(
                ["xcrun", "swiftc", str(source), "-o", str(temporary)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if completed.returncode != 0 or not temporary.is_file():
                temporary.unlink(missing_ok=True)
                raise BackgroundWindowGuardError(
                    "cannot compile macOS background window guard: "
                    + (completed.stderr.strip() or completed.stdout.strip())
                )
            temporary.chmod(0o755)
            os.replace(temporary, binary)
        return binary

    def prepare(self) -> None:
        if self.enabled:
            self._binary()

    def start(self, target_pid: int) -> None:
        if not self.enabled:
            return
        if isinstance(target_pid, bool) or not isinstance(target_pid, int) or target_pid <= 0:
            raise BackgroundWindowGuardError("target PID must be a positive integer")
        self.close()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.status_path.unlink(missing_ok=True)
        self._target_process_group = target_pid
        self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            [self._binary(), str(target_pid), str(self.status_path)],
            cwd=str(self.report_dir),
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
            text=True,
            start_new_session=True,
        )

    def metadata(self, *, required: bool = False) -> dict[str, Any]:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            if required:
                raise BackgroundWindowGuardError(
                    f"background window guard status is unavailable: {error}"
                ) from error
            return {
                "schema_version": self.schema_version,
                "mode": self.mode,
                "enabled": self.enabled,
                "active": self.active,
                "target_process_group": self._target_process_group,
            }
        if not isinstance(value, dict):
            raise BackgroundWindowGuardError("background window guard status is malformed")
        return {**value, "enabled": self.enabled, "active": self.active}

    def await_hidden(self, *, timeout_s: float = 15.0) -> dict[str, Any]:
        if not self.enabled:
            return self.metadata()
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                raise BackgroundWindowGuardError(
                    "background window guard exited before proving hidden state"
                )
            last = self.metadata()
            applications = last.get("observed_application_pids")
            windows = last.get("observed_window_pids")
            visible = last.get("visible_application_pids")
            scan_count = last.get("scan_count")
            if (
                isinstance(applications, list)
                and isinstance(windows, list)
                and isinstance(visible, list)
                and not visible
                and isinstance(scan_count, int)
                and scan_count >= 5
            ):
                return last
            time.sleep(0.02)
        raise BackgroundWindowGuardError(
            f"process group did not remain hidden within {timeout_s:g}s: {last}"
        )

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for name in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, name, None)


__all__ = ["BackgroundWindowGuardError", "MacOSBackgroundWindowGuard"]
