"""Persistent native desktop runtimes for the C1 Agent-S arms.

The normal Blender and LibreOffice adapters are deliberately batch-oriented: they
restore a checkpoint, execute its scripted transition, grade it, and exit.  That is
the wrong lifecycle for Agent-S.  This module owns a *clean*, isolated application
process and exposes only the primitives the C1 orchestrator needs:

* start from the public application entry surface;
* optionally restore only a synthesized ``target`` (never ``actions``);
* keep the real GUI alive while it remains fully obscured and non-frontmost;
* capture application-owned UI pixels and inject only application-local events; and
* run hidden, read-only semantic probes against the live application state.

The policy never receives the semantic probe payload or result.  Unsupported target
or assertion shapes fail closed rather than silently falling back to scripted task
execution.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from PIL import Image

from .blender_runtime import find_blender_executable
from .c1_background_window_guard import MacOSBackgroundWindowGuard
from .c1_evaluation import (
    evaluation_contract,
    semantic_assertions,
    surface_contract,
    visual_oracle,
)
from .policy_frame import copy_fixed_policy_frame


class C1DesktopRuntimeError(RuntimeError):
    """A persistent desktop runtime could not satisfy its fail-closed contract."""


class C1DesktopRuntimeUnsupported(C1DesktopRuntimeError):
    """The requested target/assertion surface is not implemented honestly."""


BLENDER_SEMANTIC_SOURCE = "blender_live_bpy_probe_v1"
LIBREOFFICE_SEMANTIC_SOURCE = "libreoffice_live_uno_probe_v1"
BLENDER_SEED_EVIDENCE_SOURCE = "blender_native_desktop_session_v1"
LIBREOFFICE_SEED_EVIDENCE_SOURCE = "libreoffice_native_desktop_session_v1"
NATIVE_GUI_SEED_ALGORITHM = "not_applicable_native_gui"
_BLENDER_SUPPORTED_STATE_ACTIONS = {
    "blender_add_primitive",
    "blender_assign_material",
    "blender_set_object_location",
    "blender_set_object_scale",
    "blender_set_object_rotation",
    "blender_set_frame",
    "blender_add_modifier",
    "blender_apply_modifier",
    "blender_set_keyframe",
    "blender_save_as",
    "blender_reopen",
}


def semantic_probe_passed(evidence: Any, *, expected_source: str) -> bool:
    """Return true only for non-empty, live, boolean check evidence."""

    if not isinstance(evidence, dict):
        return False
    checks = evidence.get("checks")
    return bool(
        evidence.get("status") == "success"
        and evidence.get("source") == expected_source
        and evidence.get("live") is True
        and isinstance(checks, list)
        and checks
        and all(isinstance(row, dict) and row.get("ok") is True for row in checks)
    )


def _git_commit(repo: Any) -> str | None:
    if not isinstance(repo, Path) or not repo.is_dir():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and len(value) == 40 else None


def _native_seed_evidence(
    session: Any,
    experiment_seed: int,
    semantic_probe: dict[str, Any],
    task_id: str,
    stage: str,
) -> dict[str, Any]:
    """Bind a trial seed to a real native process and live semantic observation.

    Blender and LibreOffice do not expose a seeded application PRNG.  ``seed`` is
    therefore the C1 trial-pairing seed, while ``algorithm`` explicitly records that
    native GUI randomness is not applicable.  No requested/default application state
    is promoted into evidence: the semantic mapping must come from the registered live
    probe source and contain concrete boolean checks.
    """

    if not isinstance(semantic_probe, dict) or not semantic_probe:
        raise C1DesktopRuntimeError(
            "native seed evidence requires a non-empty live semantic probe mapping"
        )
    normalized_task_id = str(task_id).strip()
    normalized_stage = str(stage).strip()
    if not normalized_task_id or not normalized_stage:
        raise C1DesktopRuntimeError(
            "native seed evidence requires non-empty task_id and stage"
        )
    expected_semantic_source = str(getattr(session, "semantic_source", ""))
    checks = semantic_probe.get("checks")
    semantic_observed = bool(
        semantic_probe.get("source") == expected_semantic_source
        and semantic_probe.get("live") is True
        and isinstance(checks, list)
        and checks
        and all(
            isinstance(check, dict) and type(check.get("ok")) is bool
            for check in checks
        )
    )
    if not semantic_observed:
        raise C1DesktopRuntimeError(
            "native seed evidence semantic probe is not registered live check evidence"
        )

    observation = copy.deepcopy(semantic_probe)
    canonical = json.dumps(
        observation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    semantic_fingerprint = hashlib.sha256(canonical).hexdigest()
    process = getattr(session, "process", None)
    process_id = getattr(process, "pid", None)
    try:
        process_alive = bool(process is not None and process.poll() is None)
    except Exception:
        process_alive = False

    executable = getattr(session, "executable", None)
    if executable is None:
        executable = getattr(session, "soffice", None)
    executable_path = Path(executable).resolve() if executable is not None else None
    executable_stat: os.stat_result | None = None
    if executable_path is not None:
        try:
            executable_stat = executable_path.stat()
        except OSError:
            executable_stat = None

    return {
        "schema_version": 1,
        "source": str(getattr(session, "seed_evidence_source", "")),
        "observed": bool(process_alive and semantic_observed),
        "algorithm": NATIVE_GUI_SEED_ALGORITHM,
        "seed": int(experiment_seed),
        "benchmark": str(getattr(session, "benchmark", "")),
        "process_id": int(process_id) if isinstance(process_id, int) else None,
        "process_alive_at_observation": process_alive,
        "task_id": normalized_task_id,
        "semantic_stage": normalized_stage,
        "semantic_source": expected_semantic_source,
        "semantic_observed": semantic_observed,
        "semantic_fingerprint": semantic_fingerprint,
        "semantic_observation": observation,
        # Compatibility aliases match the existing Luanti initial-state evidence.
        "initial_semantic_stage": normalized_stage,
        "initial_semantic_source": expected_semantic_source,
        "initial_semantic_fingerprint": semantic_fingerprint,
        "initial_semantic_observation": observation,
        "randomness": "not_applicable",
        "seed_role": "trial_pairing_only",
        "execution_backend": str(
            getattr(session, "execution_backend", "background_pixel_v1")
        ),
        "foreground_required": bool(
            getattr(session, "foreground_required", False)
        ),
        "coordinate_space": str(
            getattr(session, "coordinate_space", "frame_pixels_top_left_v1")
        ),
        "capture_backend": str(
            getattr(session, "capture_backend", "application_framebuffer")
        ),
        "input_backend": str(
            getattr(session, "input_backend", "application_event_queue")
        ),
        "repo_commit": _git_commit(getattr(session, "repo", None)),
        "executable_path": str(executable_path)
        if executable_path is not None
        else None,
        "executable_size": executable_stat.st_size
        if executable_stat is not None
        else None,
        "executable_mtime_ns": (
            executable_stat.st_mtime_ns if executable_stat is not None else None
        ),
    }


def _target_only(value: dict[str, Any]) -> dict[str, Any]:
    raw = value.get("target") if isinstance(value.get("target"), dict) else value
    if not isinstance(raw, dict):
        raise C1DesktopRuntimeUnsupported("desktop checkpoint target must be a mapping")
    return copy.deepcopy(raw)


def _assertions_only(
    value: dict[str, Any] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw: Any = semantic_assertions(value) if isinstance(value, dict) else value
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise C1DesktopRuntimeUnsupported(
            "desktop goal assertions must be a list of mappings"
        )
    return copy.deepcopy(raw)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _tail(path: Path, *, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _stop_process(
    process: subprocess.Popen[Any] | None, *, timeout: float = 8.0
) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        # The caller can inspect ``process.poll`` and report the leak.  Never claim
        # successful cleanup merely because SIGKILL was requested.
        return


class BlenderC1DesktopSession:
    """One persistent, visible Blender factory-startup session."""

    benchmark = "Blender"
    semantic_source = BLENDER_SEMANTIC_SOURCE
    seed_evidence_source = BLENDER_SEED_EVIDENCE_SOURCE
    execution_backend = "background_pixel_v1"
    foreground_required = False
    coordinate_space = "frame_pixels_top_left_v1"
    capture_backend = "blender_bpy_screen_screenshot_v1"
    input_backend = "blender_window_event_simulate_v1"

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        executable: str | Path | None = None,
        command_timeout_s: float = 30.0,
        startup_timeout_s: float = 60.0,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.command_timeout_s = float(command_timeout_s)
        self.startup_timeout_s = float(startup_timeout_s)
        if executable is None:
            self.executable = find_blender_executable(self.repo)
        else:
            candidate = Path(executable).expanduser().resolve()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise C1DesktopRuntimeError(
                    f"registered Blender executable is unavailable: {candidate}"
                )
            self.executable = candidate
        self.control_dir = self.report_dir / "blender-c1-control"
        self.commands_dir = self.control_dir / "commands"
        self.responses_dir = self.control_dir / "responses"
        self.artifact_dir = self.report_dir / "blender-c1-artifacts"
        self.stdout_path = self.report_dir / "blender-c1-stdout.log"
        self.stderr_path = self.report_dir / "blender-c1-stderr.log"
        self.process: subprocess.Popen[Any] | None = None
        self.page = None
        self.session_id = uuid.uuid4().hex
        self._sequence = 0
        self.last_policy_frame_id: int | None = None
        self.last_policy_frame_capture: dict[str, Any] | None = None
        self._policy_window_mapping: dict[str, int] | None = None
        self.width = 1280
        self.height = 800
        self._stdout_handle: Any | None = None
        self._stderr_handle: Any | None = None
        self._background_window_guard = MacOSBackgroundWindowGuard(self.report_dir)

    @property
    def pid(self) -> int | None:
        return int(self.process.pid) if self.process is not None else None

    def __enter__(self) -> "BlenderC1DesktopSession":
        return self.start_clean()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def start_clean(self) -> "BlenderC1DesktopSession":
        if self.process is not None and self.process.poll() is None:
            return self
        if sys.platform != "darwin":
            raise C1DesktopRuntimeUnsupported(
                "formal native Agent-S desktop sessions currently require macOS"
            )
        for path in (
            self.commands_dir,
            self.responses_dir,
            self.artifact_dir,
            self.report_dir / "blender-user" / "config",
            self.report_dir / "blender-user" / "data",
            self.report_dir / "blender-user" / "scripts",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.control_dir / "ready.json").unlink(missing_ok=True)
        bridge = Path(__file__).with_name("blender_c1_bridge.py")
        if not bridge.is_file():
            raise C1DesktopRuntimeError(f"Blender C1 bridge is missing: {bridge}")
        env = os.environ.copy()
        env.update(
            {
                "BLENDER_USER_CONFIG": str(self.report_dir / "blender-user" / "config"),
                "BLENDER_USER_DATAFILES": str(
                    self.report_dir / "blender-user" / "data"
                ),
                "BLENDER_USER_SCRIPTS": str(
                    self.report_dir / "blender-user" / "scripts"
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = [
            str(self.executable),
            "--factory-startup",
            "--enable-event-simulate",
            "--no-window-focus",
            "--window-geometry",
            "0",
            "0",
            str(self.width),
            str(self.height),
            "--python",
            str(bridge),
            "--",
            str(self.control_dir),
            self.session_id,
        ]
        try:
            self._background_window_guard.prepare()
            self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
            self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
            self.process = subprocess.Popen(
                command,
                cwd=str(self.repo),
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
                env=env,
                start_new_session=True,
            )
            self._background_window_guard.start(self.process.pid)
            ready = self._wait_for_json(
                self.control_dir / "ready.json", timeout_s=self.startup_timeout_s
            )
            if ready.get("status") != "ready":
                raise C1DesktopRuntimeError(
                    f"Blender bridge did not become ready: {ready}"
                )
            if ready.get("startup_splash_suppressed") is not True:
                raise C1DesktopRuntimeError(
                    "Blender bridge did not prove startup splash suppression: "
                    f"{ready}"
                )
            pong = self._command("ping")
            if pong.get("status") != "success":
                raise C1DesktopRuntimeError(f"Blender bridge ping failed: {pong}")
            self._background_window_guard.await_hidden()
            return self
        except BaseException:
            self.close()
            raise

    def _wait_for_json(self, path: Path, *, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise C1DesktopRuntimeError(
                    f"Blender exited before bridge readiness (exit={self.process.returncode}). "
                    f"stderr: {_tail(self.stderr_path)}"
                )
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        return loaded
                except (OSError, json.JSONDecodeError) as error:
                    last_error = error
            time.sleep(0.05)
        raise C1DesktopRuntimeError(
            f"timed out waiting for Blender bridge artifact {path}: {last_error or 'missing'}"
        )

    def _command(self, command: str, **payload: Any) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise C1DesktopRuntimeError("Blender desktop session is not running")
        request_id = uuid.uuid4().hex
        self._sequence += 1
        request_path = self.commands_dir / f"{request_id}.json"
        response_path = self.responses_dir / f"{request_id}.json"
        _atomic_json(
            request_path,
            {
                "schema_version": 2,
                "request_id": request_id,
                "session_id": self.session_id,
                "sequence": self._sequence,
                "command": command,
                **payload,
            },
        )
        response = self._wait_for_json(response_path, timeout_s=self.command_timeout_s)
        if response.get("request_id") != request_id:
            raise C1DesktopRuntimeError("Blender bridge response/request id mismatch")
        return response

    def restore_target(self, spec_or_target: dict[str, Any]) -> dict[str, Any]:
        """Restore only the synthesized target and leave the live GUI at that start."""

        target = _target_only(spec_or_target)
        if target.get("kind") not in {"scene", "blend_file"}:
            raise C1DesktopRuntimeUnsupported(
                f"unsupported Blender C1 target kind: {target.get('kind')!r}"
            )
        if not isinstance(target.get("objects"), list):
            raise C1DesktopRuntimeUnsupported(
                "Blender C1 target.objects must be a list"
            )
        result = self._command(
            "restore_target",
            target=target,
            artifact_dir=str(self.artifact_dir),
        )
        if result.get("status") not in {"success", "failure"}:
            raise C1DesktopRuntimeError(
                f"Blender target-only restore returned an invalid status: {result}"
            )
        return result

    def probe_start(self, canonical_spec_or_target: dict[str, Any]) -> dict[str, Any]:
        target = _target_only(canonical_spec_or_target)
        evidence = self._command("probe_target", target=target)
        evidence.setdefault("source", self.semantic_source)
        evidence.setdefault("live", True)
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        return evidence

    def probe_liveness(self) -> dict[str, Any]:
        """Return registered live-process evidence without requiring a document."""

        evidence = self._command("ping")
        evidence.setdefault("source", self.semantic_source)
        evidence.setdefault("live", True)
        evidence.setdefault("errors", [])
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        return evidence

    def probe_goal(
        self, canonical_spec_or_assertions: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        assertions = _assertions_only(canonical_spec_or_assertions)
        evidence = self._command("probe_assertions", assertions=assertions)
        evidence.setdefault("source", self.semantic_source)
        evidence.setdefault("live", True)
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        return evidence

    def execute_state_transition(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Execute only the registered programmatic Blender State transition.

        Agent-S C/D paths call only the read-only ``probe_goal`` surface above.
        This explicit arm-E method keeps mutation authority separate and returns
        a final semantic observation from the still-live Blender process.
        """

        raw_actions = spec.get("actions")
        if not isinstance(raw_actions, list) or not raw_actions or not all(
            isinstance(action, dict) for action in raw_actions
        ):
            raise C1DesktopRuntimeUnsupported(
                "Blender state transition requires a non-empty mapping action list"
            )
        unsupported = [
            action.get("type")
            for action in raw_actions
            if action.get("type") not in _BLENDER_SUPPORTED_STATE_ACTIONS
        ]
        if unsupported:
            raise C1DesktopRuntimeUnsupported(
                f"unsupported Blender C1 state actions: {unsupported!r}"
            )
        action_evidence = self._command(
            "apply_state_actions",
            actions=copy.deepcopy(raw_actions),
            artifact_dir=str(self.artifact_dir),
        )
        action_evidence.setdefault("source", self.semantic_source)
        action_evidence.setdefault("live", True)
        action_evidence.setdefault("errors", [])
        action_evidence["passed"] = semantic_probe_passed(
            action_evidence, expected_source=self.semantic_source
        )
        action_evidence["state_action_checks"] = copy.deepcopy(
            action_evidence.get("checks", [])
        )
        action_evidence[
            "state_transition_backend"
        ] = "blender_live_bpy_state_transition_v1"
        action_evidence["state_transition_stage"] = "actions"
        if not action_evidence["passed"]:
            return action_evidence
        goal_evidence = self.probe_goal(spec)
        goal_evidence["state_action_checks"] = copy.deepcopy(
            action_evidence["checks"]
        )
        goal_evidence[
            "state_transition_backend"
        ] = "blender_live_bpy_state_transition_v1"
        goal_evidence["state_transition_stage"] = "goal"
        if not goal_evidence.get("passed"):
            return goal_evidence
        return goal_evidence

    def seed_evidence(
        self,
        experiment_seed: int,
        semantic_probe: dict[str, Any],
        task_id: str,
        stage: str,
    ) -> dict[str, Any]:
        return _native_seed_evidence(
            self, experiment_seed, semantic_probe, task_id, stage
        )

    def render_proof(self, path: str | Path, *, canonical_spec: dict[str, Any]) -> str:
        """Render a non-mutating grader image from the same live scene.

        Agent-S still sees/acts on full-desktop screenshots.  This separate proof
        image exists because the registered Blender goldens are deterministic scene
        renders with axes/grid labels, not screenshots of the Blender chrome.
        """

        destination = Path(path).expanduser().resolve()
        result = self._command(
            "render_proof", path=str(destination), spec=copy.deepcopy(canonical_spec)
        )
        if result.get("status") != "success" or not destination.is_file():
            raise C1DesktopRuntimeError(f"Blender live proof render failed: {result}")
        return str(destination)

    def screenshot(self, path: str | Path, full_page: bool = False) -> str:
        del full_page
        return self.capture_policy_frame(path)

    def policy_frame_dimensions(self) -> tuple[int, int]:
        return self.width, self.height

    def capture_policy_frame(self, path: str | Path) -> str:
        destination = Path(path).expanduser().resolve()
        result = self._command("capture_ui")
        source_raw = result.get("path")
        if result.get("status") != "success" or not isinstance(source_raw, str):
            raise C1DesktopRuntimeError(f"Blender framebuffer capture failed: {result}")
        source = Path(source_raw).resolve()
        frames_root = (self.control_dir / "frames").resolve()
        if source.parent != frames_root or not source.is_file():
            raise C1DesktopRuntimeError("Blender bridge returned an untrusted frame path")
        width = result.get("width")
        height = result.get("height")
        frame_id = result.get("frame_id")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or isinstance(height, bool)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
            or width > self.width
            or height > self.height
        ):
            raise C1DesktopRuntimeError(
                f"Blender policy window drifted to unsupported {width}x{height}; "
                f"maximum is {self.width}x{self.height}"
            )
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
            raise C1DesktopRuntimeError("Blender bridge returned an invalid frame id")
        offset_x = (self.width - width) // 2
        offset_y = (self.height - height) // 2
        if (width, height) == (self.width, self.height):
            self.last_policy_frame_capture = copy_fixed_policy_frame(
                source,
                destination,
                width=self.width,
                height=self.height,
                backend=self.capture_backend,
                error_type=C1DesktopRuntimeError,
            )
        else:
            logical = destination.with_name(
                f".{destination.stem}.logical-window{destination.suffix}"
            )
            try:
                source_capture = copy_fixed_policy_frame(
                    source,
                    logical,
                    width=width,
                    height=height,
                    backend=self.capture_backend,
                    error_type=C1DesktopRuntimeError,
                )
                with Image.open(logical) as window_image:
                    window_image.load()
                    canvas = Image.new("RGB", (self.width, self.height), (0, 0, 0))
                    canvas.paste(window_image.convert("RGB"), (offset_x, offset_y))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    canvas.save(destination, format="PNG")
                self.last_policy_frame_capture = {
                    **source_capture,
                    "application_window": [width, height],
                    "policy_frame": [self.width, self.height],
                    "policy_content_offset": [offset_x, offset_y],
                    "normalization": "centered_letterbox_no_rescale_v1",
                }
            finally:
                logical.unlink(missing_ok=True)
        self._policy_window_mapping = {
            "width": width,
            "height": height,
            "offset_x": offset_x,
            "offset_y": offset_y,
        }
        self.last_policy_frame_id = frame_id
        return str(destination)

    def policy_to_backend_point(self, x: int, y: int) -> dict[str, Any]:
        mapping = self._policy_window_mapping
        if mapping is None:
            raise C1DesktopRuntimeError(
                "Blender input cannot precede a bound policy frame"
            )
        window_x = int(x) - mapping["offset_x"]
        window_y = int(y) - mapping["offset_y"]
        if not (
            0 <= window_x < mapping["width"]
            and 0 <= window_y < mapping["height"]
        ):
            raise C1DesktopRuntimeError(
                f"Blender policy point ({x}, {y}) falls outside the live "
                f"{mapping['width']}x{mapping['height']} window content"
            )
        return {
            "serialized_frame_point": [window_x, window_y],
            "backend_event_point": [
                window_x,
                mapping["height"] - 1 - window_y,
            ],
            "backend_coordinate_space": "blender_window_pixels_bottom_left",
            "application_window": [mapping["width"], mapping["height"]],
            "policy_content_offset": [
                mapping["offset_x"],
                mapping["offset_y"],
            ],
            "normalization": "centered_letterbox_no_rescale_v1",
        }

    def dispatch_input_batch(self, batch: Any) -> dict[str, Any]:
        if getattr(batch, "session_id", None) != self.session_id:
            raise C1DesktopRuntimeError("Blender input batch session mismatch")
        serialized_actions: list[dict[str, Any]] = []
        for raw_action in getattr(batch, "actions", ()):
            action = raw_action.to_dict()
            for x_key, y_key in (("x", "y"), ("x2", "y2")):
                x = action.get(x_key)
                y = action.get(y_key)
                if x is None and y is None:
                    continue
                if (
                    isinstance(x, bool)
                    or not isinstance(x, int)
                    or isinstance(y, bool)
                    or not isinstance(y, int)
                ):
                    raise C1DesktopRuntimeError(
                        "Blender input coordinates must be integer pairs"
                    )
                translated = self.policy_to_backend_point(x, y)
                action[x_key], action[y_key] = translated[
                    "serialized_frame_point"
                ]
            serialized_actions.append(action)
        result = self._command(
            "input_batch",
            frame_id=getattr(batch, "frame_id", None),
            batch_id=getattr(batch, "batch_id", None),
            actions=serialized_actions,
        )
        if result.get("status") != "success":
            raise C1DesktopRuntimeError(f"Blender input dispatch failed: {result}")
        return result

    def background_guard_metadata(self) -> dict[str, Any]:
        return self._background_window_guard.metadata()

    def close(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                self._command("shutdown")
                process.wait(timeout=8)
            except Exception:
                _stop_process(process)
        self.process = None
        self._background_window_guard.close()
        for handle_name in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, handle_name, None)
        leaked = process is not None and process.poll() is None
        if leaked:
            raise C1DesktopRuntimeError(
                f"Blender desktop process {process.pid} did not terminate cleanly"
            )


