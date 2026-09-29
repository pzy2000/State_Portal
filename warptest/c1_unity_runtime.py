"""Persistent, background-pixel Unity sessions for C1 GUI-agent trials.

The normal jynew and PAL3 adapters are intentionally cold, batch-mode utility
runners.  C1 has a different trust boundary: the checkpoint hook may establish
Phase A, but it must leave one real game process alive while the agent performs
Phase B through final GameView pixels and its private event queue. This module implements the host
side of the small, sequence-numbered IPC protocol exposed by the two bundled
Unity instrumentation templates.

No C1 operation in this module ever submits ``spec.actions`` to the Unity
checkpoint hook.  Save/load tasks instead submit a bounded declarative
``transition_expectation`` (kind + slot) so the live C# session can attest that
the public UI produced the registered transition after the start boundary.
``restore_target`` additionally clears assertions before it is serialized, so
a future hook regression cannot silently execute or certify Phase B during
checkpoint setup.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from . import native_window
from .c1_background_window_guard import MacOSBackgroundWindowGuard
from .c1_evaluation import materialize_legacy_spec, surface_contract
from .jynew_runtime import (
    _NoOpPage,
    _build_utility_spec,
    _find_unity_executable,
    _screenshot_is_informative,
    ensure_jynew_instrumentation,
    find_jynew_project_path,
)
from .native_input import QuartzNativeInput, native_runtime_lock
from .pal3_runtime import (
    _build_pal3_utility_spec,
    _find_pal3_unity_executable,
    _pal3_project_editor_version,
    _pal3_unity_editor_version,
    ensure_pal3_instrumentation,
    find_pal3_project_path,
)
from .unity_serialization_guard import (
    capture_jynew_serialization_guard,
    restore_jynew_serialization_guard,
)


C1_UNITY_PROTOCOL_VERSION = "warptest-c1-unity-v3"
DEFAULT_STARTUP_TIMEOUT_S = 300.0
DEFAULT_REQUEST_TIMEOUT_S = 180.0


class UnityC1ProtocolError(RuntimeError):
    """The headed Unity process violated the C1 IPC contract."""


def _save_load_transition_expectation(
    spec: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return the YAML-declared public save/load witness, if required.

    This intentionally never inspects ``spec.actions``.  The requirement stays
    in the trusted evaluation control plane; only its bounded kind/slot payload
    is forwarded to the read-only Unity bridge.
    """

    surface = surface_contract(spec, "gui")
    goal = surface.get("goal") if isinstance(surface, Mapping) else None
    requirements = goal.get("requirements") if isinstance(goal, Mapping) else None
    matches = [
        item
        for item in (requirements if isinstance(requirements, list) else [])
        if isinstance(item, Mapping)
        and item.get("name") == "c1.public_save_load_witness"
    ]
    if len(matches) != 1:
        return None
    requirement = matches[0]
    return {
        "kind": requirement.get("kind"),
        "slot": requirement.get("slot"),
        "ordered_events": copy.deepcopy(requirement.get("ordered_events")),
    }


