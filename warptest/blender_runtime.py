from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .reports import write_json
from .runtime import RuntimeErrorWithLog


class BlenderRuntimeAdapter:
    """Visible Blender GUI runtime driven by bpy/operator scripts.

    The adapter intentionally does not use ``--background``. It launches a normal
    Blender app process with ``--factory-startup`` and runs a short Python driver
    that creates, saves, reopens, and validates scene checkpoints.
    """

    def __init__(self, repo: Path, report_dir: Path, headless: bool = True) -> None:
        self.repo = repo.expanduser().resolve()
        self.report_dir = report_dir.expanduser().resolve()
        self.headless = headless
        self.app_url = None
        self.logs: list[str] = []
        self._last_screenshot_path: str | None = None
        self._last_screenshot_metadata: dict[str, Any] = {}
        self.executable = find_blender_executable(self.repo)
        self.launch_command = self._format_launch_command()

    def __enter__(self) -> "BlenderRuntimeAdapter":
        self.report_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def _format_launch_command(self) -> str:
        driver = Path(__file__).with_name("blender_driver.py")
        return " ".join(
            shlex.quote(part)
            for part in [
                str(self.executable),
                "--factory-startup",
                "--python",
                str(driver),
                "--",
                "{request}",
                "{report}",
            ]
        )

    def run_checkpoint(self, spec: dict[str, Any], spec_path: Path) -> dict[str, Any]:
        out_dir = self.report_dir / "blender"
        artifact_dir = out_dir / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        request_path = (out_dir / f"{spec_path.stem}-blender-request.json").resolve()
        report_path = (out_dir / f"{spec_path.stem}-blender-result.json").resolve()
        stdout_path = (out_dir / f"{spec_path.stem}-blender-stdout.log").resolve()
        stderr_path = (out_dir / f"{spec_path.stem}-blender-stderr.log").resolve()
        screenshot_before = f"screenshots/{spec_path.stem}.before.png"
        screenshot_after = f"screenshots/{spec_path.stem}.after.png"
        hold_ms = int(os.environ.get("WARPTEST_BLENDER_GUI_HOLD_MS", "1000" if not self.headless else "0"))
        request = {
            "spec_path": str(spec_path),
            "spec": spec,
            "artifact_dir": str(artifact_dir.resolve()),
            "screenshot_before": screenshot_before,
            "screenshot_after": screenshot_after,
            "visible_gui": True,
            "hold_ms": hold_ms,
        }
        write_json(request_path, request)
        command = [
            str(self.executable),
            "--factory-startup",
            "--python",
            str(Path(__file__).with_name("blender_driver.py")),
            "--",
            str(request_path),
            str(report_path),
        ]
        timeout = _timeout_seconds(spec)
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                completed = subprocess.run(
                    command,
                    cwd=str(self.repo),
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    timeout=timeout,
                    env=self._env(out_dir),
                )
        except subprocess.TimeoutExpired as error:
            return self._failure_payload(
                request_path,
                report_path,
                stdout_path,
                stderr_path,
                f"Blender runtime timed out after {timeout:g}s: {error}",
                "probe.blender_timeout",
            )
        except OSError as error:
            return self._failure_payload(
                request_path,
                report_path,
                stdout_path,
                stderr_path,
                f"Unable to launch Blender executable {self.executable}: {error}",
                "probe.blender_launch_failed",
            )

        result = self._load_report(report_path)
        result.update(
            {
                "exit_code": completed.returncode,
                "request_path": str(request_path),
                "report_path": str(report_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "artifact_dir": str(artifact_dir),
                "executable": str(self.executable),
                "launch_command": " ".join(shlex.quote(part) for part in command),
            }
        )
        self._record_screenshot_metadata(result)
        if completed.returncode != 0 and result.get("status") == "success":
            result["status"] = "failure"
            result.setdefault("checks", []).append(
                {
                    "name": "probe.blender_exit_code",
                    "status": "failure",
                    "detail": f"Blender exited with code {completed.returncode}.",
                    "data": {},
                    "duration_ms": 0,
                }
            )
        write_json(report_path, result)
        self.logs.extend([str(stdout_path), str(stderr_path), str(report_path), str(request_path)])
        return result

    def screenshot(self, name: str = "blender-screenshot.png", full_page: bool = False) -> str:
        if self._last_screenshot_path and Path(self._last_screenshot_path).exists():
            target = self.report_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if Path(self._last_screenshot_path).resolve() != target.resolve():
                shutil.copy2(self._last_screenshot_path, target)
            self._last_screenshot_metadata = {
                **self._last_screenshot_metadata,
                "live_source": "blender_render_capture",
                "screenshot_source": self._last_screenshot_path,
                "full_page": full_page,
            }
            return str(target)
        detail = self._last_screenshot_metadata.get("screenshot_detail") or "Blender did not produce an after screenshot."
        raise RuntimeErrorWithLog(f"Blender capture failure: {detail}")

    def last_screenshot_metadata(self) -> dict[str, Any]:
        return dict(self._last_screenshot_metadata)

    def _record_screenshot_metadata(self, result: dict[str, Any]) -> None:
        before = result.get("screenshot_before")
        after = result.get("screenshot_after")
        before_path = Path(before) if isinstance(before, str) else None
        after_path = Path(after) if isinstance(after, str) else None
        self._last_screenshot_path = str(after_path) if after_path is not None and after_path.exists() else None
        if self._last_screenshot_path:
            self._last_screenshot_metadata = {
                "live_source": "blender_render_capture",
                "screenshot_source": self._last_screenshot_path,
                "screenshot_status": "success",
                "screenshot_detail": "Blender rendered deterministic before/after scene screenshots.",
                "screenshot_before": str(before_path) if before_path is not None else None,
                "screenshot_after": str(after_path),
            }
            return
        self._last_screenshot_metadata = {
            "live_source": "capture_failure",
            "screenshot_source": "capture_failure",
            "screenshot_status": "failure",
            "screenshot_detail": "Blender driver did not report an existing after screenshot.",
            "screenshot_before": str(before_path) if before_path is not None else None,
            "screenshot_after": str(after_path) if after_path is not None else None,
        }

    def _env(self, out_dir: Path) -> dict[str, str]:
        runtime_root = out_dir / "user"
        config = runtime_root / "config"
        data = runtime_root / "data"
        scripts = runtime_root / "scripts"
        for path in [config, data, scripts]:
            path.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "BLENDER_USER_CONFIG": str(config),
                "BLENDER_USER_DATAFILES": str(data),
                "BLENDER_USER_SCRIPTS": str(scripts),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    def _load_report(self, report_path: Path) -> dict[str, Any]:
        if not report_path.exists():
            return {
                "status": "failure",
                "checks": [
                    {
                        "name": "probe.blender_report",
                        "status": "failure",
                        "detail": f"Blender did not write report JSON: {report_path}",
                        "data": {},
                        "duration_ms": 0,
                    }
                ],
        }
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as error:
            return {
                "status": "failure",
                "checks": [
                    {
                        "name": "probe.blender_report",
                        "status": "failure",
                        "detail": f"Unable to parse Blender report JSON: {error}",
                        "data": {},
                        "duration_ms": 0,
                    }
                ],
            }
        return loaded if isinstance(loaded, dict) else {"status": "failure", "checks": []}

    def _failure_payload(
        self,
        request_path: Path,
        report_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        detail: str,
        check_name: str,
    ) -> dict[str, Any]:
        payload = {
            "status": "failure",
            "checks": [
                {
                    "name": check_name,
                    "status": "failure",
                    "detail": detail,
                    "data": {},
                    "duration_ms": 0,
                }
            ],
            "request_path": str(request_path),
            "report_path": str(report_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "executable": str(self.executable),
        }
        self._last_screenshot_path = None
        self._last_screenshot_metadata = {
            "live_source": "capture_failure",
            "screenshot_source": "capture_failure",
            "screenshot_status": "failure",
            "screenshot_detail": detail,
        }
        write_json(report_path, payload)
        self.logs.extend([str(stdout_path), str(stderr_path), str(report_path), str(request_path)])
        return payload


def find_blender_executable(repo: Path) -> Path:
    explicit = os.environ.get("WARPTEST_BLENDER_EXECUTABLE")
    if explicit:
        return _require_executable(Path(explicit).expanduser(), "WARPTEST_BLENDER_EXECUTABLE")

    build_dir = os.environ.get("WARPTEST_BLENDER_BUILD_DIR")
    if build_dir:
        candidate = _candidate_in_build_dir(Path(build_dir).expanduser())
        if candidate is not None:
            return candidate
        raise RuntimeErrorWithLog(f"WARPTEST_BLENDER_BUILD_DIR does not contain a Blender executable: {build_dir}")

    candidates = [
        repo.parent / "build_darwin_lite" / "bin" / "Blender.app" / "Contents" / "MacOS" / "Blender",
        repo.parent / "build_darwin" / "bin" / "Blender.app" / "Contents" / "MacOS" / "Blender",
        repo.parent / "build_darwin_lite" / "bin" / "blender",
        repo.parent / "build_darwin" / "bin" / "blender",
    ]
    for candidate in candidates:
        if _is_executable(candidate):
            return candidate.resolve()

    path_candidate = shutil.which("blender") or shutil.which("Blender")
    if path_candidate:
        return Path(path_candidate).resolve()

    for app in sorted(Path("/Applications").glob("Blender*.app")):
        candidate = app / "Contents" / "MacOS" / "Blender"
        if _is_executable(candidate):
            return candidate.resolve()

    raise RuntimeErrorWithLog(
        "No Blender executable found. Set WARPTEST_BLENDER_EXECUTABLE, set "
        "WARPTEST_BLENDER_BUILD_DIR, install Blender.app, or build games/blender "
        "with `make lite ninja BUILD_DIR=../build_darwin_lite`."
    )


def _candidate_in_build_dir(build_dir: Path) -> Path | None:
    for candidate in [
        build_dir / "bin" / "Blender.app" / "Contents" / "MacOS" / "Blender",
        build_dir / "bin" / "blender",
        build_dir / "Blender.app" / "Contents" / "MacOS" / "Blender",
        build_dir / "blender",
    ]:
        if _is_executable(candidate):
            return candidate.resolve()
    return None


def _timeout_seconds(spec: dict[str, Any]) -> float:
    timeout_ms = spec.get("timeout_ms")
    if timeout_ms is not None:
        return max(1.0, float(timeout_ms) / 1000.0)
    return float(os.environ.get("WARPTEST_BLENDER_TIMEOUT", "120"))


def _require_executable(path: Path, label: str) -> Path:
    if not _is_executable(path):
        raise RuntimeErrorWithLog(f"{label} is not an executable Blender binary: {path}")
    return path.resolve()


def _is_executable(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)