_LO_SUPPORTED_TARGET_KINDS = {
    "writer_document",
    "calc_spreadsheet",
    "impress_presentation",
}
_LO_SUPPORTED_ASSERTIONS = {
    "libreoffice_text_contains",
    "libreoffice_toc_contains",
    "libreoffice_table_cell_equals",
    "libreoffice_pdf_exists",
    "libreoffice_cell_value_equals",
    "libreoffice_formula_equals",
    "libreoffice_named_range_exists",
    "libreoffice_chart_exists",
    "libreoffice_sheet_exists",
    "libreoffice_slide_count_equals",
    "libreoffice_slide_title_equals",
    "libreoffice_slide_text_contains",
    "libreoffice_shape_text_contains",
    "no_libreoffice_errors",
}
_LO_SUPPORTED_STATE_ACTIONS = {
    "libreoffice_open_document",
    "libreoffice_insert_toc",
    "libreoffice_set_table_cell",
    "libreoffice_append_paragraph",
    "libreoffice_update_indexes",
    "libreoffice_set_cell",
    "libreoffice_recalculate",
    "libreoffice_sort_range",
    "libreoffice_define_named_range",
    "libreoffice_create_chart",
    "libreoffice_move_slide",
    "libreoffice_set_slide_title",
    "libreoffice_set_slide_bullets",
    "libreoffice_add_shape",
    "libreoffice_export_pdf",
}
_LO_TRANSITION_PERSISTENCE_CHECKS = (
    "transition.session_same",
    "transition.saved_url",
    "transition.persisted_file",
    "transition.not_modified",
    "transition.document_reopened",
)
_LO_PROTOCOL_HEADER = "C1_DESKTOP_V1"
_LO_RESULT_HEADER = "C1_DESKTOP_RESULT_V1"
_LO_WRITER_STYLE_ALIASES = {
    # Writer exposes this localized UI name in prompts and document metadata,
    # while the UNO ParaStyleName property requires the stable programmatic name.
    "Default Paragraph Style": "Standard",
}


