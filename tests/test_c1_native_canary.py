import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from warptest.c1_native_canary import (
    CANARY_CONFIRMATION_TIMEOUT_SECONDS,
    CANARY_DIALOG_PROCESS_GRACE_SECONDS,
    CanaryStartCancelled,
    _confirm_canary_start,
    _exercise_runtime,
    _luanti_behavior_checks,
    native_canary_actions,
)


def test_canary_confirmation_waits_for_ok_and_input_settle_delay() -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout='{"action":"confirm","minutes":0,"timed_out":false}\n',
        stderr="",
    )
    with (
        patch("warptest.c1_native_canary.sys.platform", "darwin"),
        patch("warptest.c1_native_canary.subprocess.run", return_value=completed) as run,
        patch("warptest.c1_native_canary.time.sleep") as sleep,
    ):
        _confirm_canary_start(settle_seconds=3.0)

    command = run.call_args.args[0]
    assert command[:2] == ["/usr/bin/xcrun", "swift"]
    assert command[2].endswith("c1_canary_confirmation.swift")
    assert command[3] == str(CANARY_CONFIRMATION_TIMEOUT_SECONDS)
    assert run.call_args.kwargs["timeout"] == (
        CANARY_CONFIRMATION_TIMEOUT_SECONDS + CANARY_DIALOG_PROCESS_GRACE_SECONDS
    )
    sleep.assert_called_once_with(3.0)


def test_canary_confirmation_cancel_aborts_without_settle_delay() -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout='{"action":"cancel","minutes":0,"timed_out":false}\n',
        stderr="",
    )
    with (
        patch("warptest.c1_native_canary.sys.platform", "darwin"),
        patch("warptest.c1_native_canary.subprocess.run", return_value=completed),
        patch("warptest.c1_native_canary.time.sleep") as sleep,
        pytest.raises(CanaryStartCancelled),
    ):
        _confirm_canary_start()

    sleep.assert_not_called()


@pytest.mark.parametrize("wait_minutes", [5, 10, 20, 30, 45, 60])
def test_canary_confirmation_waits_for_selected_delay(wait_minutes: int) -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout=(
            f'{{"action":"wait","minutes":{wait_minutes},"timed_out":false}}\n'
        ),
        stderr="",
    )
    with (
        patch("warptest.c1_native_canary.sys.platform", "darwin"),
        patch("warptest.c1_native_canary.subprocess.run", return_value=completed),
        patch("warptest.c1_native_canary.time.sleep") as sleep,
    ):
        _confirm_canary_start(settle_seconds=3.0)

    assert sleep.call_args_list == [
        ((wait_minutes * 60,),),
        ((3.0,),),
    ]


def test_canary_confirmation_rejects_unknown_wait_delay() -> None:
    completed = SimpleNamespace(
        returncode=0,
        stdout='{"action":"wait","minutes":15,"timed_out":false}\n',
        stderr="",
    )
    with (
        patch("warptest.c1_native_canary.sys.platform", "darwin"),
        patch("warptest.c1_native_canary.subprocess.run", return_value=completed),
        patch("warptest.c1_native_canary.time.sleep") as sleep,
        pytest.raises(RuntimeError, match="invalid wait"),
    ):
        _confirm_canary_start()

    sleep.assert_not_called()


def test_canary_confirmation_timeout_defaults_to_confirm_and_settles(capsys) -> None:
    with (
        patch("warptest.c1_native_canary.sys.platform", "darwin"),
        patch(
            "warptest.c1_native_canary.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd="swift",
                timeout=(
                    CANARY_CONFIRMATION_TIMEOUT_SECONDS
                    + CANARY_DIALOG_PROCESS_GRACE_SECONDS
                ),
            ),
        ),
        patch("warptest.c1_native_canary.time.sleep") as sleep,
    ):
        _confirm_canary_start(settle_seconds=3.0)

    sleep.assert_called_once_with(3.0)
    output = capsys.readouterr().out
    assert "defaulting to confirmed" in output
    assert "automatic confirmation" in output


def test_native_canary_matrix_covers_required_low_level_actions() -> None:
    actions = native_canary_actions(1280, 720)
    assert len(actions) == 12
    assert {(action.x, action.y) for action in actions[:5]} == {
        (1, 1),
        (1278, 1),
        (1, 718),
        (1278, 718),
        (640, 360),
    }
    assert any(action.kind == "click" and action.clicks == 2 for action in actions)
    assert any(action.kind == "click" and action.button == "right" for action in actions)
    assert any(action.kind == "drag" for action in actions)
    assert any(action.kind == "scroll" for action in actions)
    assert any(action.kind == "key" and action.modifiers == ("meta",) for action in actions)
    assert any(action.kind == "type" and "中文" in (action.text or "") for action in actions)


