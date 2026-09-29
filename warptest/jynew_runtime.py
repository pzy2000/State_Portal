from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from .runtime import json_dumps
from .unity_process import cleanup_detached_unity_hub_processes
from .unity_serialization_guard import (
    capture_jynew_serialization_guard,
    restore_jynew_serialization_guard,
)


JYNEW_HOOK_RELATIVE_PATH = Path("jyx2/Assets/Scripts/GameCore/WarptestCheckpointCommand.cs")
JYNEW_HOOK_TEMPLATE_PATH = Path(__file__).resolve().parent / "instrumentation" / "jynew" / "WarptestCheckpointCommand.cs"
DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_VISUAL_TIMEOUT_MS = 300_000
UNITY_VERSION_RE = re.compile(r"\b\d{4}\.\d+\.\d+[A-Za-z0-9.]*\b")


def ensure_jynew_instrumentation(repo: Path) -> dict[str, object]:
    """Install the bundled Unity batch-mode hook when an upstream jynew checkout lacks it."""
    repo = Path(repo).expanduser().resolve()
    target = repo / JYNEW_HOOK_RELATIVE_PATH
    source = JYNEW_HOOK_TEMPLATE_PATH
    if not source.exists():
        raise FileNotFoundError(f"Bundled jynew instrumentation template is missing: {source}")

    if target.exists():
        text = target.read_text(encoding="utf-8", errors="ignore")
        if "WarptestCheckpoint" in text and "Jyx2" in text:
            if (
                "screenshot_output_path" not in text
                or "CaptureScreenshot" not in text
                or "TextureHasVisibleRange" not in text
                or "screenshot_source" not in text
                or "PrepareVisualSceneForCapture" not in text
                or "Application.isBatchMode" not in text
                or "RunWarm" not in text
                or "RunC1" not in text
                or "warptest-c1-unity-v3" not in text
                or "transition_evidence" not in text
                or "warptest-unity-checkpoint-state-v1" not in text
                or "evidence_task_id" not in text
                or "evidence_benchmark" not in text
                or "encoderShouldEmitUTF8Identifier" not in text
                or "capture_source_width" not in text
                or "CaptureScreenshotAsTexture" in text
            ):
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                return {
                    "installed": True,
                    "reason": (
                        "refreshed stale jynew WarpTest hook with informative screenshot "
                        "and C1 background GameView IPC v3 full-frame camera capture"
                    ),
                    "path": str(target),
                    "source": str(source),
                }
            return {
                "installed": False,
                "reason": "existing jynew WarpTest hook present",
                "path": str(target),
                "source": str(source),
            }
        raise RuntimeError(f"Refusing to overwrite non-WarpTest jynew file: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "installed": True,
        "reason": "installed bundled jynew WarpTest hook",
        "path": str(target),
        "source": str(source),
    }


def find_jynew_project_path(repo: Path) -> Path:
    """Locate the Unity project root (``jyx2/``) within a jynew checkout.

    Shared by the cold-per-request adapter and the C3 warm session.
    """
    repo = Path(repo).expanduser().resolve()
    jyx2 = repo / "jyx2"
    if jyx2.is_dir() and (jyx2 / "Assets").is_dir():
        return jyx2
    if (repo / "Assets").is_dir():
        return repo
    return jyx2


