"""In-process Blender command bridge for a persistent C1 desktop session.

This script is launched by Blender itself.  Commands are polled with
``bpy.app.timers`` so the normal GUI event loop remains responsive while Agent-S
uses Blender's private framebuffer and event queue. Restore is target-only and
semantic commands are
read-only probes.  The separately named ``apply_state_actions`` command exists
only for the registered programmatic State arm; the Agent-S orchestration never
calls it.
"""

from __future__ import annotations

import json
import math
import os
import sys
import traceback
from pathlib import Path
from typing import Any

import bpy


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest import blender_driver as driver  # noqa: E402


SEMANTIC_SOURCE = "blender_live_bpy_probe_v1"
CONTROL_DIR: Path
COMMANDS_DIR: Path
RESPONSES_DIR: Path
BRIDGE_ERRORS: list[str] = []
SHUTTING_DOWN = False
SESSION_ID: str
LAST_SEQUENCE = 0
LAST_CAPTURED_FRAME_ID = -1
FRAME_CONSUMED = True
RECEIPTS: dict[str, dict[str, Any]] = {}
STATE_ACTION_TYPES = {
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

MAX_BATCH_ACTIONS = 64
MAX_TEXT_CHARS = 4096
MAX_ACTION_SECONDS = 5.0
KEY_TYPES = {
    "enter": "RET",
    "return": "RET",
    "escape": "ESC",
    "esc": "ESC",
    "backspace": "BACK_SPACE",
    "delete": "DEL",
    "tab": "TAB",
    "space": "SPACE",
    "left": "LEFT_ARROW",
    "right": "RIGHT_ARROW",
    "up": "UP_ARROW",
    "down": "DOWN_ARROW",
    "home": "HOME",
    "end": "END",
    "pageup": "PAGE_UP",
    "pagedown": "PAGE_DOWN",
}
MODIFIER_NAMES = {
    "shift": "shift",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "meta": "oskey",
    "command": "oskey",
    "cmd": "oskey",
}
BUTTON_TYPES = {
    "left": "LEFTMOUSE",
    "right": "RIGHTMOUSE",
    "middle": "MIDDLEMOUSE",
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _check(name: str, operation: Any) -> dict[str, Any]:
    try:
        detail = operation()
        return {"name": name, "ok": True, "detail": str(detail)}
    except Exception as error:
        return {"name": name, "ok": False, "detail": str(error)}


def _close_enough(actual: Any, expected: Any, *, tolerance: float = 1e-4) -> bool:
    try:
        actual_values = [float(value) for value in actual]
        expected_values = [float(value) for value in expected]
    except Exception:
        return False
    return len(actual_values) == len(expected_values) and all(
        math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)
        for left, right in zip(actual_values, expected_values)
    )


def _probe_target(target: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    kind = target.get("kind")
    checks.append(
        {
            "name": "target.kind",
            "ok": kind in {"scene", "blend_file"},
            "detail": f"live Blender target kind={kind!r}",
        }
    )
    objects = target.get("objects")
    if not isinstance(objects, list):
        return checks + [
            {
                "name": "target.objects",
                "ok": False,
                "detail": "target.objects is not a list",
            }
        ]
    for index, object_spec in enumerate(objects):
        if not isinstance(object_spec, dict):
            checks.append(
                {
                    "name": f"target.object[{index}]",
                    "ok": False,
                    "detail": "object specification is not a mapping",
                }
            )
            continue
        name = str(object_spec.get("name") or "")
        obj = bpy.data.objects.get(name)
        checks.append(
            {
                "name": f"target.object[{name}].exists",
                "ok": obj is not None,
                "detail": f"object {name!r} {'exists' if obj is not None else 'is missing'}",
            }
        )
        if obj is None:
            continue
        expected_type = object_spec.get("type")
        if expected_type:
            checks.append(
                {
                    "name": f"target.object[{name}].type",
                    "ok": obj.type == expected_type,
                    "detail": f"actual={obj.type!r} expected={expected_type!r}",
                }
            )
        if isinstance(object_spec.get("location"), list):
            expected_location = object_spec["location"]
            actual_location = list(obj.location)
            checks.append(
                {
                    "name": f"target.object[{name}].location",
                    "ok": _close_enough(actual_location, expected_location),
                    "detail": f"actual={actual_location!r} expected={expected_location!r}",
                }
            )
        size = object_spec.get("size")
        if size is not None and str(object_spec.get("primitive") or "cube") == "cube":
            expected_dimensions = [float(size)] * 3
            actual_dimensions = list(obj.dimensions)
            checks.append(
                {
                    "name": f"target.object[{name}].dimensions",
                    "ok": _close_enough(actual_dimensions, expected_dimensions),
                    "detail": f"actual={actual_dimensions!r} expected={expected_dimensions!r}",
                }
            )
        material_spec = object_spec.get("material")
        if isinstance(material_spec, dict):
            expected_name = str(material_spec.get("name") or "")
            material = (
                obj.data.materials[0] if getattr(obj.data, "materials", None) else None
            )
            checks.append(
                {
                    "name": f"target.object[{name}].material",
                    "ok": material is not None and material.name == expected_name,
                    "detail": (
                        f"actual={getattr(material, 'name', None)!r} expected={expected_name!r}"
                    ),
                }
            )
            expected_color = material_spec.get("color")
            if material is not None and isinstance(expected_color, list):
                actual_color = list(material.diffuse_color)
                checks.append(
                    {
                        "name": f"target.object[{name}].material_color",
                        "ok": _close_enough(actual_color, expected_color),
                        "detail": f"actual={actual_color!r} expected={expected_color!r}",
                    }
                )
    if target.get("frame") is not None:
        expected_frame = int(target["frame"])
        checks.append(
            {
                "name": "target.frame",
                "ok": int(bpy.context.scene.frame_current) == expected_frame,
                "detail": (
                    f"actual={int(bpy.context.scene.frame_current)} expected={expected_frame}"
                ),
            }
        )
    return checks


def _probe_assertions(assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not assertions:
        return [
            {"name": "assertions", "ok": False, "detail": "assertion list is empty"}
        ]
    for index, assertion in enumerate(assertions):
        assertion_type = assertion.get("type") if isinstance(assertion, dict) else None
        name = f"assertion[{assertion_type}]@{index}"
        if not isinstance(assertion, dict):
            checks.append(
                {"name": name, "ok": False, "detail": "assertion is not a mapping"}
            )
            continue
        if assertion_type == "no_blender_python_errors":
            checks.append(
                {
                    "name": name,
                    "ok": not BRIDGE_ERRORS,
                    "detail": "no bridge errors"
                    if not BRIDGE_ERRORS
                    else "; ".join(BRIDGE_ERRORS),
                }
            )
            continue
        checks.append(
            _check(
                name, lambda assertion=assertion: driver._run_assertion(assertion)[0]
            )
        )
    return checks


def _evidence(checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    passed = bool(checks) and all(check.get("ok") is True for check in checks)
    return {
        "status": "success" if passed else "failure",
        "source": SEMANTIC_SOURCE,
        "live": True,
        "checks": checks,
        **extra,
    }


def _window() -> Any:
    windows = list(bpy.context.window_manager.windows)
    if not windows:
        raise RuntimeError("background bridge found no Blender windows")
    # File Open/Save creates a second Blender window.  That is a normal visible
    # policy state, not an infrastructure failure, and subsequent input/capture
    # must stay bound to the file browser until it closes.
    file_browsers = [
        window
        for window in windows
        if any(area.type == "FILE_BROWSER" for area in window.screen.areas)
    ]
    if file_browsers:
        return file_browsers[-1]
    context_window = getattr(bpy.context, "window", None)
    if context_window in windows:
        return context_window
    return windows[-1]


def _modifiers(names: Any) -> dict[str, bool]:
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        raise ValueError("modifiers must be a string list")
    result = {"shift": False, "ctrl": False, "alt": False, "oskey": False}
    for name in names:
        mapped = MODIFIER_NAMES.get(name.lower())
        if mapped is None:
            raise ValueError(f"unsupported modifier {name!r}")
        result[mapped] = True
    return result


def _point(action: dict[str, Any], prefix: str = "") -> tuple[int, int]:
    window = _window()
    x = action.get(prefix + "x")
    y = action.get(prefix + "y")
    if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
        raise ValueError("input coordinates must be integers")
    if not 0 <= x < window.width or not 0 <= y < window.height:
        raise ValueError(f"input coordinate ({x}, {y}) is outside {window.width}x{window.height}")
    # Policy frames use a top-left origin; Blender events use a bottom-left origin.
    return x, window.height - 1 - y


def _event(*, event_type: str, value: str, x: int, y: int, unicode: str | None = None, modifiers: dict[str, bool] | None = None) -> None:
    _window().event_simulate(
        type=event_type,
        value=value,
        unicode=unicode,
        x=x,
        y=y,
        **(modifiers or {"shift": False, "ctrl": False, "alt": False, "oskey": False}),
    )


def _tap_key(key: str, modifiers: list[str], *, unicode: str | None = None) -> int:
    window = _window()
    x, y = window.width // 2, window.height // 2
    event_type = KEY_TYPES.get(key.lower(), key.upper())
    if len(event_type) == 1 and event_type.isalnum():
        event_type = event_type.upper()
    flags = _modifiers(modifiers)
    _event(event_type=event_type, value="PRESS", x=x, y=y, unicode=unicode, modifiers=flags)
    _event(event_type=event_type, value="RELEASE", x=x, y=y, unicode=None, modifiers=flags)
    return 2


def _dispatch_action(action: dict[str, Any]) -> int:
    kind = action.get("kind")
    if kind in {"done", "fail"}:
        return 0
    if kind == "wait":
        seconds = action.get("seconds", 0.0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= float(seconds) <= MAX_ACTION_SECONDS:
            raise ValueError("wait duration is outside 0..5 seconds")
        # The main-thread command itself is atomic. Do not sleep the Blender UI;
        # the parent owns policy pacing after this acknowledged no-op.
        return 0
    if kind == "click":
        x, y = _point(action)
        button = BUTTON_TYPES.get(str(action.get("button") or "left").lower())
        clicks = action.get("clicks") or 1
        if button is None or isinstance(clicks, bool) or not isinstance(clicks, int) or not 1 <= clicks <= 3:
            raise ValueError("click parameters are invalid")
        flags = _modifiers(action.get("modifiers") or [])
        for _ in range(clicks):
            _event(event_type=button, value="PRESS", x=x, y=y, modifiers=flags)
            _event(event_type=button, value="RELEASE", x=x, y=y, modifiers=flags)
        return 2 * clicks
    if kind == "key":
        key = action.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("key action requires a key")
        return _tap_key(key, list(action.get("modifiers") or []))
    if kind == "type":
        text = action.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            raise ValueError("type text is invalid")
        count = 0
        point = None
        if action.get("x") is not None or action.get("y") is not None:
            point = _point(action)
        if point is not None:
            flags = _modifiers([])
            _event(event_type="LEFTMOUSE", value="PRESS", x=point[0], y=point[1], modifiers=flags)
            _event(event_type="LEFTMOUSE", value="RELEASE", x=point[0], y=point[1], modifiers=flags)
            count += 2
        if action.get("overwrite"):
            count += _tap_key("a", ["command"])
            count += _tap_key("backspace", [])
        for character in text:
            count += _tap_key("f24", [], unicode=character)
        if action.get("enter"):
            count += _tap_key("enter", [])
        return count
    if kind == "scroll":
        x, y = _point(action)
        dx = action.get("dx") or 0
        dy = action.get("dy") or 0
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (dx, dy)):
            raise ValueError("scroll deltas must be integers")
        event_type = "WHEELUPMOUSE" if dy > 0 else "WHEELDOWNMOUSE"
        if dx:
            event_type = "WHEELUPMOUSE" if dx > 0 else "WHEELDOWNMOUSE"
        count = max(1, min(abs(dx or dy), 32))
        flags = _modifiers(["shift"] if dx else [])
        for _ in range(count):
            _event(event_type=event_type, value="PRESS", x=x, y=y, modifiers=flags)
        return count
    if kind == "drag":
        x, y = _point(action)
        x2, y2 = _point({"x": action.get("x2"), "y": action.get("y2")})
        duration = action.get("duration") or 0.0
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 <= float(duration) <= MAX_ACTION_SECONDS:
            raise ValueError("drag duration is outside 0..5 seconds")
        button = BUTTON_TYPES.get(str(action.get("button") or "left").lower())
        if button is None:
            raise ValueError("drag button is invalid")
        flags = _modifiers(action.get("modifiers") or [])
        _event(event_type="MOUSEMOVE", value="NOTHING", x=x, y=y, modifiers=flags)
        _event(event_type=button, value="PRESS", x=x, y=y, modifiers=flags)
        steps = max(2, min(30, int(float(duration) * 30) or 2))
        for step in range(1, steps + 1):
            move_x = round(x + (x2 - x) * step / steps)
            move_y = round(y + (y2 - y) * step / steps)
            _event(event_type="MOUSEMOVE", value="NOTHING", x=move_x, y=move_y, modifiers=flags)
        _event(event_type=button, value="RELEASE", x=x2, y=y2, modifiers=flags)
        return steps + 3
    raise ValueError(f"unsupported input action {kind!r}")


def _capture_ui() -> dict[str, Any]:
    global LAST_CAPTURED_FRAME_ID, FRAME_CONSUMED
    window = _window()
    LAST_CAPTURED_FRAME_ID += 1
    frame_id = LAST_CAPTURED_FRAME_ID
    frames = CONTROL_DIR / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    path = frames / f"frame-{frame_id:08d}.png"
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
        bpy.ops.screen.screenshot(filepath=str(path))
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("Blender framebuffer screenshot was not created")
    FRAME_CONSUMED = False
    return {
        "status": "success",
        "path": str(path),
        "frame_id": frame_id,
        "width": int(window.width),
        "height": int(window.height),
        "coordinate_space": "frame_pixels_top_left_v1",
        "capture_backend": "blender_bpy_screen_screenshot_v1",
    }


def _input_batch(request: dict[str, Any]) -> dict[str, Any]:
    global FRAME_CONSUMED
    batch_id = request.get("batch_id")
    frame_id = request.get("frame_id")
    actions = request.get("actions")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("input batch id is missing")
    if batch_id in RECEIPTS:
        return dict(RECEIPTS[batch_id])
    if frame_id != LAST_CAPTURED_FRAME_ID or FRAME_CONSUMED:
        raise ValueError("input batch references a stale or consumed frame")
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_BATCH_ACTIONS or not all(isinstance(action, dict) for action in actions):
        raise ValueError("input batch actions are invalid")
    event_count = sum(_dispatch_action(action) for action in actions)
    FRAME_CONSUMED = True
    receipt = {
        "status": "success",
        "batch_id": batch_id,
        "accepted": True,
        "event_count": event_count,
        "resulting_frame_id": frame_id + 1,
        "error": "",
        "input_backend": "blender_window_event_simulate_v1",
    }
    RECEIPTS[batch_id] = dict(receipt)
    return receipt


def _handle(request: dict[str, Any]) -> dict[str, Any]:
    global SHUTTING_DOWN, LAST_SEQUENCE
    if request.get("session_id") != SESSION_ID:
        raise ValueError("Blender bridge session nonce mismatch")
    sequence = request.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= LAST_SEQUENCE:
        raise ValueError("Blender bridge request sequence is stale")
    LAST_SEQUENCE = sequence
    command = request.get("command")
    if command == "ping":
        return _evidence(
            [
                {
                    "name": "bridge.ready",
                    "ok": True,
                    "detail": "Blender UI bridge is live",
                }
            ],
            app_version=".".join(str(part) for part in bpy.app.version),
            pid=os.getpid(),
            execution_backend="background_pixel_v1",
            foreground_required=False,
            startup_splash_suppressed=STARTUP_SPLASH_SUPPRESSED,
        )
    if command == "capture_ui":
        return _capture_ui()
    if command == "input_batch":
        return _input_batch(request)
    if command == "restore_target":
        target = request.get("target")
        artifact_dir = Path(
            str(request.get("artifact_dir") or CONTROL_DIR / "artifacts")
        ).resolve()
        if not isinstance(target, dict):
            return _evidence(
                [
                    {
                        "name": "restore.target",
                        "ok": False,
                        "detail": "target is not a mapping",
                    }
                ]
            )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        driver.ARTIFACT_DIR = artifact_dir
        try:
            detail, _data = driver._restore_target(target)
            artifact = artifact_dir / "c1-restored-start.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(artifact))
            checks = [
                {"name": "restore.target_only", "ok": True, "detail": detail},
                *_probe_target(target),
            ]
            return _evidence(checks, artifact_path=str(artifact))
        except Exception as error:
            BRIDGE_ERRORS.append(f"restore_target: {error}")
            return _evidence(
                [{"name": "restore.target_only", "ok": False, "detail": str(error)}]
            )
    if command == "probe_target":
        target = request.get("target")
        if not isinstance(target, dict):
            return _evidence(
                [{"name": "target", "ok": False, "detail": "target is not a mapping"}]
            )
        return _evidence(_probe_target(target), scene_file=str(bpy.data.filepath or ""))
    if command == "probe_assertions":
        assertions = request.get("assertions")
        if not isinstance(assertions, list):
            return _evidence(
                [
                    {
                        "name": "assertions",
                        "ok": False,
                        "detail": "assertions is not a list",
                    }
                ]
            )
        return _evidence(
            _probe_assertions(assertions), scene_file=str(bpy.data.filepath or "")
        )
    if command == "apply_state_actions":
        actions = request.get("actions")
        artifact_dir = Path(
            str(request.get("artifact_dir") or CONTROL_DIR / "artifacts")
        ).resolve()
        if not isinstance(actions, list) or not actions:
            return _evidence(
                [
                    {
                        "name": "transition.actions_applied",
                        "ok": False,
                        "detail": "state action list is empty or malformed",
                    }
                ]
            )
        checks: list[dict[str, Any]] = []
        artifact_dir.mkdir(parents=True, exist_ok=True)
        driver.ARTIFACT_DIR = artifact_dir
        for index, action in enumerate(actions):
            action_type = action.get("type") if isinstance(action, dict) else None
            if not isinstance(action, dict) or action_type not in STATE_ACTION_TYPES:
                checks.append(
                    {
                        "name": f"action[{index}]",
                        "ok": False,
                        "detail": f"unsupported Blender state action: {action_type!r}",
                    }
                )
                break
            try:
                detail, _data = driver._run_action(action)
                checks.append(
                    {
                        "name": f"action[{action_type}]@{index}",
                        "ok": True,
                        "detail": detail,
                    }
                )
            except Exception as error:
                BRIDGE_ERRORS.append(f"apply_state_actions[{index}]: {error}")
                checks.append(
                    {
                        "name": f"action[{action_type}]@{index}",
                        "ok": False,
                        "detail": str(error),
                    }
                )
                break
        checks.append(
            {
                "name": "transition.actions_applied",
                "ok": len(checks) == len(actions)
                and all(check.get("ok") is True for check in checks),
                "detail": f"applied {len(checks)} of {len(actions)} registered actions",
            }
        )
        return _evidence(
            checks,
            scene_file=str(bpy.data.filepath or ""),
            artifact_dir=str(artifact_dir),
        )
    if command == "render_proof":
        path = Path(str(request.get("path") or "")).expanduser().resolve()
        spec = request.get("spec")
        if not isinstance(spec, dict) or not str(path):
            return _evidence(
                [
                    {
                        "name": "render.proof",
                        "ok": False,
                        "detail": "path/spec is invalid",
                    }
                ]
            )
        try:
            driver.RENDER_CENTER, driver.RENDER_ORTHO_SCALE = (
                driver._render_frame_for_spec(spec)
            )
            driver._render_scene_png(path)
            return _evidence(
                [
                    {
                        "name": "render.proof",
                        "ok": path.is_file() and path.stat().st_size > 0,
                        "detail": str(path),
                    }
                ],
                path=str(path),
            )
        except Exception as error:
            BRIDGE_ERRORS.append(f"render_proof: {error}")
            return _evidence(
                [{"name": "render.proof", "ok": False, "detail": str(error)}]
            )
    if command == "shutdown":
        SHUTTING_DOWN = True
        return _evidence(
            [{"name": "bridge.shutdown", "ok": True, "detail": "shutdown scheduled"}]
        )
    return _evidence(
        [
            {
                "name": "command",
                "ok": False,
                "detail": f"unsupported command: {command!r}",
            }
        ]
    )


def _quit() -> None:
    try:
        bpy.ops.wm.quit_blender()
    except Exception:
        pass
    return None


def _poll() -> float | None:
    for request_path in sorted(COMMANDS_DIR.glob("*.json")):
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request payload is not a mapping")
            response = _handle(request)
        except Exception as error:
            BRIDGE_ERRORS.append(f"command: {error}")
            response = {
                "status": "failure",
                "source": SEMANTIC_SOURCE,
                "live": True,
                "checks": [
                    {"name": "bridge.exception", "ok": False, "detail": str(error)}
                ],
                "traceback": traceback.format_exc(),
            }
            request = {}
        request_id = str(request.get("request_id") or request_path.stem)
        response.update({"schema_version": 1, "request_id": request_id})
        _atomic_json(RESPONSES_DIR / f"{request_id}.json", response)
        request_path.unlink(missing_ok=True)
    if SHUTTING_DOWN:
        bpy.app.timers.register(_quit, first_interval=0.05)
        return None
    return 0.05


def _control_from_args() -> tuple[Path, str]:
    if "--" not in sys.argv:
        raise RuntimeError("Blender C1 bridge requires -- <control-dir>")
    tail = sys.argv[sys.argv.index("--") + 1 :]
    if len(tail) != 2 or len(tail[1]) < 32:
        raise RuntimeError("Blender C1 bridge requires control directory and session nonce")
    return Path(tail[0]).expanduser().resolve(), tail[1]


CONTROL_DIR, SESSION_ID = _control_from_args()
COMMANDS_DIR = CONTROL_DIR / "commands"
RESPONSES_DIR = CONTROL_DIR / "responses"
COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
RESPONSES_DIR.mkdir(parents=True, exist_ok=True)

# ``--factory-startup`` deliberately ignores user preferences.  In a fresh
# isolated BLENDER_USER_CONFIG that otherwise makes Blender display its Quick
# Setup splash after this script returns, obscuring the real task pixels.  The
# preference is changed in memory before creator.cc calls
# WM_init_splash_on_startup(); no user profile or foreground input is needed.
bpy.context.preferences.view.show_splash = False
STARTUP_SPLASH_SUPPRESSED = bpy.context.preferences.view.show_splash is False
_atomic_json(
    CONTROL_DIR / "ready.json",
    {
        "status": "ready",
        "schema_version": 2,
        "pid": os.getpid(),
        "session_id": SESSION_ID,
        "app_version": ".".join(str(part) for part in bpy.app.version),
        "startup_splash_suppressed": STARTUP_SPLASH_SUPPRESSED,
    },
)
bpy.app.timers.register(_poll, first_interval=0.05, persistent=True)