@patch("warptest.c1_native_canary.sys.platform", "darwin")
def test_exercise_runtime_requires_exactly_once_receipt_and_isolation(tmp_path: Path) -> None:
    class Runtime:
        session_id = "canary-session"
        capture_backend = "fixture_capture"
        input_backend = "fixture_input"
        pid = 4242

        def __init__(self) -> None:
            self.last_policy_frame_id = 0

        guard = {
            "enabled": True,
            "active": True,
            "mode": "macos_process_group_hide_v1",
            "observed_application_pids": [4242],
            "observed_window_pids": [4242],
            "observed_onscreen_window_pids": [],
            "observed_active_pids": [],
            "visible_application_pids": [],
            "hidden_application_pids": [4242],
            "scan_count": 5,
        }

        @staticmethod
        def policy_frame_dimensions() -> tuple[int, int]:
            return 1280, 720

        def capture_policy_frame(self, path: Path) -> str:
            self.last_policy_frame_id += 1
            Image.new("RGB", (1280, 720), (20, 40, 80)).save(path)
            return str(path)

        @staticmethod
        def dispatch_input_batch(batch):
            return {
                "batch_id": batch.batch_id,
                "accepted": True,
                "event_count": 41,
                "resulting_frame_id": batch.frame_id + 1,
                "error": "",
            }

        @staticmethod
        def background_guard_metadata() -> dict:
            return dict(Runtime.guard)

    class Sentinel:
        @staticmethod
        def snapshot() -> dict:
            return {
                "pid": 111,
                "frontmost_pid": 111,
                "observed_frontmost_pids": [111],
                "pointer": [10.0, 20.0],
                "initial_pointer": [10.0, 20.0],
                "clipboard_change_count": 5,
                "initial_clipboard_change_count": 5,
                "foreground_text": "marker",
                "marker": "marker",
            }

        @staticmethod
        def activate() -> dict:
            return Sentinel.snapshot()

    result = _exercise_runtime(Runtime(), tmp_path, Sentinel())  # type: ignore[arg-type]
    assert result["passed"] is True
    assert result["foreground_isolation"]["background_window_guard_passed"] is True
    assert result["exactly_once"] is True
    assert result["execution_receipt"] == result["duplicate_execution_receipt"]
    assert result["input_mapping"]["max_error_pixels"] == 0
    assert all(result["foreground_isolation"].values())

    Runtime.guard = {**Runtime.guard, "observed_onscreen_window_pids": [4242]}
    flashed_dir = tmp_path / "flashed"
    flashed_dir.mkdir()
    flashed = _exercise_runtime(Runtime(), flashed_dir, Sentinel())  # type: ignore[arg-type]
    assert flashed["passed"] is False
    assert flashed["foreground_isolation"]["background_window_guard_passed"] is False

    Runtime.capture_backend = "unity_gameview_capture_v3"
    Runtime.guard = {
        **Runtime.guard,
        "hide_successes": 1,
        "visible_application_pids": [],
        "max_onscreen_duration_ms": 40,
        "first_hidden_after_onscreen_unix_ms": 123456,
    }
    minimized_dir = tmp_path / "unity-minimized"
    minimized_dir.mkdir()
    minimized = _exercise_runtime(Runtime(), minimized_dir, Sentinel())  # type: ignore[arg-type]
    assert minimized["passed"] is True
    assert minimized["foreground_isolation"]["transient_window_policy_passed"] is True

    Runtime.guard = {**Runtime.guard, "max_onscreen_duration_ms": 10_001}
    slow_dir = tmp_path / "unity-too-slow"
    slow_dir.mkdir()
    too_slow = _exercise_runtime(Runtime(), slow_dir, Sentinel())  # type: ignore[arg-type]
    assert too_slow["passed"] is False
    assert too_slow["foreground_isolation"]["transient_window_policy_passed"] is False


def test_luanti_canary_requires_observable_game_behaviors(tmp_path: Path) -> None:
    class Session:
        def __init__(self) -> None:
            self.position = {"x": 0.0, "y": 1.0, "z": 0.0}
            self._log = tmp_path / "luanti.log"
            self._log.write_text("", encoding="utf-8")

        def semantic_snapshot(self, _task_id, _spec):
            return {"snapshot": {"player": {"pos": dict(self.position)}}}

    class Runtime:
        session_id = "luanti-canary"
        capture_backend = "luanti_irrlicht_create_screenshot_v1"

        def __init__(self) -> None:
            self.session = Session()
            self.last_policy_frame_id = -1
            self.menu = True  # generic canary matrix ends by opening Escape menu
            self.debug = False
            self.camera = False
            self.chat_open = False

        def capture_policy_frame(self, path: Path) -> str:
            self.last_policy_frame_id += 1
            image = Image.new("RGB", (1280, 720), (20, 40, 80))
            draw = ImageDraw.Draw(image)
            if self.debug:
                draw.rectangle((0, 0, 1279, 219), fill=(240, 240, 240))
            if self.camera:
                draw.rectangle((0, 220, 1279, 719), fill=(100, 150, 20))
            if self.menu:
                draw.rectangle((256, 120, 1023, 639), fill=(180, 180, 180))
            image.save(path)
            return str(path)

        def dispatch_input_batch(self, batch):
            action = batch.actions[0]
            if action.kind == "key" and action.key == "escape":
                self.menu = not self.menu
            elif action.kind == "key" and action.key == "f5":
                self.debug = not self.debug
            elif action.kind == "key" and action.key == "w":
                self.session.position["x"] += 1.0
            elif action.kind == "drag":
                self.camera = not self.camera
            elif action.kind == "key" and action.key == "/":
                self.chat_open = True
            elif action.kind == "type" and self.chat_open:
                with self.session._log.open("a", encoding="utf-8") as output:
                    output.write((action.text or "") + "\n")
            return {
                "batch_id": batch.batch_id,
                "accepted": True,
                "consumed": True,
                "consumed_frame_count": 2 if action.key == "w" else 1,
                "event_count": 2,
                "resulting_frame_id": batch.frame_id + 1,
                "error": "",
            }

    result = _luanti_behavior_checks(Runtime(), tmp_path)
    assert result["passed"] is True
    assert all(result["checks"].values())
    assert result["horizontal_move_distance"] == 1.0
    assert result["receipts"]["move_w"]["consumed_frame_count"] == 2
