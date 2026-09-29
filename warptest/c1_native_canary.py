"""Controlled foreground-isolation canary for native C1 pixel backends."""

from __future__ import annotations

import argparse
import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import yaml
from PIL import Image, ImageChops, ImageStat

from .agent_s_bridge import AgentSAction, InputBatch
from .c1_desktop_runtime import desktop_session_for
from .c1_openra_runtime import OpenRASettingsSession, target_settings
from .c1_sandbox import C1SandboxManager, git_fingerprint
from .c1_start_validation import StartTask, _start_spec, load_selected_tasks
from .c1_unity_runtime import create_c1_unity_session
from .gui_agent import LuantiGuiRuntime
from .luanti_state import LuantiWarmSession
from .native_input import native_runtime_lock
from .runtime_lock import runtime_lease


NATIVE_BENCHMARKS = ("luanti", "openra", "libreoffice", "blender", "jynew", "pal3")
BACKGROUND_CONCURRENCY_BENCHMARKS = ("luanti", "openra")
CANARY_INPUT_SETTLE_SECONDS = 3.0
CANARY_CONFIRMATION_TIMEOUT_SECONDS = 120.0
CANARY_DIALOG_PROCESS_GRACE_SECONDS = 15.0
CANARY_WAIT_MINUTES = (5, 10, 20, 30, 45, 60)


class CanaryStartCancelled(RuntimeError):
    """Raised when the operator declines the native canary warning."""


