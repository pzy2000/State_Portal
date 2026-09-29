from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .runtime import json_dumps
from .unity_process import cleanup_detached_unity_hub_processes
from .jynew_runtime import (
    _NoOpPage,
    _capture_metadata,
    _find_unity_executable,
    _live_fallback_image,
    _process_output,
    _screenshot_is_informative,
    _unity_screenshot_path,
)


# Pal3.Unity keeps the Unity project at the repository root (Assets/ lives there),
# unlike jynew which nests its project under jyx2/.
PAL3_HOOK_RELATIVE_PATH = Path("Assets/Scripts/Pal3.Game/Command/WarptestCheckpointCommand.cs")
PAL3_HOOK_TEMPLATE_PATH = Path(__file__).resolve().parent / "instrumentation" / "pal3" / "WarptestCheckpointCommand.cs"
UNITY_VERSION_RE = re.compile(r"\b\d{4}\.\d+\.\d+[A-Za-z0-9.]*\b")


def find_pal3_project_path(repo: Path) -> Path:
    """Locate Pal3.Unity when callers receive either the child or parent repo."""

    repo = Path(repo).expanduser().resolve()
    for candidate in (
        repo,
        repo / "games" / "Pal3.Unity",
        repo / "Pal3.Unity",
    ):
        if (candidate / "Assets").is_dir():
            return candidate
    return repo


def ensure_pal3_instrumentation(repo: Path) -> dict[str, object]:
    """Install the bundled Unity batch-mode hook when an upstream Pal3.Unity checkout lacks it."""
    project_path = find_pal3_project_path(repo)
    target = project_path / PAL3_HOOK_RELATIVE_PATH
    source = PAL3_HOOK_TEMPLATE_PATH
    if not source.exists():
        raise FileNotFoundError(f"Bundled pal3 instrumentation template is missing: {source}")

    if target.exists():
        text = target.read_text(encoding="utf-8", errors="ignore")
        if "WarptestCheckpoint" in text and "Pal3.Game.Command" in text:
            if (
                "WarptestRunnerBehaviour" not in text
                or "screenshot_output_path" not in text
                or "RunC1" not in text
                or "warptest-c1-unity-v3" not in text
                or "transition_evidence" not in text
                or "warptest-unity-checkpoint-state-v1" not in text
                or "evidence_task_id" not in text
                or "evidence_benchmark" not in text
                # Visual-pair capture needs the settle wait and the overlay toggles;
                # an older hook without them silently drops those actions.
                or "pal3_toggle_world_map" not in text
                # Party membership is only visible once the followers are spawned.
                or "pal3_team_open" not in text
                # Without the pinned rendering resolution the HiDPI GameView
                # readback keeps only the bottom-left quarter of every frame.
                or "PinGameViewRenderingResolution" not in text
                # Oracle Start parity requires the assertion to retain the
                # registered target's >= comparison even when the threshold is 0.
                or "bool ok = flag >= assertion.int_value;" not in text
            ):
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                return {
                    "installed": True,
                    "reason": (
                        "refreshed stale pal3 WarpTest hook with informative screenshot, "
                        "C1 background GameView IPC v3, visual-pair overlay actions, "
                        "team-open capture, full-frame GameView capture, and "
                        "world-region threshold parity"
                    ),
                    "path": str(target),
                    "source": str(source),
                }
            return {
                "installed": False,
                "reason": "existing pal3 WarpTest hook present",
                "path": str(target),
                "source": str(source),
            }
        raise RuntimeError(f"Refusing to overwrite non-WarpTest pal3 file: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "installed": True,
        "reason": "installed bundled pal3 WarpTest hook",
        "path": str(target),
        "source": str(source),
    }


def _pal3_project_editor_version(project_path: Path) -> str | None:
    version_file = project_path / "ProjectSettings" / "ProjectVersion.txt"
    if not version_file.exists():
        return None
    for line in version_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("m_EditorVersion:"):
            version = line.split(":", 1)[1].strip()
            return version or None
    return None