def _b64(value: Any) -> str:
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _line(kind: str, *values: Any) -> str:
    return "\t".join([kind, *(_b64(value) for value in values)])


def _libreoffice_writer_style_name(value: Any) -> Any:
    return _LO_WRITER_STYLE_ALIASES.get(value, value)


def _scalar_parts(value: Any) -> tuple[str, str]:
    if isinstance(value, bool):
        return "bool", "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number", repr(value)
    if value is None:
        return "null", ""
    return "string", str(value)


def _libreoffice_protocol(
    command: str,
    *,
    target: dict[str, Any] | None = None,
    assertions: Iterable[dict[str, Any]] | None = None,
    actions: Iterable[dict[str, Any]] | None = None,
    visual: dict[str, Any] | None = None,
    artifact_dir: Path | None = None,
    transition_origin: dict[str, Any] | None = None,
    transition_checks: Iterable[str] | None = None,
) -> str:
    """Serialize the small, allowlisted C1 UNO command surface.

    A line protocol keeps the Java UNO bridge dependency-free.  Every value is
    base64 encoded, so tabs/newlines in slide text cannot alter command structure.
    """

    lines = [_LO_PROTOCOL_HEADER, _line("COMMAND", command)]
    if artifact_dir is not None:
        lines.append(_line("ARTIFACT_DIR", artifact_dir.resolve()))
    if transition_origin is not None:
        document_identity = transition_origin.get("document_identity")
        session_identity = transition_origin.get("session_identity")
        if not isinstance(document_identity, str) or not document_identity.strip():
            raise C1DesktopRuntimeError(
                "LibreOffice transition origin lacks document_identity"
            )
        if not isinstance(session_identity, str) or not session_identity.strip():
            raise C1DesktopRuntimeError(
                "LibreOffice transition origin lacks session_identity"
            )
        lines.append(_line("ORIGIN_DOCUMENT_IDENTITY", document_identity))
        lines.append(_line("ORIGIN_SESSION_IDENTITY", session_identity))
    if transition_checks is not None:
        if command != "probe_assertions":
            raise C1DesktopRuntimeError(
                "LibreOffice transition checks are exclusive to probe_assertions"
            )
        selected = list(transition_checks)
        if len(selected) != len(set(selected)):
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice transition checks must be unique"
            )
        for check_name in selected:
            if check_name not in _LO_TRANSITION_PERSISTENCE_CHECKS:
                raise C1DesktopRuntimeUnsupported(
                    f"unsupported LibreOffice transition check: {check_name!r}"
                )
            lines.append(_line("TRANSITION_CHECK", check_name))
    if target is not None:
        kind = target.get("kind")
        if kind not in _LO_SUPPORTED_TARGET_KINDS:
            raise C1DesktopRuntimeUnsupported(
                f"unsupported LibreOffice C1 target kind: {kind!r}"
            )
        lines.append(_line("KIND", kind))
        raw_path = target.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            if Path(raw_path).is_absolute() or ".." in Path(raw_path).parts:
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice C1 target.path must be a contained relative artifact path"
                )
            lines.append(_line("DOCUMENT_PATH", raw_path))
        if kind == "writer_document":
            paragraphs = target.get("paragraphs")
            if not isinstance(paragraphs, list) or not paragraphs:
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice Writer C1 target.paragraphs must be a non-empty list"
                )
            for paragraph in paragraphs:
                if not isinstance(paragraph, dict):
                    raise C1DesktopRuntimeUnsupported(
                        "Writer paragraph payload must be a mapping"
                    )
                lines.append(
                    _line(
                        "PARAGRAPH",
                        paragraph.get("text", ""),
                        _libreoffice_writer_style_name(
                            paragraph.get("style", "")
                        ),
                    )
                )
            tables = target.get("tables") or {}
            if not isinstance(tables, dict):
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice Writer C1 target.tables must be a mapping"
                )
            for table_name, rows in tables.items():
                if not isinstance(rows, list) or not rows:
                    raise C1DesktopRuntimeUnsupported(
                        "Writer table rows must be a non-empty list"
                    )
                columns = max(
                    (len(row) for row in rows if isinstance(row, list)),
                    default=0,
                )
                if columns < 1:
                    raise C1DesktopRuntimeUnsupported(
                        "Writer table rows must contain cells"
                    )
                lines.append(_line("TABLE", table_name, len(rows), columns))
                for row_index, row in enumerate(rows, start=1):
                    if not isinstance(row, list):
                        raise C1DesktopRuntimeUnsupported(
                            "Writer table row must be a list"
                        )
                    for column_index, value in enumerate(row, start=1):
                        lines.append(
                            _line(
                                "TABLE_CELL",
                                table_name,
                                column_index,
                                row_index,
                                value,
                            )
                        )
        elif kind == "calc_spreadsheet":
            sheets = target.get("sheets")
            if not isinstance(sheets, dict) or not sheets:
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice Calc C1 target.sheets must be a non-empty mapping"
                )
            for sheet_name, sheet_value in sheets.items():
                if not isinstance(sheet_value, dict):
                    raise C1DesktopRuntimeUnsupported(
                        "Calc sheet payload must be a mapping"
                    )
                lines.append(_line("SHEET", sheet_name))
                for address, value in (sheet_value.get("cells") or {}).items():
                    scalar_type, scalar_value = _scalar_parts(value)
                    lines.append(
                        _line("CELL", sheet_name, address, scalar_type, scalar_value)
                    )
                for address, formula in (sheet_value.get("formulas") or {}).items():
                    lines.append(_line("FORMULA", sheet_name, address, formula))
        else:
            slides = target.get("slides")
            if not isinstance(slides, list) or not slides:
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice Impress C1 target.slides must be a non-empty list"
                )
            lines.append(_line("SLIDE_COUNT", len(slides)))
            for index, slide in enumerate(slides, start=1):
                if not isinstance(slide, dict):
                    raise C1DesktopRuntimeUnsupported(
                        "Impress slide payload must be a mapping"
                    )
                for role in ("title", "subtitle"):
                    if slide.get(role) is not None:
                        lines.append(_line("SLIDE_TEXT", index, role, slide[role]))
                bullets = slide.get("bullets") or []
                if not isinstance(bullets, list):
                    raise C1DesktopRuntimeUnsupported("Impress bullets must be a list")
                for bullet in bullets:
                    lines.append(_line("SLIDE_TEXT", index, "bullet", bullet))
    if visual is not None:
        if command != "prepare_visual_capture":
            raise C1DesktopRuntimeError(
                "LibreOffice visual options are exclusive to prepare_visual_capture"
            )
        label = visual.get("label")
        if label not in {"before", "after"}:
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice visual capture label must be before or after"
            )
        lines.append(_line("VISUAL_LABEL", label))
        active_sheet = visual.get("active_sheet")
        if active_sheet is not None:
            if not isinstance(active_sheet, str) or not active_sheet.strip():
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice visual active_sheet must be a non-empty string"
                )
            lines.append(_line("ACTIVE_SHEET", active_sheet))
        active_slide = visual.get("active_slide")
        if active_slide is not None:
            if isinstance(active_slide, bool) or not isinstance(active_slide, int):
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice visual active_slide must be an integer"
                )
            lines.append(_line("ACTIVE_SLIDE", active_slide))
    if assertions is not None:
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice assertion must be a mapping"
                )
            assertion_type = assertion.get("type")
            if assertion_type not in _LO_SUPPORTED_ASSERTIONS:
                raise C1DesktopRuntimeUnsupported(
                    f"unsupported LibreOffice C1 assertion: {assertion_type!r}"
                )
            if assertion_type in {
                "libreoffice_text_contains",
                "libreoffice_toc_contains",
            }:
                lines.append(
                    _line("ASSERT", assertion_type, assertion.get("text", ""))
                )
            elif assertion_type == "libreoffice_table_cell_equals":
                lines.append(
                    _line(
                        "ASSERT",
                        assertion_type,
                        assertion.get("table", ""),
                        assertion.get("cell", ""),
                        assertion.get("expected", ""),
                    )
                )
            elif assertion_type == "libreoffice_pdf_exists":
                lines.append(
                    _line("ASSERT", assertion_type, assertion.get("path", ""))
                )
            elif assertion_type == "libreoffice_cell_value_equals":
                scalar_type, scalar_value = _scalar_parts(assertion.get("expected"))
                lines.append(
                    _line(
                        "ASSERT",
                        assertion_type,
                        assertion.get("sheet", "Sheet1"),
                        assertion.get("cell", ""),
                        scalar_type,
                        scalar_value,
                    )
                )
            elif assertion_type == "libreoffice_sheet_exists":
                lines.append(
                    _line("ASSERT", assertion_type, assertion.get("sheet", "Sheet1"))
                )
            elif assertion_type == "libreoffice_formula_equals":
                lines.append(
                    _line(
                        "ASSERT",
                        assertion_type,
                        assertion.get("sheet", "Sheet1"),
                        assertion.get("cell", ""),
                        assertion.get("formula", ""),
                    )
                )
            elif assertion_type == "libreoffice_named_range_exists":
                lines.append(
                    _line("ASSERT", assertion_type, assertion.get("name", ""))
                )
            elif assertion_type == "libreoffice_chart_exists":
                lines.append(
                    _line(
                        "ASSERT",
                        assertion_type,
                        assertion.get("sheet", "Sheet1"),
                        assertion.get("name", ""),
                    )
                )
            elif assertion_type == "libreoffice_slide_count_equals":
                lines.append(
                    _line("ASSERT", assertion_type, assertion.get("expected", ""))
                )
            elif assertion_type == "libreoffice_slide_title_equals":
                lines.append(
                    _line(
                        "ASSERT",
                        assertion_type,
                        assertion.get("slide", ""),
                        assertion.get("expected", ""),
                    )
                )
            elif assertion_type in {
                "libreoffice_slide_text_contains",
                "libreoffice_shape_text_contains",
            }:
                lines.append(
                    _line(
                        "ASSERT",
                        assertion_type,
                        assertion.get("slide", ""),
                        assertion.get("text", ""),
                    )
                )
            else:
                lines.append(_line("ASSERT", assertion_type))
    if actions is not None:
        if command not in {"apply_actions", "export_pdf"}:
            raise C1DesktopRuntimeError(
                "LibreOffice state actions are exclusive to apply_actions/export_pdf"
            )
        for action in actions:
            if not isinstance(action, dict):
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice state action must be a mapping"
                )
            action_type = action.get("type")
            if action_type not in _LO_SUPPORTED_STATE_ACTIONS:
                raise C1DesktopRuntimeUnsupported(
                    f"unsupported LibreOffice C1 state action: {action_type!r}"
                )
            if command == "export_pdf":
                if action_type != "libreoffice_export_pdf":
                    raise C1DesktopRuntimeUnsupported(
                        "LibreOffice export_pdf command only accepts export actions"
                    )
                lines.append(
                    _line("ACTION", action_type, action.get("path", ""))
                )
            elif action_type == "libreoffice_open_document":
                lines.append(_line("ACTION", action_type))
            elif action_type == "libreoffice_insert_toc":
                styles = action.get("include_styles") or []
                if not isinstance(styles, list):
                    raise C1DesktopRuntimeUnsupported(
                        "LibreOffice insert_toc include_styles must be a list"
                    )
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("title", "Contents"),
                        *styles,
                    )
                )
            elif action_type == "libreoffice_set_table_cell":
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("table", ""),
                        action.get("cell", ""),
                        action.get("value", ""),
                    )
                )
            elif action_type == "libreoffice_append_paragraph":
                lines.append(
                    _line("ACTION", action_type, action.get("text", ""))
                )
            elif action_type == "libreoffice_update_indexes":
                lines.append(_line("ACTION", action_type))
            elif action_type == "libreoffice_set_cell":
                scalar_type, scalar_value = _scalar_parts(action.get("value"))
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("sheet", ""),
                        action.get("cell", ""),
                        scalar_type,
                        scalar_value,
                    )
                )
            elif action_type == "libreoffice_recalculate":
                lines.append(_line("ACTION", action_type))
            elif action_type == "libreoffice_define_named_range":
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("name", ""),
                        action.get("sheet", "Sheet1"),
                        action.get("range", ""),
                    )
                )
            elif action_type == "libreoffice_sort_range":
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("sheet", "Sheet1"),
                        action.get("range", ""),
                        action.get("key", ""),
                        action.get("order", "ascending"),
                    )
                )
            elif action_type == "libreoffice_create_chart":
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("sheet", "Sheet1"),
                        action.get("name", ""),
                        action.get("range", ""),
                        action.get("chart_type", "Column"),
                    )
                )
            elif action_type == "libreoffice_move_slide":
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("from", ""),
                        action.get("to", ""),
                    )
                )
            elif action_type == "libreoffice_set_slide_title":
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("slide", ""),
                        action.get("title", ""),
                    )
                )
            elif action_type == "libreoffice_set_slide_bullets":
                bullets = action.get("bullets")
                if not isinstance(bullets, list) or not all(
                    isinstance(bullet, str) for bullet in bullets
                ):
                    raise C1DesktopRuntimeUnsupported(
                        "LibreOffice slide bullets action requires a string list"
                    )
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("slide", ""),
                        *bullets,
                    )
                )
            else:
                lines.append(
                    _line(
                        "ACTION",
                        action_type,
                        action.get("slide", ""),
                        action.get("shape", ""),
                        action.get("text", ""),
                    )
                )
    return "\n".join(lines) + "\n"