class _NoOpPage:
    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class JynewRuntimeAdapter:
    """Run jynew checkpoint specs through the Unity batch-mode WarpTest command."""

    def __init__(self, repo: Path, out_dir: Path, *, headless: bool = True, visual_capture: bool = False) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.out_dir = Path(out_dir).expanduser().resolve()
        self._visual_capture = visual_capture
        self.headless = headless
        batch_flags = "-batchmode -nographics " if headless else ""
        self.launch_command = (
            f"unity {batch_flags}-projectPath {{project}} "
            "-executeMethod Jyx2.WarptestCheckpoint.Run "
            "-- --warptest-request {request} --warptest-report {report}"
        )
        self.report_dir = self.out_dir
        self.page = _NoOpPage()
        self.app_url = None
        self.logs: list[str] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.network_api_calls: dict[str, Any] = {"attempted": []}
        self._project_path = self._find_project_path()
        self._serialization_guard = capture_jynew_serialization_guard(
            self._project_path
        )
        self._last_screenshot_path: str | None = None
        self._last_screenshot_metadata: dict[str, Any] = {
            "screenshot_status": "skipped",
            "screenshot_source": "",
            "screenshot_detail": "",
            "live_source": "capture_failure",
        }

    def __enter__(self) -> "JynewRuntimeAdapter":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        install = ensure_jynew_instrumentation(self.repo)
        self.logs.append(f"jynew_instrumentation={install['reason']}:{install['path']}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        restore_jynew_serialization_guard(
            self._serialization_guard, log=self.logs.append
        )

    def _find_project_path(self) -> Path:
        """Locate the Unity project root (jyx2/) within the repo."""
        return find_jynew_project_path(self.repo)

    def run_checkpoint(
        self,
        spec: dict[str, Any],
        spec_path: Path,
        *,
        evidence_task_id: str = "",
        evidence_seed: int = 0,
        evidence_stage: str = "",
        evidence_benchmark: str = "",
    ) -> dict[str, Any]:
        request_path = self.out_dir / f"{Path(spec_path).stem}-jynew-request.json"
        report_path = self.out_dir / f"{Path(spec_path).stem}-jynew-result.json"
        stdout_path = self.out_dir / f"{Path(spec_path).stem}-jynew-stdout.log"
        stderr_path = self.out_dir / f"{Path(spec_path).stem}-jynew-stderr.log"

        # A timed-out Unity process may have written a complete report before it
        # stopped responding during shutdown.  Recovery from that lifecycle
        # timeout is safe only when the report was created by this invocation,
        # so remove any result left by an earlier run before writing the request.
        report_path.unlink(missing_ok=True)

        # A spec may declare visual_oracle metadata even in semantic-only runs.
        # Only request a Unity screenshot when the runner's oracle policy will
        # actually consume it; otherwise cold Unity imports can be cut short by
        # the shorter visual timeout cap.
        screenshot_output: str | None = None
        if self._visual_capture:
            screenshot_output = str(self.out_dir / "jynew-visual-post-smoke.png")
            output_path = Path(screenshot_output)
            if output_path.exists():
                output_path.unlink()

        request = {
            "spec_path": str(Path(spec_path).resolve()),
            "screenshot_output_path": screenshot_output or "",
            "evidence_task_id": str(evidence_task_id),
            "evidence_seed": int(evidence_seed),
            "evidence_stage": str(evidence_stage),
            "evidence_benchmark": str(evidence_benchmark),
            "spec": _build_utility_spec(spec),
        }
        request_path.write_text(json_dumps(request), encoding="utf-8")

        unity_exe = _find_jynew_unity_executable(self._project_path)
        self.logs.append(f"unity_executable={unity_exe}")
        project_version = _jynew_project_editor_version(self._project_path)
        editor_version = _unity_editor_version(unity_exe)
        self.logs.append(f"unity_project_version={project_version or 'unknown'}")
        self.logs.append(f"unity_editor_version={editor_version or 'unknown'}")
        cmd = [unity_exe]
        if self.headless:
            cmd.extend(["-batchmode", "-nographics"])
        cmd.extend([
            "-projectPath", str(self._project_path),
            "-executeMethod", "Jyx2.WarptestCheckpoint.Run",
            "-logFile", str(self.out_dir / "unity-editor.log"),
            "--",
            "--warptest-request", str(request_path),
            "--warptest-report", str(report_path),
        ])

        default_timeout_ms = DEFAULT_VISUAL_TIMEOUT_MS if screenshot_output else DEFAULT_TIMEOUT_MS
        timeout_ms = int(spec.get("timeout_ms", default_timeout_ms) or default_timeout_ms)
        if screenshot_output:
            timeout_ms = min(timeout_ms, DEFAULT_VISUAL_TIMEOUT_MS)
        timeout_seconds = timeout_ms / 1000
        try:
            try:
                completed = subprocess.run(
                    cmd,
                    cwd=str(self.repo),
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            finally:
                detached_hub_pids = cleanup_detached_unity_hub_processes(
                    request_path,
                    report_path,
                )
                if detached_hub_pids:
                    self.logs.append(
                        "detached_unity_hub_cleanup="
                        + ",".join(str(pid) for pid in detached_hub_pids)
                    )
        except FileNotFoundError:
            detail = f"Unity executable not found: {unity_exe}. Set UNITY_EDITOR_PATH or install Unity Hub."
            return {
                "status": "failure",
                "exit_code": None,
                "request_path": str(request_path),
                "report_path": str(report_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "stdout_tail": "",
                "stderr_tail": detail,
                "utility_report": {"status": "failure", "detail": detail},
                "checks": [{"name": "probe.unity_not_found", "status": "failure", "detail": detail}],
            }
        except OSError as error:
            detail = f"OS error launching Unity ({unity_exe}): {error}"
            return {
                "status": "failure",
                "exit_code": None,
                "request_path": str(request_path),
                "report_path": str(report_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "stdout_tail": "",
                "stderr_tail": detail,
                "utility_report": {"status": "failure", "detail": detail},
                "checks": [{"name": "probe.os_error", "status": "failure", "detail": detail}],
            }
        except subprocess.TimeoutExpired as error:
            stdout = _process_output(error.stdout)
            stderr = _process_output(error.stderr)
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            self.logs.extend([str(stdout_path), str(stderr_path)])
            unity_log = self.out_dir / "unity-editor.log"
            if unity_log.exists():
                self.logs.append(str(unity_log))

            utility_report = _load_utility_report(report_path)
            recovered, recovery_detail = _validate_timeout_success_report(
                utility_report,
                request=request,
                report_path=report_path,
                out_dir=self.out_dir,
                screenshot_output=screenshot_output,
            )
            if recovered:
                self.logs.append(
                    "jynew_process_exit_timeout_after_success_report="
                    f"{error.timeout}"
                )
                self._record_visual_capture(
                    utility_report,
                    screenshot_output=screenshot_output,
                    spec=spec,
                    spec_path=spec_path,
                )
                return {
                    "status": "success",
                    "exit_code": None,
                    "process_exit_status": "timed_out_after_success_report",
                    "process_exit_timeout_s": error.timeout,
                    "report_recovered_after_exit_timeout": True,
                    "request_path": str(request_path),
                    "report_path": str(report_path),
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "stdout_tail": stdout[-4000:],
                    "stderr_tail": stderr[-4000:],
                    "utility_report": utility_report,
                    "checks": utility_report.get("checks", []),
                    "screenshot_status": self._last_screenshot_metadata.get(
                        "screenshot_status"
                    ),
                    "screenshot_source": self._last_screenshot_metadata.get(
                        "screenshot_source"
                    ),
                    "screenshot_detail": self._last_screenshot_metadata.get(
                        "screenshot_detail"
                    ),
                    "live_source": self._last_screenshot_metadata.get("live_source"),
                }

            detail = (
                f"Unity batch mode timed out after {error.timeout} seconds; "
                f"success report was not recoverable: {recovery_detail}"
            )
            return {
                "status": "failure",
                "exit_code": None,
                "process_exit_status": "timed_out_without_valid_success_report",
                "process_exit_timeout_s": error.timeout,
                "report_recovered_after_exit_timeout": False,
                "request_path": str(request_path),
                "report_path": str(report_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "stdout_tail": stdout[-4000:],
                "stderr_tail": stderr[-4000:],
                "utility_report": {"status": "failure", "detail": detail},
                "checks": [
                    {
                        "name": "probe.timeout",
                        "status": "failure",
                        "detail": detail,
                    }
                ],
            }

        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        self.logs.extend([str(stdout_path), str(stderr_path)])

        unity_log = self.out_dir / "unity-editor.log"
        if unity_log.exists():
            self.logs.append(str(unity_log))

        utility_report = _load_utility_report(report_path)

        status = "success" if completed.returncode == 0 and utility_report.get("status") == "success" else "failure"
        self._record_visual_capture(
            utility_report,
            screenshot_output=screenshot_output,
            spec=spec,
            spec_path=spec_path,
        )

        return {
            "status": status,
            "exit_code": completed.returncode,
            "process_exit_status": "exited",
            "process_exit_timeout_s": None,
            "report_recovered_after_exit_timeout": False,
            "request_path": str(request_path),
            "report_path": str(report_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "utility_report": utility_report,
            "checks": utility_report.get("checks", []) if isinstance(utility_report, dict) else [],
            "screenshot_status": self._last_screenshot_metadata.get("screenshot_status"),
            "screenshot_source": self._last_screenshot_metadata.get("screenshot_source"),
            "screenshot_detail": self._last_screenshot_metadata.get("screenshot_detail"),
            "live_source": self._last_screenshot_metadata.get("live_source"),
        }

    def _record_visual_capture(
        self,
        utility_report: dict[str, Any],
        *,
        screenshot_output: str | None,
        spec: dict[str, Any],
        spec_path: Path,
    ) -> None:
        self._last_screenshot_path = None
        self._last_screenshot_metadata = _capture_metadata(utility_report)
        if not screenshot_output:
            return

        self._last_screenshot_metadata.setdefault(
            "requested_screenshot_path", screenshot_output
        )
        candidate = _unity_screenshot_path(utility_report, screenshot_output)
        if (
            self._last_screenshot_metadata.get("screenshot_status") == "success"
            and self._last_screenshot_metadata.get("screenshot_source")
            == "unity_capture"
            and candidate is not None
            and _screenshot_is_informative(candidate)
        ):
            self._last_screenshot_path = str(candidate)
            self._last_screenshot_metadata["live_source"] = "unity_capture"
            return

        self._last_screenshot_metadata["live_source"] = "capture_failure"
        detail = (
            self._last_screenshot_metadata.get("screenshot_detail")
            or "Unity did not report an informative capture."
        )
        self.logs.append(f"jynew_visual_capture_failure={detail}")
        fallback = _live_fallback_image(spec, spec_path)
        if fallback is not None:
            diagnostic = Path(screenshot_output).with_name(
                "jynew-visual-fallback.png"
            )
            shutil.copy2(fallback, diagnostic)
            self._last_screenshot_metadata["fallback_image"] = str(diagnostic)
            self.logs.append(f"jynew_visual_fallback_diagnostic={fallback}")

    def screenshot(self, name: str = "jynew-screenshot.png", full_page: bool = False) -> str:
        if self._last_screenshot_path and Path(self._last_screenshot_path).exists():
            target = self.out_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(target) != self._last_screenshot_path:
                shutil.copy2(self._last_screenshot_path, target)
            return str(target)
        target = self.out_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        detail = self._last_screenshot_metadata.get("screenshot_detail") or "Unity did not produce an informative image."
        raise RuntimeError(f"Jynew capture failure: {detail}")

    @property
    def last_screenshot_metadata(self) -> dict[str, Any]:
        return dict(self._last_screenshot_metadata)


def _build_utility_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Transform the YAML spec target/actions/assertions into the shape expected by WarptestCheckpointCommand.cs."""
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    actions = spec.get("actions") if isinstance(spec.get("actions"), list) else []
    assertions = spec.get("assertions") if isinstance(spec.get("assertions"), list) else []

    validations: list[dict[str, str]] = []
    if "validations" in target and isinstance(target["validations"], list):
        validations = target["validations"]

    return {
        "target": target,
        "validations": validations,
        "actions": actions,
        "assertions": assertions,
    }


def _needs_visual_capture(spec: dict[str, Any]) -> bool:
    """Return True if the spec declares a visual_oracle requiring screenshot capture."""
    oracle = spec.get("visual_oracle")
    return isinstance(oracle, dict) and bool(oracle.get("golden_image"))


def _live_fallback_image(spec: dict[str, Any], spec_path: Path) -> Path | None:
    oracle = spec.get("visual_oracle")
    if not isinstance(oracle, dict):
        return None
    value = oracle.get("live_fallback_image")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(spec_path).expanduser().resolve().parent / path
    path = path.resolve()
    if path.exists() and path.is_file() and path.stat().st_size > 0:
        return path
    return None


def _load_utility_report(report_path: Path) -> dict[str, Any]:
    if not report_path.exists():
        return {
            "status": "failure",
            "detail": "Jynew WarpTest command did not write a report.",
        }
    try:
        # Unity JsonUtility may emit a UTF-8 BOM; utf-8-sig strips it.
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except Exception as error:
        return {
            "status": "failure",
            "detail": f"Unable to parse jynew report JSON: {error}",
        }
    if not isinstance(report, dict):
        return {
            "status": "failure",
            "detail": "Jynew WarpTest report JSON is not an object.",
        }
    return report


def _validate_timeout_success_report(
    utility_report: dict[str, Any],
    *,
    request: dict[str, Any],
    report_path: Path,
    out_dir: Path,
    screenshot_output: str | None,
) -> tuple[bool, str]:
    """Accept only a fresh, complete outcome written before Unity hung on exit."""

    if not report_path.is_file():
        return False, "report file is missing"
    if utility_report.get("status") != "success":
        return False, "report status is not success"

    checks = utility_report.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, dict) or check.get("status") != "success"
            for check in checks
        )
    ):
        return False, "report checks are missing, malformed, or not all successful"

    for field in (
        "evidence_task_id",
        "evidence_seed",
        "evidence_stage",
        "evidence_benchmark",
    ):
        if utility_report.get(field) != request.get(field):
            return False, f"report {field} does not match the current request"

    if screenshot_output:
        requested = Path(screenshot_output).expanduser().resolve()
        reported_value = utility_report.get("screenshot_path")
        if not isinstance(reported_value, str) or not reported_value:
            return False, "report screenshot path is missing"
        reported = Path(reported_value).expanduser().resolve()
        try:
            reported.relative_to(out_dir.resolve())
        except ValueError:
            return False, "report screenshot is outside the current output directory"
        if reported != requested:
            return False, "report screenshot does not match the current request"
        if (
            utility_report.get("screenshot_status") != "success"
            or utility_report.get("screenshot_source") != "unity_capture"
            or not _screenshot_is_informative(reported)
        ):
            return False, "report does not contain an informative Unity capture"

    return True, "fresh success report matches the current request"


def _capture_metadata(utility_report: dict[str, Any]) -> dict[str, Any]:
    status = str(utility_report.get("screenshot_status") or "skipped")
    source = str(utility_report.get("screenshot_source") or "")
    detail = str(utility_report.get("screenshot_detail") or "")
    live_source = "unity_capture" if status == "success" and source == "unity_capture" else "capture_failure"
    return {
        "screenshot_status": status,
        "screenshot_source": source,
        "screenshot_detail": detail,
        "screenshot_path": utility_report.get("screenshot_path"),
        "live_source": live_source,
    }


def _unity_screenshot_path(utility_report: dict[str, Any], requested_output: str) -> Path | None:
    candidates = [utility_report.get("screenshot_path"), requested_output]
    for value in candidates:
        if not isinstance(value, str) or not value:
            continue
        path = Path(value).expanduser()
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            return path.resolve()
    return None


def _screenshot_is_informative(path: Path) -> bool:
    try:
        from PIL import Image

        data = path.read_bytes()
        if len(data) < 2_000 or len(set(data)) < 32:
            return False
        with Image.open(path) as image:
            extrema = image.convert("RGB").getextrema()
        return max(high - low for low, high in extrema) >= 8
    except Exception:
        return False


def find_unity_executable() -> str:
    """Public wrapper for locating the Unity editor executable.

    Shared by the cold-per-request adapter above and the C3 warm session in
    :mod:`warptest.jynew_fuzz`.
    """
    return _find_unity_executable()


def _jynew_project_editor_version(project_path: Path) -> str | None:
    version_file = project_path / "ProjectSettings" / "ProjectVersion.txt"
    if not version_file.is_file():
        return None
    for line in version_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("m_EditorVersion:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


@lru_cache(maxsize=16)
def _unity_editor_version(unity_exe: str) -> str | None:
    try:
        process = subprocess.Popen(
            [unity_exe, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output, _ = process.communicate(timeout=30)
        if process.returncode not in {0, None}:
            return None
    except Exception:
        return None
    match = UNITY_VERSION_RE.search(output)
    return match.group(0) if match else None


def _jynew_unity_candidates_for_version(version: str) -> list[str]:
    return [
        f"/Applications/Unity/Unity-{version}/Unity.app/Contents/MacOS/Unity",
        f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity",
        f"/Applications/Unity Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity",
        "/Applications/Unity/Unity.app/Contents/MacOS/Unity",
    ]


def _find_jynew_unity_executable(project_path: Path) -> str:
    """Select only an editor matching Jynew's pinned ProjectVersion.

    Jynew currently pins the China release ``2020.3.32f1c1``.  Falling back to
    a newer Pal3/Hub editor would silently migrate or corrupt the benchmark, so
    every explicit and discovered candidate is version-checked before launch.
    """

    required = _jynew_project_editor_version(project_path)
    explicit = os.environ.get("UNITY_EDITOR_PATH")
    candidates = [explicit] if explicit else []
    if required:
        candidates.extend(_jynew_unity_candidates_for_version(required))
    else:
        candidates.append(_find_unity_executable())
    checked: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in checked or not Path(candidate).exists():
            continue
        checked.append(candidate)
        observed = _unity_editor_version(candidate)
        if required is None or observed == required:
            return candidate
    if required:
        details = ", ".join(
            f"{candidate}={_unity_editor_version(candidate) or 'unknown'}"
            for candidate in checked
        ) or "no installed candidates"
        raise RuntimeError(
            "No Unity editor matching Jynew ProjectVersion is available: "
            f"required {required}; checked {details}. Install/activate that exact "
            "editor or set UNITY_EDITOR_PATH to a matching executable."
        )
    return _find_unity_executable()


def _find_unity_executable() -> str:
    """Locate the Unity editor executable for the current platform."""
    env_path = os.environ.get("UNITY_EDITOR_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    import glob
    import platform as pf
    system = pf.system()
    if system == "Darwin":
        candidates = [
            "/Applications/Unity/Unity.app/Contents/MacOS/Unity",
            "/Applications/Unity/Hub/Editor/*/Unity.app/Contents/MacOS/Unity",
            "/Applications/Unity Hub/Editor/*/Unity.app/Contents/MacOS/Unity",
        ]
        for pattern in candidates:
            if "*" in pattern:
                matches = sorted(glob.glob(pattern), reverse=True)
                if matches:
                    return matches[0]
            elif Path(pattern).exists():
                return pattern
        return "Unity"
    if system == "Linux":
        return os.environ.get("UNITY_PATH", "unity-editor")
    return "Unity.exe"


def _process_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""