def _pal3_unity_editor_version(unity_exe: str) -> str | None:
    try:
        completed = subprocess.run(
            [unity_exe, "-version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    output = f"{completed.stdout}\n{completed.stderr}"
    match = UNITY_VERSION_RE.search(output)
    return match.group(0) if match else None


def _pal3_unity_candidates_for_version(version: str) -> list[str]:
    return [
        f"/Applications/Unity/Unity-{version}/Unity.app/Contents/MacOS/Unity",
        f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity",
        f"/Applications/Unity Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity",
    ]


def _find_pal3_unity_executable(project_path: Path) -> str:
    env_path = os.environ.get("UNITY_EDITOR_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    project_version = _pal3_project_editor_version(project_path)
    if project_version:
        for candidate in _pal3_unity_candidates_for_version(project_version):
            if Path(candidate).exists():
                return candidate
    return _find_unity_executable()


class Pal3RuntimeAdapter:
    """Run Pal3.Unity checkpoint specs through the Unity batch-mode WarpTest command."""

    def __init__(self, repo: Path, out_dir: Path, *, headless: bool = True, visual_capture: bool = False) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.out_dir = Path(out_dir).expanduser().resolve()
        self._visual_capture = visual_capture
        self.headless = headless
        batch_flags = "-batchmode -nographics " if headless else ""
        self.launch_command = (
            f"unity {batch_flags}-projectPath {{project}} "
            "-executeMethod Pal3.Game.Command.WarptestCheckpoint.Run "
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
        self._last_screenshot_path: str | None = None
        self._last_screenshot_metadata: dict[str, Any] = {
            "screenshot_status": "skipped",
            "screenshot_source": "",
            "screenshot_detail": "",
            "live_source": "capture_failure",
        }

    def __enter__(self) -> "Pal3RuntimeAdapter":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        install = ensure_pal3_instrumentation(self.repo)
        self.logs.append(f"pal3_instrumentation={install['reason']}:{install['path']}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def _find_project_path(self) -> Path:
        """Locate the Unity project root. Pal3.Unity keeps Assets/ at the repo root."""
        return find_pal3_project_path(self.repo)

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
        self.out_dir.mkdir(parents=True, exist_ok=True)
        request_path = self.out_dir / f"{Path(spec_path).stem}-pal3-request.json"
        report_path = self.out_dir / f"{Path(spec_path).stem}-pal3-result.json"
        stdout_path = self.out_dir / f"{Path(spec_path).stem}-pal3-stdout.log"
        stderr_path = self.out_dir / f"{Path(spec_path).stem}-pal3-stderr.log"

        # A spec may declare visual_oracle metadata even in semantic-only runs.
        # Only request a Unity screenshot when the runner's oracle policy will
        # actually consume it; otherwise cold Unity imports can be cut short by
        # the shorter visual timeout cap.
        screenshot_output: str | None = None
        if self._visual_capture:
            screenshot_output = str(self.out_dir / "pal3-visual-post-smoke.png")
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
            "spec": _build_pal3_utility_spec(spec),
        }
        request_path.write_text(json_dumps(request), encoding="utf-8")

        unity_exe = _find_pal3_unity_executable(self._project_path)
        self.logs.append(f"unity_executable={unity_exe}")
        project_version = _pal3_project_editor_version(self._project_path)
        editor_version = _pal3_unity_editor_version(unity_exe)
        if project_version and editor_version and project_version != editor_version:
            detail = (
                "Unity editor version mismatch for Pal3.Unity: "
                f"project requires {project_version}, but {unity_exe} is {editor_version}. "
                "Install/select the matching Unity editor or set UNITY_EDITOR_PATH."
            )
            return self._failure_payload(
                request_path, report_path, stdout_path, stderr_path, detail, "probe.unity_version_mismatch",
            )
        cmd = [unity_exe]
        if self.headless:
            cmd.extend(["-batchmode", "-nographics"])
        cmd.extend([
            "-projectPath", str(self._project_path),
            "-executeMethod", "Pal3.Game.Command.WarptestCheckpoint.Run",
            "-logFile", str(self.out_dir / "unity-editor.log"),
            "--",
            "--warptest-request", str(request_path),
            "--warptest-report", str(report_path),
        ])

        timeout_seconds = int(spec.get("timeout_ms", 120_000) or 120_000) / 1000
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
            return self._failure_payload(
                request_path, report_path, stdout_path, stderr_path, detail, "probe.unity_not_found",
            )
        except OSError as error:
            detail = f"OS error launching Unity ({unity_exe}): {error}"
            return self._failure_payload(
                request_path, report_path, stdout_path, stderr_path, detail, "probe.os_error",
            )
        except subprocess.TimeoutExpired as error:
            stdout_path.write_text(_process_output(error.stdout), encoding="utf-8")
            stderr_path.write_text(_process_output(error.stderr), encoding="utf-8")
            self.logs.extend([str(stdout_path), str(stderr_path)])
            return {
                "status": "failure",
                "exit_code": None,
                "request_path": str(request_path),
                "report_path": str(report_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "stdout_tail": _process_output(error.stdout)[-4000:],
                "stderr_tail": _process_output(error.stderr)[-4000:],
                "utility_report": {"status": "failure", "detail": f"Unity batch mode timed out after {error.timeout} seconds"},
                "checks": [{"name": "probe.timeout", "status": "failure", "detail": f"Timed out after {error.timeout}s"}],
            }

        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        self.logs.extend([str(stdout_path), str(stderr_path)])

        unity_log = self.out_dir / "unity-editor.log"
        if unity_log.exists():
            self.logs.append(str(unity_log))

        utility_report: dict[str, Any] = {}
        if report_path.exists():
            try:
                # Unity JsonUtility may emit a UTF-8 BOM; utf-8-sig strips it
                utility_report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            except Exception as error:
                utility_report = {"status": "failure", "detail": f"Unable to parse pal3 report JSON: {error}"}
        else:
            utility_report = {"status": "failure", "detail": "Pal3 WarpTest command did not write a report."}

        status = "success" if completed.returncode == 0 and utility_report.get("status") == "success" else "failure"

        self._last_screenshot_path = None
        self._last_screenshot_metadata = _capture_metadata(utility_report)
        if screenshot_output:
            self._last_screenshot_metadata.setdefault("requested_screenshot_path", screenshot_output)
            candidate = _unity_screenshot_path(utility_report, screenshot_output)
            if (
                self._last_screenshot_metadata.get("screenshot_status") == "success"
                and self._last_screenshot_metadata.get("screenshot_source") == "unity_capture"
                and candidate is not None
                and _screenshot_is_informative(candidate)
            ):
                self._last_screenshot_path = str(candidate)
                self._last_screenshot_metadata["live_source"] = "unity_capture"
            else:
                self._last_screenshot_metadata["live_source"] = "capture_failure"
                detail = self._last_screenshot_metadata.get("screenshot_detail") or "Unity did not report an informative capture."
                self.logs.append(f"pal3_visual_capture_failure={detail}")
                fallback = _live_fallback_image(spec, spec_path)
                if fallback is not None:
                    diagnostic = Path(screenshot_output).with_name("pal3-visual-fallback.png")
                    shutil.copy2(fallback, diagnostic)
                    self._last_screenshot_metadata["fallback_image"] = str(diagnostic)
                    self.logs.append(f"pal3_visual_fallback_diagnostic={fallback}")

        return {
            "status": status,
            "exit_code": completed.returncode,
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

    def _failure_payload(
        self,
        request_path: Path,
        report_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        detail: str,
        check_name: str,
    ) -> dict[str, Any]:
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
            "checks": [{"name": check_name, "status": "failure", "detail": detail}],
        }

    def screenshot(self, name: str = "pal3-screenshot.png", full_page: bool = False) -> str:
        if self._last_screenshot_path and Path(self._last_screenshot_path).exists():
            target = self.out_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(target) != self._last_screenshot_path:
                shutil.copy2(self._last_screenshot_path, target)
            return str(target)
        target = self.out_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        detail = self._last_screenshot_metadata.get("screenshot_detail") or "Unity did not produce an informative image."
        raise RuntimeError(f"Pal3 capture failure: {detail}")

    @property
    def last_screenshot_metadata(self) -> dict[str, Any]:
        return dict(self._last_screenshot_metadata)


def _build_pal3_utility_spec(spec: dict[str, Any]) -> dict[str, Any]:
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