_LO_ARGUMENT_REJECTION = "IllegalArgumentException"


def _libreoffice_rejected_arguments(
    bridge_result: dict[str, Any] | None, detail: str
) -> bool:
    """True when the bridge rejected what the spec asked for, not the runtime.

    ``LibreOfficeC1Bridge`` validates every caller-supplied argument with
    ``IllegalArgumentException`` (unsupported target kind, unsupported shape,
    non-relative path) and reports its own runtime problems with
    ``IllegalStateException``. Only the first class is attributable to the
    model-generated spec.
    """

    if _LO_ARGUMENT_REJECTION in str(detail):
        return True
    if not isinstance(bridge_result, dict):
        return False
    for error in bridge_result.get("errors") or []:
        if _LO_ARGUMENT_REJECTION in str(error):
            return True
    for check in bridge_result.get("checks") or []:
        if isinstance(check, dict) and _LO_ARGUMENT_REJECTION in str(
            check.get("detail")
        ):
            return True
    return False


def _parse_libreoffice_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise C1DesktopRuntimeError(
            f"LibreOffice UNO bridge response is missing: {path}"
        )
    rows = path.read_text(encoding="utf-8").splitlines()
    if not rows or rows[0] != _LO_RESULT_HEADER:
        raise C1DesktopRuntimeError("LibreOffice UNO bridge returned an invalid header")
    result: dict[str, Any] = {"checks": [], "live": True}
    for raw in rows[1:]:
        fields = raw.split("\t")
        kind = fields[0]
        try:
            values = [base64.b64decode(value).decode("utf-8") for value in fields[1:]]
        except Exception as error:
            raise C1DesktopRuntimeError(
                f"LibreOffice UNO bridge returned malformed base64: {error}"
            ) from error
        if kind == "STATUS" and values:
            result["status"] = values[0]
        elif kind == "SOURCE" and values:
            result["source"] = values[0]
        elif kind == "DOCUMENT_PATH" and values:
            result["document_path"] = values[0]
        elif kind == "DOCUMENT_IDENTITY" and values:
            result["document_identity"] = values[0]
        elif kind == "DOCUMENT_URL" and values:
            result["document_url"] = values[0]
        elif kind == "DOCUMENT_MODIFIED" and values:
            if values[0] not in {"true", "false"}:
                raise C1DesktopRuntimeError(
                    "LibreOffice UNO bridge returned malformed document modified state"
                )
            result["document_modified"] = values[0] == "true"
        elif kind == "SESSION_IDENTITY" and values:
            result["session_identity"] = values[0]
        elif kind == "CHECK" and len(values) >= 3:
            result["checks"].append(
                {"name": values[0], "ok": values[1] == "true", "detail": values[2]}
            )
        elif kind == "ERROR" and values:
            result.setdefault("errors", []).append(values[0])
    if result.get("status") not in {"success", "failure"}:
        raise C1DesktopRuntimeError(
            "LibreOffice UNO bridge response lacks valid status"
        )
    return result