def _unity_bridge_transition_expectation(
    expectation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if expectation is None:
        return None
    return {"kind": expectation.get("kind"), "slot": expectation.get("slot")}


class UnityC1Session:
    """Base class for one persistent background Unity editor play session."""

    benchmark = "unity"
    execute_method = ""
    semantic_source = "unity_live_semantic_probe_v1"
    width = 1280
    height = 720
    execution_backend = "background_pixel_v1"
    foreground_required = False
    coordinate_space = "frame_pixels_top_left_v1"
    capture_backend = "unity_gameview_capture_v3"
    input_backend = "unity_editor_queue_gameview_input_v1"

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        experiment_seed: int = 0,
        native_factory: Callable[..., Any] = QuartzNativeInput,
        acquire_native_lock: bool = False,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.out_dir = Path(report_dir).expanduser().resolve()
        self.startup_timeout_s = float(startup_timeout_s)
        self.request_timeout_s = float(request_timeout_s)
        self.experiment_seed = int(experiment_seed)
        self._native_factory = native_factory
        self._acquire_native_lock = bool(acquire_native_lock)

        self.report_dir = self.out_dir
        self.page = _NoOpPage()
        self.app_url = None
        self.logs: list[str] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.network_api_calls: dict[str, Any] = {"attempted": []}

        self._project_path = self._find_project_path()
        self._serialization_guard = self._capture_serialization_guard()
        self._request_path = self.out_dir / f"{self.benchmark}-c1-request.json"
        self._report_path = self.out_dir / f"{self.benchmark}-c1-report.json"
        self._ready_path = self.out_dir / f"{self.benchmark}-c1-ready.json"
        self._unity_log_path = self.out_dir / f"{self.benchmark}-c1-unity.log"
        self._stdout_path = self.out_dir / f"{self.benchmark}-c1-stdout.log"
        self._stderr_path = self.out_dir / f"{self.benchmark}-c1-stderr.log"
        self._sequence = 0
        self._capture_sequence = 0
        self.session_id = uuid.uuid4().hex
        self.last_policy_frame_id: int | None = None
        self._process: subprocess.Popen[str] | None = None
        self._stdout_handle: Any = None
        self._stderr_handle: Any = None
        self._lock_context: Any = None
        self._started = False
        self.native: Any = None
        self._last_screenshot_path: str | None = None
        self._last_semantic_observation: dict[str, Any] | None = None
        self._last_screenshot_metadata: dict[str, Any] = {
            "screenshot_status": "skipped",
            "screenshot_source": "",
            "screenshot_detail": "",
            "live_source": "capture_failure",
        }
        self._background_window_guard = MacOSBackgroundWindowGuard(self.out_dir)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def hold_pid(self) -> int | None:
        return self.pid if self.is_alive() else None

    @property
    def process(self) -> subprocess.Popen[str] | None:
        return self._process

    @property
    def launch_command(self) -> list[str]:
        return self._build_launch_command()

    @property
    def last_screenshot_metadata(self) -> dict[str, Any]:
        return dict(self._last_screenshot_metadata)

    def __enter__(self) -> "UnityC1Session":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _find_project_path(self) -> Path:
        raise NotImplementedError

    def _ensure_instrumentation(self) -> Mapping[str, object]:
        raise NotImplementedError

    def _find_editor(self) -> str:
        raise NotImplementedError

    def _utility_spec(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _preflight_editor(self, editor: str) -> None:
        del editor

    def _build_launch_command(self) -> list[str]:
        editor = self._find_editor()
        self._preflight_editor(editor)
        # Deliberately headed: batchmode produces non-representative GameView
        # captures for these projects, and nographics has no framebuffer.
        return [
            editor,
            "-projectPath",
            str(self._project_path),
            "-executeMethod",
            self.execute_method,
            "-logFile",
            str(self._unity_log_path),
            "-screen-width",
            str(self.width),
            "-screen-height",
            str(self.height),
            "--",
            "--warptest-c1-request",
            str(self._request_path),
            "--warptest-c1-report",
            str(self._report_path),
            "--warptest-c1-ready",
            str(self._ready_path),
            "--warptest-c1-session",
            self.session_id,
        ]

    def _background_guard(self, editor: str) -> Path | None:
        """Build the test-only pre-main AppKit activation guard for Unity."""

        if sys.platform != "darwin":
            return None
        executable = Path(editor).expanduser().resolve()
        try:
            app = executable.parents[2]
        except IndexError:
            return None
        if app.suffix != ".app" or not app.is_dir():
            return None
        source = Path(__file__).with_name("c1_unity_background_guard.m")
        if not source.is_file():
            raise UnityC1ProtocolError(f"Unity background guard source is missing: {source}")
        output = self.out_dir / "unity-c1-background-guard.dylib"
        completed = subprocess.run(
            [
                "xcrun",
                "clang",
                "-fobjc-arc",
                "-dynamiclib",
                "-arch",
                "arm64",
                "-arch",
                "x86_64",
                "-framework",
                "AppKit",
                "-framework",
                "Foundation",
                str(source),
                "-o",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0 or not output.is_file():
            raise UnityC1ProtocolError(
                "cannot build Unity background activation guard: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        return output

    def start(self) -> "UnityC1Session":
        if self._started:
            return self
        self.out_dir.mkdir(parents=True, exist_ok=True)
        install = self._ensure_instrumentation()
        self.logs.append(
            f"{self.benchmark}_instrumentation={install.get('reason')}:{install.get('path')}"
        )
        for path in (self._request_path, self._report_path, self._ready_path):
            path.unlink(missing_ok=True)

        if self._acquire_native_lock:
            self._lock_context = native_runtime_lock()
            self._lock_context.__enter__()

        try:
            cmd = self._build_launch_command()
            self._background_window_guard.prepare()
            self.logs.append(f"unity_executable={cmd[0]}")
            guard = self._background_guard(cmd[0])
            launch_env = os.environ.copy()
            if guard is not None:
                inherited = launch_env.get("DYLD_INSERT_LIBRARIES", "").strip()
                launch_env["DYLD_INSERT_LIBRARIES"] = (
                    f"{guard}:{inherited}" if inherited else str(guard)
                )
                if self.benchmark == "jynew":
                    launch_env[
                        "WARPTEST_C1_UNITY_LEGACY_ORDER_WINDOW_GUARD"
                    ] = "1"
                self.logs.append("unity_launch_backend=dyld_appkit_background_guard_v1")
            else:
                self.logs.append("unity_launch_backend=direct")
            self._stdout_handle = self._stdout_path.open("w", encoding="utf-8")
            self._stderr_handle = self._stderr_path.open("w", encoding="utf-8")
            self._process = subprocess.Popen(
                cmd,
                cwd=str(self.repo),
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
                env=launch_env,
                start_new_session=True,
            )
            self._background_window_guard.start(self._process.pid)
            ready = self._wait_for_payload(
                self._ready_path,
                sequence=0,
                operation=None,
                timeout_s=self.startup_timeout_s,
            )
            if ready.get("status") != "ready":
                raise UnityC1ProtocolError(f"Unity C1 startup was not ready: {ready!r}")
            if ready.get("session_id") != self.session_id:
                raise UnityC1ProtocolError("Unity C1 ready session nonce mismatch")
            reported_pid = ready.get("pid")
            if not isinstance(reported_pid, int) or isinstance(reported_pid, bool) or reported_pid <= 0:
                raise UnityC1ProtocolError("Unity C1 ready payload has no valid PID")
            if reported_pid != self._process.pid:
                raise UnityC1ProtocolError(
                    f"Unity C1 ready PID {reported_pid} does not match launched PID {self._process.pid}"
                )
            guard = self._background_window_guard.await_hidden()
            self.logs.append(
                "unity_background_window_guard="
                + str(guard.get("mode") or "unavailable")
            )
            self._started = True
            self.logs.extend(
                [
                    str(self._unity_log_path),
                    str(self._stdout_path),
                    str(self._stderr_path),
                ]
            )
            return self
        except BaseException:
            self._terminate_process()
            self._release_resources()
            raise

    def focus(self) -> None:
        raise UnityC1ProtocolError("background Unity sessions must never request focus")

    def clean_entry(self) -> dict[str, Any]:
        """Return to the game's public startup scene without injecting Phase B."""

        report = self._request("clean_entry", spec={})
        report["phase"] = "clean_entry"
        report["actions_executed"] = False
        return report

    def start_clean(self) -> "UnityC1Session":
        """Compatibility entry point used by other native C1 session types."""

        self.start()
        self.clean_entry()
        return self

    def restore_target(
        self,
        spec: Mapping[str, Any],
        spec_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Restore only Phase A and leave the live process at the playable target.

        The double stripping (here and again in the C# operation handler) is a
        deliberate trust-boundary invariant: actions and goal assertions cannot
        accidentally become setup-side work.
        """

        utility = self._utility_spec(materialize_legacy_spec(spec))
        utility["actions"] = []
        utility["assertions"] = []
        payload: dict[str, Any] = {"spec": utility}
        if spec_path is not None:
            payload["spec_path"] = str(Path(spec_path).expanduser().resolve())
        report = self._request("restore_target", **payload)
        report["phase"] = "target_only"
        report["actions_executed"] = False
        return report

    def restore_target_only(
        self,
        spec: Mapping[str, Any],
        spec_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Alias matching the shared C1 web/desktop session vocabulary."""

        return self.restore_target(spec, spec_path)

    def semantic_start(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Read the live engine state and verify it still matches Phase A."""

        original = copy.deepcopy(dict(spec))
        transition_expectation = _save_load_transition_expectation(original)
        utility = self._utility_spec(materialize_legacy_spec(original))
        utility["actions"] = []
        utility["assertions"] = []
        report = self._request(
            "semantic_start",
            spec=utility,
            transition_expectation=_unity_bridge_transition_expectation(
                transition_expectation
            ),
        )
        return self._semantic_result(
            report,
            transition_expectation=transition_expectation,
            after=False,
        )

    def semantic_goal(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Evaluate the declared oracle against live state after public UI work."""

        original = copy.deepcopy(dict(spec))
        transition_expectation = _save_load_transition_expectation(original)
        utility = self._utility_spec(materialize_legacy_spec(original))
        utility["actions"] = []
        report = self._request(
            "semantic_goal",
            spec=utility,
            transition_expectation=_unity_bridge_transition_expectation(
                transition_expectation
            ),
        )
        return self._semantic_result(
            report,
            transition_expectation=transition_expectation,
            after=True,
        )

    def _transition_evidence_valid(
        self,
        report: Mapping[str, Any],
        expectation: Mapping[str, Any] | None,
        *,
        after: bool,
    ) -> bool:
        if expectation is None:
            evidence = report.get("transition_evidence")
            if not isinstance(evidence, Mapping):
                return False
            expected_source = f"{self.benchmark}_public_ui_save_load_witness_v1"
            checks = report.get("checks")
            transition_checks = [
                row
                for row in (checks if isinstance(checks, list) else [])
                if isinstance(row, Mapping)
                and str(row.get("name") or "").startswith("c1.transition.")
            ]
            common_valid = bool(
                evidence.get("required") is False
                and evidence.get("kind") in {"", None}
                and evidence.get("slot") == -1
                and evidence.get("source") == expected_source
                and evidence.get("armed_sequence") == -1
                and isinstance(evidence.get("observed_sequence"), int)
                and not isinstance(evidence.get("observed_sequence"), bool)
                and evidence.get("observed_sequence") == report.get("sequence")
                and evidence.get("save_observed") is False
                and evidence.get("load_observed") is False
                and evidence.get("save_frame") == -1
                and evidence.get("load_frame") == -1
                and evidence.get("ordered") is False
                and not transition_checks
            )
            if self.benchmark != "pal3":
                return common_valid
            return bool(
                common_valid
                and evidence.get("reset_observed") is False
                and evidence.get("scene_load_observed") is False
                and evidence.get("reset_frame") == -1
                and evidence.get("scene_load_frame") == -1
            )
        evidence = report.get("transition_evidence")
        if not isinstance(evidence, Mapping):
            return False
        expected_source = f"{self.benchmark}_public_ui_save_load_witness_v1"
        armed_sequence = evidence.get("armed_sequence")
        observed_sequence = evidence.get("observed_sequence")
        common_valid = bool(
            evidence.get("required") is True
            and evidence.get("kind") == expectation.get("kind")
            and evidence.get("slot") == expectation.get("slot")
            and evidence.get("source") == expected_source
            and isinstance(armed_sequence, int)
            and not isinstance(armed_sequence, bool)
            and armed_sequence >= 0
            and isinstance(observed_sequence, int)
            and not isinstance(observed_sequence, bool)
            and observed_sequence == report.get("sequence")
        )
        checks = report.get("checks")
        check_status = {
            str(row.get("name")): row.get("status")
            for row in (checks if isinstance(checks, list) else [])
            if isinstance(row, Mapping)
        }
        expected_check = "c1.transition.expectation" if after else "c1.transition.armed"
        if not common_valid or check_status.get(expected_check) != "success":
            return False
        save_frame = evidence.get("save_frame")
        load_frame = evidence.get("load_frame")
        save_observed = evidence.get("save_observed")
        load_observed = evidence.get("load_observed")
        if type(save_observed) is not bool or type(load_observed) is not bool:
            return False
        frames_valid = bool(
            isinstance(save_frame, int)
            and not isinstance(save_frame, bool)
            and save_frame >= 0
            if save_observed
            else save_frame == -1
        ) and bool(
            isinstance(load_frame, int)
            and not isinstance(load_frame, bool)
            and load_frame >= 0
            if load_observed
            else load_frame == -1
        )
        if not frames_valid:
            return False
        ordered = bool(
            save_observed
            and load_observed
            and isinstance(save_frame, int)
            and isinstance(load_frame, int)
            and load_frame >= save_frame
        )
        ordered_events = expectation.get("ordered_events")
        requires_reset_sequence = ordered_events == [
            "save",
            "reset",
            "scene_load",
            "load",
        ]
        if ordered_events not in (
            ["save", "load"],
            ["save", "reset", "scene_load", "load"],
        ):
            return False
        if requires_reset_sequence:
            reset_observed = evidence.get("reset_observed")
            scene_load_observed = evidence.get("scene_load_observed")
            reset_frame = evidence.get("reset_frame")
            scene_load_frame = evidence.get("scene_load_frame")
            if (
                type(reset_observed) is not bool
                or type(scene_load_observed) is not bool
            ):
                return False
            pal_frames_valid = bool(
                isinstance(reset_frame, int)
                and not isinstance(reset_frame, bool)
                and reset_frame >= 0
                if reset_observed
                else reset_frame == -1
            ) and bool(
                isinstance(scene_load_frame, int)
                and not isinstance(scene_load_frame, bool)
                and scene_load_frame >= 0
                if scene_load_observed
                else scene_load_frame == -1
            )
            if not pal_frames_valid:
                return False
            ordered = bool(
                ordered
                and reset_observed
                and scene_load_observed
                and reset_frame >= save_frame
                and scene_load_frame >= reset_frame
                and load_frame >= scene_load_frame
            )
        if evidence.get("ordered") is not ordered:
            return False
        if not after:
            return bool(not save_observed and not load_observed and not ordered)
        return bool(
            check_status.get("c1.transition.save")
            == ("success" if save_observed else "failure")
            and check_status.get("c1.transition.load")
            == ("success" if ordered else "failure")
        )

    def _semantic_result(
        self,
        report: dict[str, Any],
        *,
        transition_expectation: Mapping[str, Any] | None,
        after: bool,
    ) -> dict[str, Any]:
        checks = report.get("checks")
        check_rows = checks if isinstance(checks, list) else []
        transition_evidence_valid = self._transition_evidence_valid(
            report,
            transition_expectation,
            after=after,
        )
        ok = bool(
            report.get("status") == "success"
            and check_rows
            and transition_evidence_valid
            and all(
                isinstance(row, dict) and row.get("status") == "success"
                for row in check_rows
            )
        )
        report.update(
            ok=ok,
            live=True,
            source=self.semantic_source,
            semantic_source=self.semantic_source,
            transition_evidence_valid=transition_evidence_valid,
        )
        self._last_semantic_observation = {
            "source": self.semantic_source,
            "status": str(report.get("status") or ""),
            "ok": ok,
            "checks": copy.deepcopy(check_rows),
            "transition_evidence": copy.deepcopy(report.get("transition_evidence")),
            "transition_evidence_valid": transition_evidence_valid,
        }
        return report

    def seed_evidence(self, task_id: str) -> dict[str, Any]:
        """Return live process evidence for tasks with no engine-RNG contract.

        The four supported Unity C1 tasks are deterministic save/scene UI tasks;
        the experiment seed pairs arms but is not injected into Unity's global
        random generator.  Report that explicitly instead of fabricating a PRNG
        readback.
        """

        return {
            "schema_version": 1,
            "observed": self.is_alive(),
            "source": "unity_live_process",
            "algorithm": "not_applicable_unity_gui",
            "seed": self.experiment_seed,
            "process_id": self.hold_pid,
            "process_alive_at_observation": self.is_alive(),
            "benchmark": self.benchmark,
            "task_id": str(task_id),
            "protocol_version": C1_UNITY_PROTOCOL_VERSION,
            "sequence": self._sequence,
            "semantic_fingerprint": copy.deepcopy(self._last_semantic_observation),
        }

    def _require_full_frame_capture(self, report: Mapping[str, Any]) -> None:
        """Reject a capture that did not read back the whole GameView surface.

        ``frame_width``/``frame_height`` describe the PNG the policy sees and were
        historically hardcoded on both sides, which made the dimension check
        tautological and let a half-visible frame through for the whole catalog.
        ``capture_source_*`` is the full frame the hook actually captured. Jynew's
        camera path renders the policy frame at an exact fixed size; PAL3 verifies
        its whole GameView readback in the hook and may then downsample a larger
        source frame.
        """

        width = report.get("capture_source_width")
        height = report.get("capture_source_height")
        if not isinstance(width, int) or not isinstance(height, int):
            raise UnityC1ProtocolError(
                "Unity capture did not report a measured surface size; the "
                "instrumentation hook predates the full-frame capture guard"
            )
        if self.benchmark == "jynew" and (width, height) != (
            self.width,
            self.height,
        ):
            raise UnityC1ProtocolError(
                f"Jynew captured a {width}x{height} source instead of the exact "
                f"{self.width}x{self.height} camera-rendered policy frame"
            )
        if self.benchmark != "jynew" and (
            width < self.width or height < self.height
        ):
            raise UnityC1ProtocolError(
                f"Unity captured a {width}x{height} surface, which cannot fill the "
                f"{self.width}x{self.height} policy frame"
            )

    def _screenshot_destination(self, path: str | Path) -> Path:
        """Resolve where a capture is written.

        A bare filename means "beside this session's artifacts". A path with
        directories is caller-owned and stays relative to the cwd, as in every
        other C1 runtime; rebasing it onto ``out_dir`` would bury frames the
        caller addresses by their own repo-relative path.
        """

        destination = Path(path).expanduser()
        if not destination.is_absolute() and destination.parent == Path("."):
            destination = self.out_dir / destination
        return destination.resolve()

    def unity_screenshot(
        self, path: str | Path = "unity-c1.png", full_page: bool = False
    ) -> str:
        """Capture final GameView pixels, including overlay UI, through the bridge."""

        del full_page
        destination = self._screenshot_destination(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        frames = (self.out_dir / "background-frames").resolve()
        frames.mkdir(parents=True, exist_ok=True)
        self._capture_sequence += 1
        target = frames / f"frame-{self._capture_sequence:08d}.png"
        target.unlink(missing_ok=True)
        report = self._request("capture", screenshot_output_path=str(target))
        source = str(report.get("screenshot_source") or "")
        status = str(report.get("screenshot_status") or "")
        if (
            report.get("status") != "success"
            or status != "success"
            or source != "unity_gameview_capture"
            or not target.is_file()
            or not _screenshot_is_informative(target)
        ):
            detail = (
                report.get("screenshot_detail")
                or report.get("detail")
                or "unknown capture error"
            )
            self._last_screenshot_metadata = {
                "screenshot_status": "failure",
                "screenshot_source": "capture_failure",
                "screenshot_detail": str(detail),
                "live_source": "capture_failure",
            }
            target.unlink(missing_ok=True)
            raise RuntimeError(f"{self.benchmark} live Unity capture failed: {detail}")
        shutil.copyfile(target, destination)
        self._last_screenshot_path = str(destination)
        self._last_screenshot_metadata = {
            "screenshot_status": "success",
            "screenshot_source": "unity_gameview_capture",
            "screenshot_detail": str(report.get("screenshot_detail") or ""),
            "screenshot_path": str(destination),
            "live_source": "unity_gameview_capture",
        }
        frame_id = report.get("frame_id")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
            raise UnityC1ProtocolError("Unity capture returned an invalid frame id")
        if (report.get("frame_width"), report.get("frame_height")) != (
            self.width,
            self.height,
        ):
            raise UnityC1ProtocolError("Unity capture dimensions drifted from 1280x720")
        self._require_full_frame_capture(report)
        self.last_policy_frame_id = frame_id
        return str(destination)

    def screenshot(
        self, path: str | Path = "unity-c1-desktop.png", full_page: bool = False
    ) -> str:
        return self.unity_screenshot(path, full_page=full_page)

    def capture_policy_frame(self, path: str | Path) -> str:
        return self.unity_screenshot(path)

    def policy_frame_dimensions(self) -> tuple[int, int]:
        return self.width, self.height

    def dispatch_input_batch(self, batch: Any) -> dict[str, Any]:
        if getattr(batch, "session_id", None) != self.session_id:
            raise UnityC1ProtocolError("Unity input batch session mismatch")
        actions = []
        for action in getattr(batch, "actions", ()):
            value = action.to_dict()
            value["has_point"] = action.x is not None and action.y is not None
            actions.append(value)
        report = self._request(
            "input_batch",
            frame_id=getattr(batch, "frame_id", None),
            batch_id=getattr(batch, "batch_id", None),
            actions=actions,
        )
        if report.get("status") != "success" or report.get("accepted") is not True:
            raise UnityC1ProtocolError(
                f"Unity input batch rejected: {report.get('error') or report.get('detail')}"
            )
        return report

    def desktop_screenshot(self, path: str | Path = "unity-c1-desktop.png") -> str:
        return self.screenshot(path)

    def calibration_metadata(self) -> dict[str, Any]:
        self._require_started()
        return {
            "mode": "background_pixel",
            "benchmark": self.benchmark,
            "pid": self.pid,
            "frame_width": self.width,
            "frame_height": self.height,
            "protocol_version": C1_UNITY_PROTOCOL_VERSION,
            "screenshot_source": "unity_gameview_capture",
            "input_backend": "unity_editor_queue_gameview_input_v1",
            "foreground_required": False,
            "semantic_source": self.semantic_source,
            "background_window_guard": self.background_guard_metadata(),
        }

    def background_guard_metadata(self) -> dict[str, Any]:
        return self._background_window_guard.metadata()

    def _request(self, operation: str, **fields: Any) -> dict[str, Any]:
        self._require_started()
        allowed = {
            "clean_entry",
            "restore_target",
            "semantic_start",
            "semantic_goal",
            "capture",
            "input_batch",
            "close",
        }
        if operation not in allowed:
            raise ValueError(f"Unsupported Unity C1 operation: {operation!r}")
        self._sequence += 1
        sequence = self._sequence
        self._report_path.unlink(missing_ok=True)
        payload = {
            "version": C1_UNITY_PROTOCOL_VERSION,
            "sequence": sequence,
            "operation": operation,
            "session_id": self.session_id,
            **fields,
        }
        self._atomic_write_json(self._request_path, payload)
        report = self._wait_for_payload(
            self._report_path,
            sequence=sequence,
            operation=operation,
            timeout_s=self.request_timeout_s,
        )
        status = str(report.get("status") or "")
        if status not in {"success", "failure", "rejected", "engine_error"}:
            raise UnityC1ProtocolError(f"Invalid Unity C1 report status: {status!r}")
        # A synthesized target can be rejected by the bounded utility schema or
        # fail while the engine attempts to construct it.  That is treatment
        # evidence, not a broken IPC channel: return the signed report so the
        # caller can score the start construction as a normal fair failure.
        # The same statuses from clean-entry, capture, semantic grading, or
        # input dispatch still indicate that the registered runtime itself is
        # unavailable and therefore remain fail-closed protocol errors.
        if status in {"rejected", "engine_error"} and operation != "restore_target":
            raise UnityC1ProtocolError(
                f"Unity C1 {operation} failed closed ({status}): {report.get('detail', '')}"
            )
        return report

    def _wait_for_payload(
        self,
        path: Path,
        *,
        sequence: int,
        operation: str | None,
        timeout_s: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last_error = "file not present"
        while time.monotonic() < deadline:
            process = self._process
            if process is not None and process.poll() is not None:
                raise UnityC1ProtocolError(
                    f"Unity C1 process exited early with code {process.returncode}; see {self._unity_log_path}"
                )
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as error:
                last_error = str(error)
                time.sleep(0.05)
                continue
            if not isinstance(payload, dict):
                last_error = "JSON payload is not an object"
                time.sleep(0.05)
                continue
            if payload.get("version") != C1_UNITY_PROTOCOL_VERSION:
                raise UnityC1ProtocolError(
                    f"Unity C1 protocol mismatch in {path}: {payload.get('version')!r}"
                )
            if payload.get("sequence") != sequence:
                last_error = (
                    f"stale sequence {payload.get('sequence')!r}, expected {sequence}"
                )
                time.sleep(0.05)
                continue
            if operation is not None and payload.get("operation") != operation:
                raise UnityC1ProtocolError(
                    f"Unity C1 operation mismatch: {payload.get('operation')!r}, expected {operation!r}"
                )
            return payload
        raise TimeoutError(
            f"Timed out after {timeout_s:.1f}s waiting for Unity C1 {path.name}: {last_error}"
        )

    @staticmethod
    def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(temp, path)

    def _require_started(self) -> None:
        if (
            not self._started
            or self._process is None
            or self._process.poll() is not None
        ):
            raise RuntimeError(f"{self.benchmark} Unity C1 session is not running")

    def close(self) -> None:
        if self._started and self._process is not None and self._process.poll() is None:
            try:
                self._request("close")
            except Exception as error:
                self.logs.append(f"c1_close_warning={error}")
        self._terminate_process()
        self._restore_known_serialization_noise()
        self._release_resources()
        self._started = False
        self.native = None

    def _capture_serialization_guard(self) -> dict[Path, tuple[bytes, bytes]]:
        if self.benchmark != "jynew":
            return {}
        return capture_jynew_serialization_guard(self._project_path)

    def _restore_known_serialization_noise(self) -> None:
        restore_jynew_serialization_guard(
            self._serialization_guard, log=self.logs.append
        )

    def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            self._background_window_guard.close()
            return
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        self._process = None
        self._background_window_guard.close()

    def _release_resources(self) -> None:
        for handle_name in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, handle_name)
            if handle is not None:
                handle.close()
                setattr(self, handle_name, None)
        if self._lock_context is not None:
            self._lock_context.__exit__(None, None, None)
            self._lock_context = None


class JynewC1Session(UnityC1Session):
    benchmark = "jynew"
    execute_method = "Jyx2.WarptestCheckpoint.RunC1"
    semantic_source = "jynew_live_semantic_probe_v1"

    def _find_project_path(self) -> Path:
        return find_jynew_project_path(self.repo)

    def _ensure_instrumentation(self) -> Mapping[str, object]:
        return ensure_jynew_instrumentation(self.repo)

    def _find_editor(self) -> str:
        return _find_unity_executable()

    def _utility_spec(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        return _build_utility_spec(dict(spec))


class Pal3C1Session(UnityC1Session):
    benchmark = "pal3"
    execute_method = "Pal3.Game.Command.WarptestCheckpoint.RunC1"
    semantic_source = "pal3_live_semantic_probe_v1"

    def _find_project_path(self) -> Path:
        return find_pal3_project_path(self.repo)

    def _ensure_instrumentation(self) -> Mapping[str, object]:
        return ensure_pal3_instrumentation(self.repo)

    def _find_editor(self) -> str:
        return _find_pal3_unity_executable(self._project_path)

    def _preflight_editor(self, editor: str) -> None:
        project_version = _pal3_project_editor_version(self._project_path)
        editor_version = _pal3_unity_editor_version(editor)
        if project_version and editor_version and project_version != editor_version:
            raise RuntimeError(
                "Unity editor version mismatch for Pal3.Unity: "
                f"project requires {project_version}, but {editor} is {editor_version}."
            )

    def _utility_spec(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        return _build_pal3_utility_spec(dict(spec))


def create_c1_unity_session(
    benchmark: str,
    repo: str | Path,
    report_dir: str | Path,
    **kwargs: Any,
) -> UnityC1Session:
    """Create the bounded Unity session implementation for a C1 benchmark."""

    normalized = benchmark.strip().lower().replace("-", "").replace("_", "")
    if normalized == "jynew":
        return JynewC1Session(repo, report_dir, **kwargs)
    if normalized in {"pal3", "pal3unity"}:
        return Pal3C1Session(repo, report_dir, **kwargs)
    raise ValueError(f"Unsupported Unity C1 benchmark: {benchmark!r}")


# Descriptive aliases for callers that name the surface as well as the game.
JynewC1UnitySession = JynewC1Session
Pal3C1UnitySession = Pal3C1Session


__all__ = [
    "C1_UNITY_PROTOCOL_VERSION",
    "JynewC1Session",
    "JynewC1UnitySession",
    "Pal3C1Session",
    "Pal3C1UnitySession",
    "UnityC1ProtocolError",
    "UnityC1Session",
    "create_c1_unity_session",
]