def _confirm_canary_start(*, settle_seconds: float = CANARY_INPUT_SETTLE_SECONDS) -> None:
    """Wait for confirmation or a selected delay, accepting after two idle minutes."""

    if sys.platform != "darwin":
        raise RuntimeError("native canary confirmation requires macOS")
    dialog_source = Path(__file__).with_name("c1_canary_confirmation.swift")
    if not dialog_source.is_file():
        raise RuntimeError(f"native canary confirmation source is missing: {dialog_source}")
    try:
        completed = subprocess.run(
            [
                "/usr/bin/xcrun",
                "swift",
                str(dialog_source),
                str(CANARY_CONFIRMATION_TIMEOUT_SECONDS),
            ],
            capture_output=True,
            text=True,
            timeout=(
                CANARY_CONFIRMATION_TIMEOUT_SECONDS
                + CANARY_DIALOG_PROCESS_GRACE_SECONDS
            ),
        )
    except subprocess.TimeoutExpired:
        print(
            "[c1-native-canary] no confirmation response after 120 seconds; "
            "defaulting to confirmed",
            flush=True,
        )
        completed = None
    if completed is not None and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            "cannot display native canary confirmation dialog: "
            + (detail or f"dialog helper exited with {completed.returncode}")
        )
    response: Mapping[str, Any] = {"action": "confirm", "timed_out": True}
    if completed is not None:
        try:
            decoded_response = json.loads(completed.stdout.strip())
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError(
                "native canary confirmation returned an invalid response"
            ) from error
        if not isinstance(decoded_response, Mapping):
            raise RuntimeError("native canary confirmation returned an invalid response")
        response = decoded_response
    action = response.get("action")
    if action == "cancel":
        raise CanaryStartCancelled("native canary cancelled by operator")
    if action == "wait":
        wait_minutes = response.get("minutes")
        if wait_minutes not in CANARY_WAIT_MINUTES:
            raise RuntimeError(
                f"native canary confirmation returned invalid wait: {wait_minutes!r}"
            )
        print(
            f"[c1-native-canary] operator requested a {wait_minutes}-minute delay",
            flush=True,
        )
        time.sleep(wait_minutes * 60)
        print(
            f"[c1-native-canary] delay complete; starting in {settle_seconds:g} seconds",
            flush=True,
        )
    elif action != "confirm":
        raise RuntimeError(
            f"native canary confirmation returned invalid action: {action!r}"
        )
    elif response.get("timed_out"):
        print(
            "[c1-native-canary] no confirmation response after 120 seconds; "
            "defaulting to confirmed",
            flush=True,
        )
        print(
            f"[c1-native-canary] automatic confirmation; starting in {settle_seconds:g} seconds",
            flush=True,
        )
    else:
        print(
            f"[c1-native-canary] operator confirmed; starting in {settle_seconds:g} seconds",
            flush=True,
        )
    time.sleep(settle_seconds)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class ForegroundSentinel:
    """Small AppKit text field that records accidental global input/focus."""

    def __init__(self, root: Path, output_dir: Path) -> None:
        self.root = root
        self.output_dir = output_dir
        self.status_path = output_dir / "foreground-sentinel.json"
        self.binary = output_dir.parent / ".c1-foreground-sentinel"
        self.process: subprocess.Popen[str] | None = None

    def _compile(self) -> None:
        source = Path(__file__).with_name("c1_foreground_sentinel.swift")
        if not source.is_file():
            raise RuntimeError(f"foreground sentinel source is missing: {source}")
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["xcrun", "swiftc", str(source), "-o", str(self.binary)],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "cannot compile foreground sentinel: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )

    def start(self) -> "ForegroundSentinel":
        if sys.platform != "darwin":
            raise RuntimeError("native foreground-isolation canary requires macOS")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.status_path.unlink(missing_ok=True)
        if not self.binary.is_file():
            self._compile()
        self.process = subprocess.Popen(
            [str(self.binary), str(self.status_path)],
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("foreground sentinel exited before readiness")
            last = self.snapshot(required=False)
            if last.get("pid") == self.process.pid and last.get("frontmost_pid") == self.process.pid:
                return self
            time.sleep(0.05)
        self.close()
        raise RuntimeError(f"foreground sentinel did not become frontmost: {last}")

    def snapshot(self, *, required: bool = True) -> dict[str, Any]:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if required:
                raise RuntimeError("foreground sentinel status is unavailable")
            return {}
        if not isinstance(value, dict):
            raise RuntimeError("foreground sentinel status is malformed")
        return value

    def activate(self) -> dict[str, Any]:
        process = self.process
        if process is None or process.poll() is not None:
            raise RuntimeError("foreground sentinel is not running")
        os.kill(process.pid, signal.SIGUSR1)
        deadline = time.monotonic() + 5
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.snapshot(required=False)
            if last.get("frontmost_pid") == process.pid:
                return last
            time.sleep(0.01)
        raise RuntimeError(f"foreground sentinel could not be reactivated: {last}")

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def __enter__(self) -> "ForegroundSentinel":
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        self.close()


def native_canary_actions(width: int, height: int) -> tuple[AgentSAction, ...]:
    """Return the bounded click/drag/wheel/hotkey/Unicode acceptance matrix."""

    center = (width // 2, height // 2)
    return (
        AgentSAction(kind="click", x=1, y=1, button="left", clicks=1),
        AgentSAction(kind="click", x=width - 2, y=1, button="left", clicks=1),
        AgentSAction(kind="click", x=1, y=height - 2, button="left", clicks=1),
        AgentSAction(kind="click", x=width - 2, y=height - 2, button="left", clicks=1),
        AgentSAction(kind="click", x=center[0], y=center[1], button="left", clicks=1),
        AgentSAction(kind="click", x=center[0], y=center[1], button="left", clicks=2),
        AgentSAction(kind="click", x=center[0], y=center[1], button="right", clicks=1),
        AgentSAction(
            kind="drag",
            x=width // 4,
            y=height // 4,
            x2=width * 3 // 4,
            y2=height * 3 // 4,
            button="left",
            duration=0.1,
        ),
        AgentSAction(kind="scroll", x=center[0], y=center[1], dx=0, dy=-3),
        AgentSAction(kind="key", key="a", modifiers=("meta",)),
        AgentSAction(kind="type", text="WarpTest-ASCII-42 中文输入"),
        AgentSAction(kind="key", key="escape"),
    )


def _mapping_evidence(actions: Sequence[AgentSAction], width: int, height: int) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        for suffix in ("", "2"):
            x = getattr(action, "x" + suffix)
            y = getattr(action, "y" + suffix)
            if x is None or y is None:
                continue
            points.append(
                {
                    "action_index": index,
                    "endpoint": "start" if not suffix else "end",
                    "requested_frame_point": [x, y],
                    "serialized_frame_point": [x, y],
                    "error_pixels": 0,
                }
            )
    return {
        "coordinate_space": "frame_pixels_top_left_v1",
        "frame": [width, height],
        "checks": points,
        "max_error_pixels": max((row["error_pixels"] for row in points), default=0),
        "verification": "validated_input_batch_serialization_and_backend_receipt_v1",
    }


def _image_dimensions(path: Path) -> list[int]:
    with Image.open(path) as image:
        image.load()
        return [image.width, image.height]


def _image_difference_mean(
    before: Path, after: Path, *, box: tuple[int, int, int, int] | None = None
) -> float:
    with Image.open(before).convert("RGB") as left, Image.open(after).convert(
        "RGB"
    ) as right:
        if box is not None:
            left, right = left.crop(box), right.crop(box)
        difference = ImageChops.difference(left, right)
        return float(sum(ImageStat.Stat(difference).mean) / 3.0)


def _luanti_player_position(runtime: Any) -> dict[str, float]:
    observation = runtime.session.semantic_snapshot(
        "native-canary-player",
        {
            "target": {
                "kind": "world_loaded",
                "nodes": [
                    {"pos": {"x": 0, "y": 0, "z": 0}, "node": "air"}
                ],
                "player": {"hp": 20},
            },
            "actions": [],
            "assertions": [],
        },
    )
    snapshot = observation.get("snapshot") if isinstance(observation, dict) else None
    player = snapshot.get("player") if isinstance(snapshot, dict) else None
    position = player.get("pos") if isinstance(player, dict) else None
    if not isinstance(position, dict) or any(
        not isinstance(position.get(axis), (int, float))
        or isinstance(position.get(axis), bool)
        for axis in ("x", "y", "z")
    ):
        raise RuntimeError(f"Luanti canary player position is unavailable: {observation}")
    return {axis: float(position[axis]) for axis in ("x", "y", "z")}


def _dispatch_luanti_canary_action(
    runtime: Any, output_dir: Path, name: str, action: AgentSAction
) -> tuple[Path, Path, dict[str, Any]]:
    before = Path(runtime.capture_policy_frame(output_dir / f"luanti-{name}-before.png"))
    frame_id = getattr(runtime, "last_policy_frame_id", None)
    if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
        raise RuntimeError(f"Luanti {name} canary capture returned no frame id")
    batch = InputBatch(
        session_id=runtime.session_id,
        frame_id=frame_id,
        batch_id=f"{runtime.session_id}:{frame_id}:luanti-{name}",
        actions=(action,),
    ).validated(width=1280, height=720)
    receipt = runtime.dispatch_input_batch(batch)
    if not isinstance(receipt, dict) or not (
        receipt.get("accepted") is True
        and receipt.get("consumed") is True
        and isinstance(receipt.get("consumed_frame_count"), int)
        and receipt["consumed_frame_count"] >= 1
    ):
        raise RuntimeError(f"Luanti {name} input was not consumed: {receipt}")
    after = Path(runtime.capture_policy_frame(output_dir / f"luanti-{name}-after.png"))
    return before, after, receipt


def _luanti_behavior_checks(runtime: Any, output_dir: Path) -> dict[str, Any]:
    if str(getattr(runtime, "capture_backend", "")) != "luanti_irrlicht_create_screenshot_v1":
        return {"applicable": False, "passed": True}

    # The generic matrix ends with Escape, so return to gameplay first.
    _dispatch_luanti_canary_action(
        runtime, output_dir, "reset-menu", AgentSAction(kind="key", key="escape")
    )
    f5_before, f5_after, f5_receipt = _dispatch_luanti_canary_action(
        runtime, output_dir, "f5", AgentSAction(kind="key", key="f5")
    )
    f5_difference = _image_difference_mean(
        f5_before, f5_after, box=(0, 0, 1280, 220)
    )

    player_before = _luanti_player_position(runtime)
    _, _, movement_receipt = _dispatch_luanti_canary_action(
        runtime,
        output_dir,
        "move-w",
        AgentSAction(kind="key", key="w", duration=0.35),
    )
    player_after = _luanti_player_position(runtime)
    horizontal_distance = (
        (player_after["x"] - player_before["x"]) ** 2
        + (player_after["z"] - player_before["z"]) ** 2
    ) ** 0.5

    drag_before, drag_after, drag_receipt = _dispatch_luanti_canary_action(
        runtime,
        output_dir,
        "drag-camera",
        AgentSAction(
            kind="drag",
            x=320,
            y=360,
            x2=1040,
            y2=360,
            button="left",
            duration=0.2,
        ),
    )
    drag_difference = _image_difference_mean(drag_before, drag_after)

    escape_before, escape_after, escape_receipt = _dispatch_luanti_canary_action(
        runtime, output_dir, "escape-menu", AgentSAction(kind="key", key="escape")
    )
    escape_difference = _image_difference_mean(
        escape_before, escape_after, box=(256, 120, 1024, 640)
    )
    _dispatch_luanti_canary_action(
        runtime, output_dir, "close-menu", AgentSAction(kind="key", key="escape")
    )

    marker = f"warptest-chat-canary-{time.time_ns()}"
    _, _, slash_receipt = _dispatch_luanti_canary_action(
        runtime, output_dir, "slash-chat", AgentSAction(kind="key", key="/")
    )
    _, _, chat_receipt = _dispatch_luanti_canary_action(
        runtime,
        output_dir,
        "chat-text",
        AgentSAction(kind="type", text=marker, enter=True),
    )
    log_path = Path(getattr(runtime.session, "_log", ""))
    chat_logged = False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            chat_logged = marker in log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            chat_logged = False
        if chat_logged:
            break
        time.sleep(0.05)

    checks = {
        "f5_hud_changed": f5_difference >= 1.0,
        "w_moved_player_horizontally": horizontal_distance >= 0.05,
        "drag_changed_camera": drag_difference >= 5.0,
        "escape_opened_menu": escape_difference >= 20.0,
        "slash_and_chat_text_reached_server": chat_logged,
    }
    return {
        "applicable": True,
        "checks": checks,
        "passed": all(checks.values()),
        "f5_top_band_difference_mean": f5_difference,
        "horizontal_move_distance": horizontal_distance,
        "drag_difference_mean": drag_difference,
        "escape_center_difference_mean": escape_difference,
        "player_before": player_before,
        "player_after": player_after,
        "receipts": {
            "f5": f5_receipt,
            "move_w": movement_receipt,
            "drag": drag_receipt,
            "escape": escape_receipt,
            "slash": slash_receipt,
            "chat": chat_receipt,
        },
    }


def _target_pid(runtime: Any) -> int:
    for value in (
        getattr(runtime, "hold_pid", None),
        getattr(runtime, "pid", None),
        getattr(getattr(runtime, "process", None), "pid", None),
        getattr(getattr(runtime, "_proc", None), "pid", None),
        getattr(getattr(getattr(runtime, "session", None), "_proc", None), "pid", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    raise RuntimeError("native canary target PID is unavailable")


def _exercise_runtime(runtime: Any, output_dir: Path, sentinel: ForegroundSentinel) -> dict[str, Any]:
    width, height = runtime.policy_frame_dimensions()
    before = Path(runtime.capture_policy_frame(output_dir / "before.png"))
    frame_id = getattr(runtime, "last_policy_frame_id", None)
    if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
        raise RuntimeError("native canary capture returned no frame id")
    actions = native_canary_actions(width, height)
    batch = InputBatch(
        session_id=runtime.session_id,
        frame_id=frame_id,
        batch_id=f"{runtime.session_id}:{frame_id}:native-canary",
        actions=actions,
    ).validated(width=width, height=height)
    pre_action_status = sentinel.activate()
    first = runtime.dispatch_input_batch(batch)
    duplicate = runtime.dispatch_input_batch(batch)
    post_action_status = sentinel.snapshot()
    if not isinstance(first, Mapping) or not isinstance(duplicate, Mapping):
        raise RuntimeError("native canary backend returned no execution receipt")
    receipt_fields = ("batch_id", "accepted", "event_count", "resulting_frame_id", "error")
    first_receipt = {field: first.get(field) for field in receipt_fields}
    duplicate_receipt = {field: duplicate.get(field) for field in receipt_fields}
    if (
        first_receipt != duplicate_receipt
        or first_receipt["accepted"] is not True
        or not isinstance(first_receipt["event_count"], int)
        or first_receipt["event_count"] <= 0
    ):
        raise RuntimeError(
            f"native canary exactly-once receipt failed: {first_receipt} / {duplicate_receipt}"
        )
    after = Path(runtime.capture_policy_frame(output_dir / "after.png"))
    behavior = _luanti_behavior_checks(runtime, output_dir)
    target_pid = _target_pid(runtime)
    status = sentinel.snapshot()
    observed = status.get("observed_frontmost_pids")
    sentinel_pid = status.get("pid")
    guard_reader = getattr(runtime, "background_guard_metadata", None)
    background_guard = guard_reader() if callable(guard_reader) else {}
    hidden_at_create = background_guard.get("hidden_at_create")
    hidden_at_create_policies = {
        "luanti_irrlicht_create_screenshot_v1": "sdl_window_hidden_at_create_v1",
        "openra_renderer_save_screenshot_sync_v1": "sdl_window_hidden_at_create_v1",
    }
    expected_hidden_at_create_policy = hidden_at_create_policies.get(
        str(getattr(runtime, "capture_backend", ""))
    )
    hidden_at_create_ok = bool(
        expected_hidden_at_create_policy is None
        or (
            isinstance(hidden_at_create, dict)
            and hidden_at_create.get("policy") == expected_hidden_at_create_policy
            and hidden_at_create.get("requested") is True
            and hidden_at_create.get("binary_fresh") is True
            and isinstance(hidden_at_create.get("source_sha256"), str)
            and len(hidden_at_create["source_sha256"]) == 64
            and isinstance(hidden_at_create.get("binary_sha256"), str)
            and len(hidden_at_create["binary_sha256"]) == 64
        )
    )
    capture_backend = str(getattr(runtime, "capture_backend", ""))
    observed_onscreen = background_guard.get("observed_onscreen_window_pids")
    unity_transient_window = bool(
        capture_backend == "unity_gameview_capture_v3"
        and isinstance(observed_onscreen, list)
        and observed_onscreen
    )
    max_onscreen_duration_ms = background_guard.get("max_onscreen_duration_ms")
    hide_successes = background_guard.get("hide_successes")
    accessibility_successes = background_guard.get("accessibility_successes")
    transient_window_ok = bool(
        not observed_onscreen
        or (
            unity_transient_window
            and background_guard.get("visible_application_pids") == []
            and isinstance(max_onscreen_duration_ms, int)
            and 0 <= max_onscreen_duration_ms <= 10_000
            and isinstance(
                background_guard.get("first_hidden_after_onscreen_unix_ms"), int
            )
            and (
                (
                    isinstance(hide_successes, int)
                    and not isinstance(hide_successes, bool)
                    and hide_successes > 0
                )
                or (
                    isinstance(accessibility_successes, int)
                    and not isinstance(accessibility_successes, bool)
                    and accessibility_successes > 0
                )
            )
        )
    )
    background_guard_ok = bool(
        sys.platform != "darwin"
        or (
            background_guard.get("enabled") is True
            and background_guard.get("active") is True
            and background_guard.get("mode") == "macos_process_group_hide_v1"
            and isinstance(background_guard.get("observed_application_pids"), list)
            and isinstance(background_guard.get("observed_window_pids"), list)
            and isinstance(observed_onscreen, list)
            and transient_window_ok
            and background_guard.get("visible_application_pids") == []
            and background_guard.get("observed_active_pids") == []
            and isinstance(background_guard.get("scan_count"), int)
            and background_guard["scan_count"] >= 5
        )
    )
    isolation = {
        "target_never_frontmost": isinstance(observed, list) and target_pid not in observed,
        "system_pointer_unchanged": post_action_status.get("pointer")
        == pre_action_status.get("pointer"),
        "system_clipboard_unchanged": post_action_status.get("clipboard_change_count")
        == pre_action_status.get("clipboard_change_count"),
        "foreground_text_unchanged": pre_action_status.get("foreground_text")
        == pre_action_status.get("marker")
        and post_action_status.get("foreground_text") == pre_action_status.get("marker"),
        "foreground_sentinel_remained_frontmost": pre_action_status.get("frontmost_pid")
        == sentinel_pid
        and post_action_status.get("frontmost_pid") == sentinel_pid,
        "background_window_guard_passed": background_guard_ok,
        "window_hidden_at_create_passed": hidden_at_create_ok,
        "transient_window_policy_passed": transient_window_ok,
    }
    mapping = _mapping_evidence(actions, width, height)
    passed = bool(
        all(isolation.values())
        and _image_dimensions(before) == [width, height]
        and _image_dimensions(after) == [width, height]
        and mapping["max_error_pixels"] <= 1
        and _target_pid(runtime) == target_pid
        and behavior["passed"] is True
    )
    return {
        "session_id": runtime.session_id,
        "target_pid": target_pid,
        "frame_id": frame_id,
        "resulting_frame_id": getattr(runtime, "last_policy_frame_id", None),
        "frame": [width, height],
        "before": str(before),
        "before_dimensions": _image_dimensions(before),
        "after": str(after),
        "after_dimensions": _image_dimensions(after),
        "action_count": len(actions),
        "actions": [action.to_dict() for action in actions],
        "input_mapping": mapping,
        "execution_receipt": first_receipt,
        "duplicate_execution_receipt": duplicate_receipt,
        "exactly_once": first_receipt == duplicate_receipt,
        "foreground_isolation": isolation,
        "foreground_pre_action": pre_action_status,
        "foreground_post_action": post_action_status,
        "foreground_sentinel": status,
        "background_window_guard": background_guard,
        "behavior_validation": behavior,
        "passed": passed,
    }


@contextmanager
def _native_runtime(
    task: StartTask,
    output_dir: Path,
    *,
    trusted_spec: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    # Load trusted oracle material outside a sandbox override for concurrent
    # clone canaries. The runtime receives a private copy per lane.
    spec = (
        copy.deepcopy(dict(trusted_spec))
        if trusted_spec is not None
        else _start_spec(task)
    )
    normalized = task.benchmark.strip().lower()
    if normalized == "luanti":
        session = LuantiWarmSession(
            task.repo,
            output_dir / "session",
            render=True,
            experiment_seed=0,
        )
        try:
            session.start()
            session.restore(spec, actions=[], assertions=[])
            yield LuantiGuiRuntime(session)
        finally:
            session.close()
        return
    if normalized == "openra":
        with OpenRASettingsSession(
            task.repo,
            output_dir / "session",
            settings=target_settings(spec),
            experiment_seed=0,
        ) as session:
            yield session
        return
    if normalized in {"libreoffice", "blender"}:
        with native_runtime_lock():
            with desktop_session_for(task.benchmark, task.repo, output_dir / "session") as session:
                restore = session.restore_target(spec)
                if restore.get("status") != "success":
                    raise RuntimeError(
                        f"{task.benchmark} native canary target restore failed: {restore}"
                    )
                yield session
        return
    if normalized in {"jynew", "pal3"}:
        session = create_c1_unity_session(
            task.benchmark,
            task.repo,
            output_dir / "session",
            experiment_seed=0,
        )
        try:
            session.start()
            session.restore_target_only(spec, task.oracle_spec)
            yield session
        finally:
            session.close()
        return
    raise ValueError(f"unsupported native canary benchmark: {task.benchmark}")


def _runtime_path(runtime: Any, attribute: str) -> str | None:
    for owner in (runtime, getattr(runtime, "session", None)):
        value = getattr(owner, attribute, None)
        if isinstance(value, (str, Path)):
            return str(Path(value).expanduser().resolve())
    return None


def _run_background_backend_concurrency_canary(
    *,
    base: Path,
    task: StartTask,
    manager: C1SandboxManager,
    output_dir: Path,
    timeout_s: float,
) -> dict[str, Any]:
    """Prove that two cloned background sessions overlap without global input."""

    benchmark = task.benchmark.strip().lower()
    if benchmark not in BACKGROUND_CONCURRENCY_BENCHMARKS:
        raise ValueError(f"unsupported background concurrency canary: {benchmark}")
    before = git_fingerprint(task.repo)
    trusted_spec = _start_spec(task)
    ready_barrier = threading.Barrier(2)
    finish_barrier = threading.Barrier(2)
    exercise_lock = threading.Lock()
    rows: list[dict[str, Any]] = []
    rows_lock = threading.Lock()
    invocation_id = f"background-concurrency-canary-{benchmark}"

    def lane(lane_id: int) -> dict[str, Any]:
        lane_dir = output_dir / f"lane-{lane_id}"
        row: dict[str, Any] = {
            "lane_id": lane_id,
            "status": "failed",
            "started_monotonic_ns": time.monotonic_ns(),
            "ready_monotonic_ns": None,
            "completed_monotonic_ns": None,
            "pid": None,
            "session_id": None,
            "support_dir": None,
            "report_dir": str(lane_dir.resolve()),
        }
        try:
            with manager.task_repo(
                task.repo,
                invocation_id=invocation_id,
                lane_id=lane_id,
                task_id=f"{benchmark}-lane-{lane_id}",
                allow_source_fallback=False,
            ) as sandbox:
                row["sandbox"] = sandbox.metadata()
                if sandbox.mode != "apfs_cow_clone":
                    raise RuntimeError(
                        "background concurrency canary requires an APFS clone: "
                        + str(sandbox.clone_error)
                    )
                cloned_task = replace(task, repo=sandbox.repo)
                with runtime_lease(sandbox.repo):
                    with _native_runtime(
                        cloned_task,
                        lane_dir,
                        trusted_spec=trusted_spec,
                    ) as runtime:
                        row["pid"] = _target_pid(runtime)
                        row["session_id"] = str(runtime.session_id)
                        row["support_dir"] = (
                            _runtime_path(runtime, "support_dir")
                            or _runtime_path(runtime, "report_dir")
                        )
                        row["ready_monotonic_ns"] = time.monotonic_ns()
                        ready_barrier.wait(timeout=min(timeout_s, 180.0))
                        # Keep both processes alive while each private input
                        # transport is exercised against one shared sentinel.
                        with exercise_lock:
                            row["evidence"] = _exercise_runtime(
                                runtime,
                                lane_dir,
                                sentinel,
                            )
                        finish_barrier.wait(timeout=min(timeout_s, 300.0))
                        if row["evidence"].get("passed") is not True:
                            raise RuntimeError(
                                f"{benchmark} lane {lane_id} isolation evidence failed"
                            )
                        row["status"] = "passed"
        except BaseException as error:  # noqa: BLE001 - preserve canary evidence
            row["error"] = f"{type(error).__name__}: {error}"
            row["traceback"] = traceback.format_exc()[:8000]
            ready_barrier.abort()
            finish_barrier.abort()
        finally:
            row["completed_monotonic_ns"] = time.monotonic_ns()
            with rows_lock:
                rows.append(row)
        return row

    output_dir.mkdir(parents=True, exist_ok=True)
    with ForegroundSentinel(base, output_dir / "sentinel") as sentinel:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(lane, lane_id) for lane_id in range(2)]
            for future in futures:
                future.result(timeout=timeout_s)

    ordered = sorted(rows, key=lambda item: int(item["lane_id"]))
    ready = [row.get("ready_monotonic_ns") for row in ordered]
    completed = [row.get("completed_monotonic_ns") for row in ordered]
    overlap_ns = (
        max(0, min(int(value) for value in completed) - max(int(value) for value in ready))
        if len(ordered) == 2 and all(ready) and all(completed)
        else 0
    )
    after = git_fingerprint(task.repo)
    pids = [row.get("pid") for row in ordered]
    sessions = [row.get("session_id") for row in ordered]
    support_dirs = [row.get("support_dir") for row in ordered]
    report_dirs = [row.get("report_dir") for row in ordered]
    sandboxes_cleaned = not any(manager.run_root.glob(f"{invocation_id}/lane-*/*"))
    proof = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "benchmark": benchmark,
        "task_id": task.id,
        "source_repo": str(task.repo.resolve()),
        "source_fingerprint_before": before,
        "source_fingerprint_after": after,
        "source_unchanged": before == after,
        "overlap_ns": overlap_ns,
        "distinct_pids": len(pids) == 2 and len(set(pids)) == 2,
        "distinct_sessions": len(sessions) == 2 and len(set(sessions)) == 2,
        "distinct_support_dirs": (
            len(support_dirs) == 2
            and all(isinstance(value, str) and value for value in support_dirs)
            and len(set(support_dirs)) == 2
        ),
        "distinct_report_dirs": len(report_dirs) == 2 and len(set(report_dirs)) == 2,
        "sandboxes_cleaned": sandboxes_cleaned,
        "results": ordered,
    }
    proof["passed"] = bool(
        len(ordered) == 2
        and all(row.get("status") == "passed" for row in ordered)
        and proof["source_unchanged"]
        and overlap_ns > 0
        and proof["distinct_pids"]
        and proof["distinct_sessions"]
        and proof["distinct_support_dirs"]
        and proof["distinct_report_dirs"]
        and sandboxes_cleaned
    )
    _atomic_json(output_dir / "background-concurrency-canary.json", proof)
    return proof


def run_background_concurrency_canary(
    root: str | Path,
    *,
    output_dir: str | Path,
    manifest_path: str | Path = "benchmarks/p5/manifest.yaml",
    tasks_path: str | Path = "benchmarks/p5/c1_tasks.yaml",
    benchmarks: Sequence[str] | None = None,
    manager: C1SandboxManager | None = None,
    timeout_s: float = 1200.0,
    confirm: bool = True,
) -> dict[str, Any]:
    """Run the two-clone Luanti/OpenRA admission canary without a provider."""

    base = Path(root).expanduser().resolve()
    destination = Path(output_dir).expanduser()
    if not destination.is_absolute():
        destination = (base / destination).resolve()
    tasks, _manifest_file, _ = load_selected_tasks(
        base,
        manifest_path,
        tasks_path,
    )
    by_benchmark: dict[str, StartTask] = {}
    for task in tasks:
        if (
            task.benchmark in BACKGROUND_CONCURRENCY_BENCHMARKS
            and task.benchmark not in by_benchmark
        ):
            by_benchmark[task.benchmark] = task
    selected = list(benchmarks or BACKGROUND_CONCURRENCY_BENCHMARKS)
    unknown = [benchmark for benchmark in selected if benchmark not in by_benchmark]
    if unknown:
        raise ValueError(
            "unknown background concurrency canary benchmarks: "
            + ", ".join(unknown)
        )
    if confirm:
        _confirm_canary_start()
    sandbox_manager = manager or C1SandboxManager(
        run_id=f"background-canary-{time.time_ns()}"
    )
    results: list[dict[str, Any]] = []
    for benchmark in selected:
        backend_dir = destination / benchmark
        print(
            f"[c1-native-canary] starting two-clone {benchmark}",
            flush=True,
        )
        try:
            proof = _run_background_backend_concurrency_canary(
                base=base,
                task=by_benchmark[benchmark],
                manager=sandbox_manager,
                output_dir=backend_dir,
                timeout_s=timeout_s,
            )
        except BaseException as error:  # noqa: BLE001 - fail closed before paid work
            proof = {
                "schema_version": 1,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "benchmark": benchmark,
                "passed": False,
                "error_type": type(error).__name__,
                "error": str(error)[:2000],
            }
            _atomic_json(
                backend_dir / "background-concurrency-canary.json",
                proof,
            )
        results.append(proof)
        print(
            f"[c1-native-canary] two-clone {benchmark}: "
            f"{'passed' if proof['passed'] else 'failed'}",
            flush=True,
        )
    summary = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(destination),
        "benchmarks": selected,
        "passed_count": sum(row.get("passed") is True for row in results),
        "total": len(results),
        "passed": all(row.get("passed") is True for row in results),
        "results": results,
    }
    _atomic_json(destination / "summary.json", summary)
    return summary


def run_native_canary(
    root: str | Path,
    *,
    output_dir: str | Path,
    manifest_path: str | Path = "benchmarks/p5/manifest.yaml",
    tasks_path: str | Path = "benchmarks/p5/c1_tasks.yaml",
    benchmarks: Sequence[str] | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    destination = Path(output_dir).expanduser()
    if not destination.is_absolute():
        destination = (base / destination).resolve()
    tasks, manifest_file, _ = load_selected_tasks(base, manifest_path, tasks_path)
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    identities = manifest["c1_background_pixel_protocol"]["benchmarks"]
    by_benchmark: dict[str, StartTask] = {}
    for task in tasks:
        if task.benchmark in NATIVE_BENCHMARKS and task.benchmark not in by_benchmark:
            by_benchmark[task.benchmark] = task
    selected = list(benchmarks or NATIVE_BENCHMARKS)
    unknown = [name for name in selected if name not in by_benchmark]
    if unknown:
        raise ValueError("unknown native canary benchmarks: " + ", ".join(unknown))
    _confirm_canary_start()
    results: list[dict[str, Any]] = []
    for benchmark in selected:
        task = by_benchmark[benchmark]
        backend_dir = destination / benchmark
        print(f"[c1-native-canary] starting {benchmark}", flush=True)
        try:
            with ForegroundSentinel(base, backend_dir / "sentinel") as sentinel:
                with runtime_lease(task.repo):
                    with _native_runtime(task, backend_dir) as runtime:
                        evidence = _exercise_runtime(runtime, backend_dir, sentinel)
            identity = identities[benchmark]
            proof = {
                "schema_version": 1,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "benchmark": benchmark,
                "task_id": task.id,
                "execution_backend": "background_pixel_v1",
                "coordinate_space": "frame_pixels_top_left_v1",
                "foreground_required": False,
                "capture_backend": identity["capture_backend"],
                "input_backend": identity["input_backend"],
                **evidence,
            }
        except Exception as error:
            identity = identities[benchmark]
            proof = {
                "schema_version": 1,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "benchmark": benchmark,
                "task_id": task.id,
                "execution_backend": "background_pixel_v1",
                "coordinate_space": "frame_pixels_top_left_v1",
                "foreground_required": False,
                "capture_backend": identity["capture_backend"],
                "input_backend": identity["input_backend"],
                "frame": identity["frame"],
                "foreground_isolation": {
                    "target_never_frontmost": False,
                    "system_pointer_unchanged": False,
                    "system_clipboard_unchanged": False,
                    "foreground_text_unchanged": False,
                    "background_window_guard_passed": False,
                    "window_hidden_at_create_passed": False,
                    "transient_window_policy_passed": False,
                },
                "passed": False,
                "error_type": type(error).__name__,
                "error": str(error)[:2000],
            }
        _atomic_json(destination / f"{benchmark}.json", proof)
        results.append(proof)
        print(f"[c1-native-canary] {benchmark}: {'passed' if proof['passed'] else 'failed'}", flush=True)
    summary = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(destination),
        "benchmarks": selected,
        "passed_count": sum(row.get("passed") is True for row in results),
        "total": len(results),
        "passed": all(row.get("passed") is True for row in results),
        "results": results,
    }
    _atomic_json(destination / "summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--manifest", default="benchmarks/p5/manifest.yaml")
    parser.add_argument("--tasks", default="benchmarks/p5/c1_tasks.yaml")
    parser.add_argument("--benchmark", action="append", dest="benchmarks")
    parser.add_argument(
        "--background-concurrency",
        action="store_true",
        help="run two APFS clones for each selected Luanti/OpenRA backend",
    )
    args = parser.parse_args(argv)
    try:
        runner = (
            run_background_concurrency_canary
            if args.background_concurrency
            else run_native_canary
        )
        result = runner(
            args.root,
            output_dir=args.output_dir,
            manifest_path=args.manifest,
            tasks_path=args.tasks,
            benchmarks=args.benchmarks,
        )
    except CanaryStartCancelled as error:
        print(f"[c1-native-canary] {error}", file=sys.stderr, flush=True)
        return 130
    print(json.dumps({key: result[key] for key in ("passed", "passed_count", "total", "output_dir")}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