def _libreoffice_document_origin(
    evidence: dict[str, Any], *, expected_session: str
) -> dict[str, Any]:
    """Extract fail-closed identity state from one live UNO document probe."""

    document_identity = evidence.get("document_identity")
    session_identity = evidence.get("session_identity")
    document_url = evidence.get("document_url")
    document_modified = evidence.get("document_modified")
    if not isinstance(document_identity, str) or not document_identity.strip():
        raise C1DesktopRuntimeError(
            "LibreOffice semantic evidence lacks a live document identity"
        )
    if session_identity != expected_session:
        raise C1DesktopRuntimeError(
            "LibreOffice semantic evidence is bound to the wrong office session"
        )
    if not isinstance(document_url, str):
        raise C1DesktopRuntimeError(
            "LibreOffice semantic evidence lacks document URL state"
        )
    if type(document_modified) is not bool:
        raise C1DesktopRuntimeError(
            "LibreOffice semantic evidence lacks document modification state"
        )
    return {
        "document_identity": document_identity,
        "session_identity": session_identity,
        "document_url": document_url,
        "document_modified": document_modified,
    }


def _resolve_libreoffice_install(repo: Path) -> tuple[Path, Path]:
    configured = os.environ.get("WARPTEST_LIBREOFFICE_SOFFICE")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            repo / "instdir" / "LibreOfficeDev.app" / "Contents" / "MacOS" / "soffice",
            repo / "instdir" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice",
            Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        ]
    )
    path_candidate = shutil.which("soffice")
    if path_candidate:
        candidates.append(Path(path_candidate))
    for raw in candidates:
        candidate = raw.resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        parts = candidate.parts
        if "Contents" in parts:
            index = parts.index("Contents")
            contents = Path(*parts[: index + 1])
            if (contents / "Resources" / "java" / "libreoffice.jar").is_file():
                return candidate, contents
        # A Homebrew wrapper does not expose the UNO Java runtime beside itself;
        # prefer the matching installed app bundle if available.
        app_contents = Path("/Applications/LibreOffice.app/Contents")
        direct = app_contents / "MacOS" / "soffice"
        if (
            direct.is_file()
            and (app_contents / "Resources/java/libreoffice.jar").is_file()
        ):
            return direct.resolve(), app_contents.resolve()
    raise C1DesktopRuntimeUnsupported(
        "LibreOffice C1 requires a visible app bundle with Resources/java/libreoffice.jar"
    )


class LibreOfficeC1DesktopSession:
    """One persistent, visible LibreOffice process with a read-only UNO grader."""

    benchmark = "LibreOffice"
    semantic_source = LIBREOFFICE_SEMANTIC_SOURCE
    seed_evidence_source = LIBREOFFICE_SEED_EVIDENCE_SOURCE
    execution_backend = "background_pixel_v1"
    foreground_required = False
    coordinate_space = "frame_pixels_top_left_v1"
    capture_backend = "libreoffice_svp_composited_v1"
    input_backend = "libreoffice_vcl_post_event_v1"

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        command_timeout_s: float = 45.0,
        startup_timeout_s: float = 60.0,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.command_timeout_s = float(command_timeout_s)
        self.startup_timeout_s = float(startup_timeout_s)
        self.soffice, self.contents_dir = _resolve_libreoffice_install(self.repo)
        self.java_jar = self.contents_dir / "Resources" / "java" / "libreoffice.jar"
        self.frameworks_dir = self.contents_dir / "Frameworks"
        self.user_dir = self.report_dir / "libreoffice-c1-user"
        self.artifact_dir = self.report_dir / "libreoffice-c1-artifacts"
        self.bridge_dir = self.report_dir / "libreoffice-c1-java"
        self.classes_dir = self.bridge_dir / "classes"
        self.stdout_path = self.report_dir / "libreoffice-c1-stdout.log"
        self.stderr_path = self.report_dir / "libreoffice-c1-stderr.log"
        self.process: subprocess.Popen[Any] | None = None
        self.page = None
        self.session_id = uuid.uuid4().hex
        self.pipe_name = "warptest_c1_lo_" + self.session_id
        self.width = 1440
        self.height = 1000
        self.last_policy_frame_id: int | None = None
        self.last_policy_frame_capture: dict[str, Any] | None = None
        self._vcl_sequence = 0
        # macOS limits AF_UNIX paths to roughly 104 bytes.  Report roots can be
        # arbitrarily deep, so keep the nonce-scoped 0700 transport directory at
        # a fixed short path and copy captured frames into the report tree.
        self.vcl_bridge_dir = Path("/tmp") / (
            "warptest-lo-vcl-" + self.session_id[:12]
        )
        self.vcl_socket_path = self.vcl_bridge_dir / "bridge.sock"
        self._stdout_handle: Any | None = None
        self._stderr_handle: Any | None = None
        self._background_window_guard = MacOSBackgroundWindowGuard(self.report_dir)
        self._command_index = 0
        self._transition_origin: dict[str, Any] | None = None
        self._transition_origin_from_restore = False

    @property
    def pid(self) -> int | None:
        return int(self.process.pid) if self.process is not None else None

    def __enter__(self) -> "LibreOfficeC1DesktopSession":
        return self.start_clean()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _compile_bridge(self) -> None:
        source = Path(__file__).with_name("LibreOfficeC1Bridge.java")
        if not source.is_file():
            raise C1DesktopRuntimeError(
                f"LibreOffice C1 Java bridge is missing: {source}"
            )
        self.classes_dir.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                "javac",
                "-encoding",
                "UTF-8",
                "-cp",
                str(self.java_jar),
                "-d",
                str(self.classes_dir),
                str(source),
            ],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise C1DesktopRuntimeError(
                "LibreOffice C1 Java bridge compilation failed: "
                + (completed.stderr or completed.stdout)[-4000:]
            )

    def _prepare_user_profile(self) -> None:
        """Seed a deterministic profile without first-run or system dialogs."""

        registry = self.contents_dir / "Resources" / "registry" / "main.xcd"
        try:
            installed = registry.read_text(encoding="utf-8")
        except OSError as error:
            raise C1DesktopRuntimeError(
                f"LibreOffice installation registry is unavailable: {registry}"
            ) from error
        match = re.search(
            r'<prop oor:name="ooSetupVersion"><value>([0-9]+(?:\.[0-9]+)+)</value></prop>',
            installed,
        )
        if match is None:
            raise C1DesktopRuntimeError(
                "LibreOffice installation registry lacks a numeric product version"
            )
        profile = self.user_dir / "user"
        profile.mkdir(parents=True, exist_ok=True)
        modifications = profile / "registrymodifications.xcu"
        if modifications.exists():
            return
        product_version = match.group(1)
        modifications.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
            'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
            '<item oor:path="/org.openoffice.Setup/Product">'
            '<prop oor:name="ooSetupLastVersion" oor:op="fuse"><value>'
            + product_version
            + '</value></prop>'
            '<prop oor:name="WhatsNew" oor:op="fuse"><value>false</value></prop>'
            '<prop oor:name="WhatsNewDialog" oor:op="fuse"><value>false</value></prop>'
            '</item>\n'
            '<item oor:path="/org.openoffice.Setup/Office">'
            '<prop oor:name="ooSetupInstCompleted" oor:op="fuse">'
            '<value>true</value></prop></item>\n'
            '<item oor:path="/org.openoffice.Office.Common/Misc">'
            '<prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop>'
            '<prop oor:name="ShowTipOfTheDay" oor:op="fuse">'
            '<value>false</value></prop>'
            '<prop oor:name="UseSystemFileDialog" oor:op="fuse">'
            '<value>false</value></prop></item>\n'
            '</oor:items>\n',
            encoding="utf-8",
        )

    def start_clean(self) -> "LibreOfficeC1DesktopSession":
        if self.process is not None and self.process.poll() is None:
            return self
        if sys.platform != "darwin":
            raise C1DesktopRuntimeUnsupported(
                "formal native Agent-S desktop sessions currently require macOS"
            )
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        self.vcl_bridge_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.vcl_bridge_dir.chmod(0o700)
        self.vcl_socket_path.unlink(missing_ok=True)
        self._prepare_user_profile()
        self._compile_bridge()
        user_url = self.user_dir.resolve().as_uri()
        command = [
            str(self.soffice),
            f"-env:UserInstallation={user_url}",
            "--norestore",
            "--nofirststartwizard",
            f"--accept=pipe,name={self.pipe_name};urp;",
        ]
        try:
            self._background_window_guard.prepare()
            self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
            self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
            self.process = subprocess.Popen(
                command,
                cwd=str(self.repo),
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "SAL_USE_VCLPLUGIN": "svp",
                    "WARPTEST_C1_VCL_NONCE": self.session_id,
                    "WARPTEST_C1_VCL_SOCKET": str(self.vcl_socket_path),
                    "WARPTEST_C1_VCL_ROOT": str(self.vcl_bridge_dir),
                },
                start_new_session=True,
            )
            self._background_window_guard.start(self.process.pid)
            vcl_ping = self._vcl_request(
                "ping", timeout_s=self.startup_timeout_s
            )
            if (
                vcl_ping.get("accepted") is not True
                or (vcl_ping.get("width"), vcl_ping.get("height"))
                != (self.width, self.height)
            ):
                raise C1DesktopRuntimeError(
                    f"LibreOffice VCL background bridge ping failed: {vcl_ping}"
                )
            # The Java resolver performs its own bounded retry against the pipe.
            # Launch it once so a broken UNO installation fails promptly instead of
            # spawning a train of 15-second helper processes and desktop dialogs.
            ping = self._run_bridge("ping")
            if ping.get("status") != "success":
                raise C1DesktopRuntimeError(
                    f"LibreOffice UNO bridge ping failed: {ping}"
                )
            self._background_window_guard.await_hidden()
            return self
        except BaseException:
            self.close()
            raise

    def _vcl_request(
        self,
        operation: str,
        *,
        timeout_s: float | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise C1DesktopRuntimeError("LibreOffice VCL bridge process is not running")
        deadline = time.monotonic() + float(timeout_s or self.command_timeout_s)
        last_error: Exception | None = None
        sequence = self._vcl_sequence + 1
        request = {
            "schema_version": 1,
            "session_id": self.session_id,
            "sequence": sequence,
            "operation": operation,
            **payload,
        }
        encoded_request = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        while time.monotonic() < deadline:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(min(1.0, max(0.05, deadline - time.monotonic())))
            try:
                try:
                    client.connect(str(self.vcl_socket_path))
                except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as error:
                    last_error = error
                    time.sleep(0.05)
                    continue
                # A successful connect establishes the one in-flight logical
                # request.  Do not abandon it after an arbitrary one-second
                # slice and resend with a new sequence while the SVP render loop
                # is still starting.  Wait for the remainder of the registered
                # command timeout instead.
                client.settimeout(max(0.05, deadline - time.monotonic()))
                client.sendall(encoded_request)
                client.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                try:
                    while True:
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                except socket.timeout as error:
                    raise C1DesktopRuntimeError(
                        "LibreOffice VCL bridge response timed out after request dispatch; "
                        "the single in-flight request will not be replayed"
                    ) from error
                response = json.loads(b"".join(chunks).decode("utf-8"))
                if not isinstance(response, dict):
                    raise C1DesktopRuntimeError(
                        "LibreOffice VCL bridge returned non-object JSON"
                    )
                if response.get("session_id") != self.session_id:
                    raise C1DesktopRuntimeError(
                        "LibreOffice VCL bridge session nonce mismatch"
                    )
                if response.get("sequence") != sequence:
                    raise C1DesktopRuntimeError(
                        "LibreOffice VCL bridge response sequence mismatch: "
                        f"expected {sequence}, received {response.get('sequence')!r}"
                    )
                # A nonce/sequence-valid rejected request has still consumed its
                # transport sequence on the single-flight VCL bridge. Advance
                # locally before surfacing the operation error so a later fresh
                # capture cannot replay the rejected sequence.
                self._vcl_sequence = sequence
                if response.get("accepted") is not True:
                    raise C1DesktopRuntimeError(
                        "LibreOffice VCL bridge rejected request: "
                        + str(response.get("error") or response)
                    )
                return response
            finally:
                client.close()
        raise C1DesktopRuntimeError(
            "LibreOffice SVP/VCL background bridge is unavailable; formal mode "
            f"does not fall back to desktop capture or Quartz input: {last_error}"
        )

    def _run_bridge(
        self,
        command: str,
        *,
        target: dict[str, Any] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        actions: list[dict[str, Any]] | None = None,
        visual: dict[str, Any] | None = None,
        transition_origin: dict[str, Any] | None = None,
        transition_checks: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise C1DesktopRuntimeError("LibreOffice desktop session is not running")
        self._command_index += 1
        stem = f"{self._command_index:04d}-{command}"
        request_path = self.bridge_dir / f"{stem}.request"
        response_path = self.bridge_dir / f"{stem}.response"
        request_path.write_text(
            _libreoffice_protocol(
                command,
                target=target,
                assertions=assertions,
                actions=actions,
                visual=visual,
                artifact_dir=self.artifact_dir,
                transition_origin=transition_origin,
                transition_checks=transition_checks,
            ),
            encoding="utf-8",
        )
        classpath = os.pathsep.join([str(self.classes_dir), str(self.java_jar)])
        command_line = [
            "java",
            f"-Djava.library.path={self.frameworks_dir}",
            "-cp",
            classpath,
            "LibreOfficeC1Bridge",
            self.pipe_name,
            str(request_path),
            str(response_path),
        ]
        try:
            completed = subprocess.run(
                command_line,
                cwd=str(self.repo),
                capture_output=True,
                text=True,
                timeout=self.command_timeout_s,
                env={
                    **os.environ,
                    "UNO_PATH": str(self.contents_dir / "MacOS"),
                    "URE_BOOTSTRAP": "vnd.sun.star.pathname:"
                    + str(self.contents_dir / "Resources/fundamentalrc"),
                },
            )
        except subprocess.TimeoutExpired as error:
            if command not in {"probe_target", "probe_assertions"}:
                raise C1DesktopRuntimeError(
                    f"LibreOffice UNO bridge command timed out: {command}"
                ) from error
            if self.process is None or self.process.poll() is not None:
                raise C1DesktopRuntimeError(
                    "LibreOffice semantic probe timed out after the process exited"
                ) from error
            return {
                "status": "failure",
                "source": self.semantic_source,
                "live": True,
                "passed": False,
                "checks": [
                    {
                        "name": "bridge.semantic_timeout",
                        "ok": False,
                        "detail": (
                            "read-only UNO semantic probe timed out while the "
                            "background office process remained alive"
                        ),
                    }
                ],
                "errors": [f"semantic probe timed out: {command}"],
                "session_identity": self.pipe_name,
                "probe_timeout": True,
            }
        if completed.returncode != 0:
            bridge_result = (
                _parse_libreoffice_result(response_path)
                if response_path.is_file()
                else None
            )
            detail = (completed.stderr or completed.stdout)[-4000:] or repr(
                bridge_result
            )
            message = "LibreOffice UNO bridge command failed: " + detail
            # The bridge validates its arguments with IllegalArgumentException and
            # reports internal runtime problems with IllegalStateException. An
            # argument rejection means the model's own spec asked for an operation
            # this task does not expose, which is a scored treatment failure; only
            # the latter is an infrastructure outage. Conflating them let one
            # unexecutable spec abort the whole invocation and forbid recovery.
            if _libreoffice_rejected_arguments(bridge_result, detail):
                raise C1DesktopRuntimeUnsupported(message)
            raise C1DesktopRuntimeError(message)
        return _parse_libreoffice_result(response_path)

    def restore_target(self, spec_or_target: dict[str, Any]) -> dict[str, Any]:
        target = _target_only(spec_or_target)
        result = self._run_bridge("restore_target", target=target)
        if result.get("status") not in {"success", "failure"}:
            raise C1DesktopRuntimeError(
                f"LibreOffice target-only restore returned an invalid status: {result}"
            )
        origin = _libreoffice_document_origin(
            result, expected_session=self.pipe_name
        )
        if origin["document_url"]:
            raise C1DesktopRuntimeError(
                "LibreOffice target-only restore unexpectedly pre-saved the document"
            )
        self._transition_origin = origin
        self._transition_origin_from_restore = True
        return result

    def probe_start(self, canonical_spec_or_target: dict[str, Any]) -> dict[str, Any]:
        target = _target_only(canonical_spec_or_target)
        evidence = self._run_bridge("probe_target", target=target)
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        if evidence["passed"]:
            origin = _libreoffice_document_origin(
                evidence, expected_session=self.pipe_name
            )
            if self._transition_origin_from_restore:
                if origin["document_identity"] != self._transition_origin.get(
                    "document_identity"
                ):
                    raise C1DesktopRuntimeError(
                        "LibreOffice restored start probe changed document identity"
                    )
            else:
                # From-start Phase A owns construction; the final successful start
                # probe is the transition origin for the policy's save/reopen proof.
                self._transition_origin = origin
        return evidence

    def probe_liveness(self) -> dict[str, Any]:
        """Return registered live-process evidence without requiring a document."""

        evidence = self._run_bridge("ping")
        evidence.setdefault("errors", [])
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        return evidence

    def prepare_visual_capture(
        self, spec: dict[str, Any], label: str
    ) -> dict[str, Any]:
        if label not in {"before", "after"}:
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice visual capture label must be before or after"
            )
        target = _target_only(spec)
        oracle = visual_oracle(spec)
        capture = oracle.get("capture") if isinstance(oracle, dict) else None
        capture = capture if isinstance(capture, dict) else {}
        visual: dict[str, Any] = {"label": label}
        if target.get("kind") == "calc_spreadsheet":
            active_sheet = (
                capture.get(f"{label}_active_sheet")
                or capture.get("active_sheet")
                or next(iter((target.get("sheets") or {}).keys()), None)
            )
            if active_sheet is not None:
                visual["active_sheet"] = active_sheet
        elif target.get("kind") == "impress_presentation":
            raw_slide = (
                capture.get(f"{label}_active_slide")
                or capture.get("active_slide")
                or 1
            )
            try:
                visual["active_slide"] = int(raw_slide)
            except (TypeError, ValueError) as error:
                raise C1DesktopRuntimeUnsupported(
                    "LibreOffice visual active slide is invalid"
                ) from error
        evidence = self._run_bridge(
            "prepare_visual_capture", target=target, visual=visual
        )
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        if not evidence["passed"]:
            raise C1DesktopRuntimeError(
                f"LibreOffice visual preparation failed: {evidence}"
            )
        return evidence

    def probe_goal(
        self, canonical_spec_or_assertions: dict[str, Any] | list[dict[str, Any]]
    ) -> dict[str, Any]:
        assertions = _assertions_only(canonical_spec_or_assertions)
        transition_checks = list(_LO_TRANSITION_PERSISTENCE_CHECKS)
        if isinstance(canonical_spec_or_assertions, dict):
            declared: list[str] | None = None
            for surface_name in ("gui", "state"):
                surface = surface_contract(canonical_spec_or_assertions, surface_name)
                goal = surface.get("goal") if isinstance(surface, dict) else None
                value = goal.get("native_checks") if isinstance(goal, dict) else None
                if isinstance(value, list):
                    declared = value
                    break
            if evaluation_contract(canonical_spec_or_assertions) is not None:
                if declared is None:
                    raise C1DesktopRuntimeUnsupported(
                        "LibreOffice evaluation contract lacks native goal checks"
                    )
                transition_checks = list(declared)
        if self._transition_origin is None:
            raise C1DesktopRuntimeError(
                "LibreOffice goal probe lacks a bound start document identity"
            )
        evidence = self._run_bridge(
            "probe_assertions",
            assertions=assertions,
            transition_origin=self._transition_origin,
            transition_checks=transition_checks,
        )
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        return evidence

    @staticmethod
    def _file_url_path(value: Any) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return None
        try:
            return Path(unquote(parsed.path)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    def _document_state(
        self, target: dict[str, Any], *, require_present: bool = True
    ) -> dict[str, Any]:
        evidence = self._run_bridge("probe_document_state", target=target)
        evidence["passed"] = semantic_probe_passed(
            evidence, expected_source=self.semantic_source
        )
        if require_present and not evidence["passed"]:
            raise C1DesktopRuntimeError(
                f"LibreOffice live document-state probe failed: {evidence}"
            )
        return evidence

    def _wait_for_document_state(
        self,
        target: dict[str, Any],
        predicate: Any,
        *,
        description: str,
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] | None = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                last = self._document_state(target, require_present=False)
                if predicate(last):
                    return last
            except C1DesktopRuntimeError as error:
                last_error = error
            time.sleep(0.35)
        suffix = f"; last probe={last}" if last is not None else ""
        if last_error is not None:
            suffix += f"; last error={last_error}"
        raise C1DesktopRuntimeError(
            f"LibreOffice timed out waiting for {description}{suffix}"
        )

    def _state_document_path(self, target: dict[str, Any]) -> Path:
        raw_path = target.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice state transition requires target.path"
            )
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice state transition target.path must stay in its artifact directory"
            )
        destination = (self.artifact_dir / relative).resolve()
        try:
            destination.relative_to(self.artifact_dir.resolve())
        except ValueError as error:
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice state transition target.path escapes its artifact directory"
            ) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise C1DesktopRuntimeError(
                    f"LibreOffice state artifact is not a regular file: {destination}"
                )
            destination.unlink()
        return destination

    def _save_and_reopen(
        self, target: dict[str, Any], destination: Path
    ) -> dict[str, Any]:
        if self._transition_origin is None:
            raise C1DesktopRuntimeError(
                "LibreOffice save/reopen requires a live bound transition origin"
            )
        origin = self._transition_origin
        saved = self._run_bridge("save_document", target=target)
        if not semantic_probe_passed(saved, expected_source=self.semantic_source):
            raise C1DesktopRuntimeError(
                f"LibreOffice bounded state save failed: {saved}"
            )
        if not (
            saved.get("session_identity") == origin["session_identity"]
            and saved.get("document_identity") == origin["document_identity"]
            and saved.get("document_modified") is False
            and self._file_url_path(saved.get("document_url")) == destination
            and destination.is_file()
            and destination.stat().st_size > 0
        ):
            raise C1DesktopRuntimeError(
                f"LibreOffice saved artifact identity is invalid: {saved}"
            )
        # Load the exact persisted artifact back into the same headed frame.  The
        # Java bridge uses the frame's `_self` loader so LibreOffice replaces the
        # live component in the same process/session.  A changed UNO OID, clean
        # modification state, and the persisted URL prove a real reopen rather
        # than another view of the unsaved in-memory document.
        reopened = self._run_bridge("reopen_document", target=target)
        if not semantic_probe_passed(
            reopened, expected_source=self.semantic_source
        ):
            raise C1DesktopRuntimeError(
                f"LibreOffice persisted document did not reopen cleanly: {reopened}"
            )
        if not (
            reopened.get("session_identity") == origin["session_identity"]
            and reopened.get("document_identity") != origin["document_identity"]
            and reopened.get("document_modified") is False
            and self._file_url_path(reopened.get("document_url")) == destination
            and Path(str(reopened.get("document_path", ""))).resolve()
            == destination
            and destination.is_file()
            and destination.stat().st_size > 0
        ):
            raise C1DesktopRuntimeError(
                f"LibreOffice reopened artifact identity is invalid: {reopened}"
            )
        reopened["saved_document_path"] = str(destination)
        reopened["pre_close_document_identity"] = saved.get("document_identity")
        return reopened

    def execute_state_transition(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Apply the registered arm-E mutation and prove native save/reopen.

        This surface is deliberately separate from Agent-S.  State arm E may use
        the registered programmatic mutations, while save and reopen still travel
        through the headed application UI and the final grade is a live Java UNO
        observation bound to the original session/document identity.
        """

        if self._transition_origin is None:
            raise C1DesktopRuntimeError(
                "LibreOffice state transition lacks a restored start identity"
            )
        target = _target_only(spec)
        raw_actions = spec.get("actions")
        if not isinstance(raw_actions, list) or not all(
            isinstance(action, dict) for action in raw_actions
        ):
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice state transition requires a mapping action list"
            )
        action_types = [action.get("type") for action in raw_actions]
        save_index = (
            action_types.index("libreoffice_save")
            if "libreoffice_save" in action_types
            else -1
        )
        reopen_index = (
            action_types.index("libreoffice_reopen")
            if "libreoffice_reopen" in action_types
            else -1
        )
        if (
            len(action_types) < 3
            or action_types.count("libreoffice_save") != 1
            or action_types.count("libreoffice_reopen") != 1
            or reopen_index != save_index + 1
            or any(
                action_type != "libreoffice_export_pdf"
                for action_type in action_types[reopen_index + 1 :]
            )
        ):
            raise C1DesktopRuntimeUnsupported(
                "LibreOffice state transition requires one consecutive save/reopen, "
                "optionally followed only by registered PDF export actions"
            )
        mutation_actions = copy.deepcopy(raw_actions[:save_index])
        export_actions = copy.deepcopy(raw_actions[reopen_index + 1 :])
        destination = self._state_document_path(target)
        action_evidence = self._run_bridge(
            "apply_actions", target=target, actions=mutation_actions
        )
        action_evidence.setdefault("errors", [])
        action_evidence["passed"] = semantic_probe_passed(
            action_evidence, expected_source=self.semantic_source
        )
        action_evidence["state_action_checks"] = copy.deepcopy(
            action_evidence.get("checks", [])
        )
        action_evidence[
            "state_transition_backend"
        ] = "uno_mutation_native_ui_save_reopen_v1"
        action_evidence["state_transition_stage"] = "actions"
        origin = self._transition_origin
        if not action_evidence["passed"]:
            return action_evidence
        if (
            action_evidence.get("session_identity") != origin["session_identity"]
            or action_evidence.get("document_identity")
            != origin["document_identity"]
            or action_evidence.get("document_url")
        ):
            raise C1DesktopRuntimeError(
                "LibreOffice registered mutation changed or pre-saved the start document"
            )

        reopen_evidence = self._save_and_reopen(target, destination)
        export_checks: list[dict[str, Any]] = []
        for export_action in export_actions:
            exported = self._run_bridge(
                "export_pdf", target=target, actions=[export_action]
            )
            if not semantic_probe_passed(
                exported, expected_source=self.semantic_source
            ):
                raise C1DesktopRuntimeError(
                    f"LibreOffice bounded PDF export failed: {exported}"
                )
            export_checks.extend(copy.deepcopy(exported.get("checks", [])))
        goal_evidence = self.probe_goal(spec)
        if (
            goal_evidence.get("document_identity")
            != reopen_evidence.get("document_identity")
            or self._file_url_path(goal_evidence.get("document_url")) != destination
            or Path(str(goal_evidence.get("document_path", ""))).resolve()
            != destination
        ):
            raise C1DesktopRuntimeError(
                "LibreOffice final semantic grade does not match the reopened artifact"
            )
        goal_evidence["state_action_checks"] = copy.deepcopy(
            [*action_evidence["checks"], *export_checks]
        )
        goal_evidence[
            "state_transition_backend"
        ] = "uno_mutation_native_ui_save_reopen_v1"
        goal_evidence["state_transition_stage"] = "goal"
        return goal_evidence

    def seed_evidence(
        self,
        experiment_seed: int,
        semantic_probe: dict[str, Any],
        task_id: str,
        stage: str,
    ) -> dict[str, Any]:
        return _native_seed_evidence(
            self, experiment_seed, semantic_probe, task_id, stage
        )

    def screenshot(self, path: str | Path, full_page: bool = False) -> str:
        del full_page
        return self.capture_policy_frame(path)

    def policy_frame_dimensions(self) -> tuple[int, int]:
        return self.width, self.height

    def capture_policy_frame(self, path: str | Path) -> str:
        destination = Path(path).expanduser().resolve()
        frames = (self.vcl_bridge_dir / "frames").resolve()
        frames.mkdir(parents=True, exist_ok=True)
        internal = frames / f"frame-{self._vcl_sequence + 1:08d}.png"
        response = self._vcl_request("capture", path=str(internal))
        if (
            response.get("capture_backend") != self.capture_backend
            or (response.get("width"), response.get("height"))
            != (self.width, self.height)
        ):
            raise C1DesktopRuntimeError(
                f"LibreOffice VCL framebuffer dimensions/backend drifted: {response}"
            )
        frame_id = response.get("frame_id")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 1:
            raise C1DesktopRuntimeError(
                "LibreOffice VCL bridge returned an invalid frame id"
            )
        if internal.parent != frames or not internal.is_file():
            raise C1DesktopRuntimeError(
                "LibreOffice VCL bridge did not produce a contained frame"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(internal, destination)
        self.last_policy_frame_id = frame_id
        self.last_policy_frame_capture = {
            "frame_id": frame_id,
            "active_window_title": response.get("active_window_title"),
            "visible_window_count": response.get("visible_window_count"),
        }
        return str(destination)

    def dispatch_input_batch(self, batch: Any) -> dict[str, Any]:
        if getattr(batch, "session_id", None) != self.session_id:
            raise C1DesktopRuntimeError("LibreOffice input batch session mismatch")
        response = self._vcl_request(
            "input_batch",
            frame_id=getattr(batch, "frame_id", None),
            batch_id=getattr(batch, "batch_id", None),
            actions=[action.to_dict() for action in getattr(batch, "actions", ())],
        )
        if response.get("input_backend") != self.input_backend:
            raise C1DesktopRuntimeError(
                f"LibreOffice VCL input backend drifted: {response}"
            )
        return response

    def background_guard_metadata(self) -> dict[str, Any]:
        return self._background_window_guard.metadata()

    def close(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                self._run_bridge("shutdown")
                process.wait(timeout=8)
            except Exception:
                _stop_process(process)
        self.process = None
        self._background_window_guard.close()
        self.last_policy_frame_id = None
        self.last_policy_frame_capture = None
        self.vcl_socket_path.unlink(missing_ok=True)
        self._transition_origin = None
        self._transition_origin_from_restore = False
        for handle_name in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, handle_name, None)
        leaked = process is not None and process.poll() is None
        if leaked:
            raise C1DesktopRuntimeError(
                f"LibreOffice desktop process {process.pid} did not terminate cleanly"
            )


def desktop_session_for(
    benchmark: str,
    repo: str | Path,
    report_dir: str | Path,
    **kwargs: Any,
) -> BlenderC1DesktopSession | LibreOfficeC1DesktopSession:
    normalized = str(benchmark).strip().lower()
    if normalized == "blender":
        return BlenderC1DesktopSession(repo, report_dir, **kwargs)
    if normalized in {"libreoffice", "libre_office"}:
        return LibreOfficeC1DesktopSession(repo, report_dir, **kwargs)
    raise C1DesktopRuntimeUnsupported(
        f"no persistent C1 desktop session is registered for {benchmark!r}"
    )


__all__ = [
    "BLENDER_SEMANTIC_SOURCE",
    "BLENDER_SEED_EVIDENCE_SOURCE",
    "LIBREOFFICE_SEMANTIC_SOURCE",
    "LIBREOFFICE_SEED_EVIDENCE_SOURCE",
    "NATIVE_GUI_SEED_ALGORITHM",
    "BlenderC1DesktopSession",
    "C1DesktopRuntimeError",
    "C1DesktopRuntimeUnsupported",
    "LibreOfficeC1DesktopSession",
    "desktop_session_for",
    "semantic_probe_passed",
]
