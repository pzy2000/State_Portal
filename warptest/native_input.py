"""Native macOS input and window-coordinate mapping for rendered Luanti C1 runs.

No model-produced code is ever evaluated here.  The harness accepts only the small
structured action vocabulary and posts corresponding Quartz events to the Luanti
window after mapping screenshot pixels into its content area.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from .runtime_lock import exclusive_file_lease


@dataclass(frozen=True)
class WindowBounds:
    x: float
    y: float
    width: float
    height: float
    content_x: float
    content_y: float
    content_width: float
    content_height: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


class QuartzInputError(RuntimeError):
    pass


class NativeActionValidationError(QuartzInputError):
    infrastructure_error = False


class NativeInputUnavailableError(QuartzInputError):
    infrastructure_error = True


QUARTZ_TEXT_INPUT_TRANSPORT = (
    "physical_ascii_keycode_unicode_payload_down_up_v1"
)
LUANTI_TEXT_INPUT_PROTOCOL = "luanti_chat_command_exact_v1"


def valid_quartz_text_input_proof(value: object) -> bool:
    """Return whether a calibration proves an exact multi-character SDL round trip."""

    if not isinstance(value, dict):
        return False
    expected = value.get("expected_sha256")
    observed = value.get("observed_sha256")
    expected_length = value.get("expected_length")
    observed_length = value.get("observed_length")
    return bool(
        value.get("passed") is True
        and value.get("protocol") == LUANTI_TEXT_INPUT_PROTOCOL
        and value.get("transport") == QUARTZ_TEXT_INPUT_TRANSPORT
        and isinstance(expected, str)
        and len(expected) == 64
        and all(character in "0123456789abcdef" for character in expected.lower())
        and observed == expected
        and isinstance(expected_length, int)
        and expected_length > 1
        and observed_length == expected_length
    )


def current_keyboard_input_source_fingerprint() -> str:
    completed = subprocess.run(
        ["defaults", "read", "com.apple.HIToolbox", "AppleSelectedInputSources"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    payload = completed.stdout.strip()
    if completed.returncode != 0 or not payload:
        raise NativeInputUnavailableError(
            "could not inspect the macOS keyboard input source"
        )
    normalized = "\n".join(line.rstrip() for line in payload.splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _console_lock_state_from_ioreg(output: str) -> bool | None:
    """Return the macOS console lock state parsed from ``ioreg`` output.

    ``IOConsoleLocked`` is the top-level WindowServer view while
    ``CGSSessionScreenIsLocked`` is recorded per console user.  Treat either
    affirmative value as locked.  If neither key is present, fail closed rather
    than assuming an interactive desktop exists.
    """

    values: list[bool] = []
    for key in ("IOConsoleLocked", "CGSSessionScreenIsLocked"):
        for match in re.finditer(
            rf'"{key}"\s*=\s*(Yes|No)',
            output,
            flags=re.IGNORECASE,
        ):
            values.append(match.group(1).lower() == "yes")
    if any(values):
        return True
    if values:
        return False
    return None


def require_unlocked_native_session() -> dict[str, bool | str]:
    """Fail before model spend unless macOS can receive foreground GUI input.

    A rendered Luanti process can still produce screenshots while the login
    window owns the console, but Quartz clicks are then discarded.  Check the
    console and the two permissions used by this module before calibration or a
    formal GUI run starts, so a locked machine cannot consume provider tokens
    and only fail at the final click.
    """

    if os.uname().sysname != "Darwin":
        raise NativeInputUnavailableError(
            "native Luanti input requires an interactive macOS session"
        )
    completed = subprocess.run(
        ["ioreg", "-n", "Root", "-d1"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise NativeInputUnavailableError(
            "could not inspect the macOS console lock state"
        )
    locked = _console_lock_state_from_ioreg(completed.stdout)
    if locked is None:
        raise NativeInputUnavailableError("macOS console lock state is unavailable")
    if locked:
        raise NativeInputUnavailableError("macOS console session is locked")

    application_services = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    application_services.AXIsProcessTrusted.restype = ctypes.c_bool
    application_services.CGPreflightPostEventAccess.restype = ctypes.c_bool
    accessibility_trusted = bool(application_services.AXIsProcessTrusted())
    event_post_access = bool(application_services.CGPreflightPostEventAccess())
    if not accessibility_trusted:
        raise NativeInputUnavailableError(
            "macOS accessibility permission is unavailable"
        )
    if not event_post_access:
        raise NativeInputUnavailableError(
            "macOS Quartz event-post permission is unavailable"
        )
    return {
        "platform": "macos",
        "console_unlocked": True,
        "accessibility_trusted": True,
        "event_post_access": True,
        "keyboard_input_sources_sha256": current_keyboard_input_source_fingerprint(),
    }


@contextlib.contextmanager
def native_runtime_lock(
    path: str | Path = "/tmp/warptest-c1-luanti.lock",
    *,
    wait_timeout_s: float | None = None,
    blocking: bool = True,
) -> Iterator[None]:
    """Serialize native trials without charging queue time to task execution."""

    with exclusive_file_lease(
        path,
        resource_kind="native-backend",
        wait_timeout_s=wait_timeout_s,
        blocking=blocking,
    ):
        yield


class QuartzNativeInput:
    """Map rendered-frame pixels to a PID-owned window and post Quartz events."""

    # SDL/Luanti can ignore a synthetic down/up pair posted back-to-back, even
    # though Quartz accepts both events. Preserve human-scale settle/press
    # intervals so the engine's event pump observes a real click.
    _MOUSE_SETTLE_S = 0.2
    _MOUSE_PRESS_S = 0.08
    _KEY_PRESS_S = 0.02
    _KEY_INTERVAL_S = 0.01
    _SHIFT_FLAG = 1 << 17
    _CONTROL_FLAG = 1 << 18
    _OPTION_FLAG = 1 << 19
    _COMMAND_FLAG = 1 << 20
    _MODIFIER_KEYCODES = (
        (_SHIFT_FLAG, 56),
        (_CONTROL_FLAG, 59),
        (_OPTION_FLAG, 58),
        (_COMMAND_FLAG, 55),
    )

    _KEYCODES = {
        "a": 0,
        "s": 1,
        "d": 2,
        "f": 3,
        "h": 4,
        "g": 5,
        "z": 6,
        "x": 7,
        "c": 8,
        "v": 9,
        "b": 11,
        "q": 12,
        "w": 13,
        "e": 14,
        "r": 15,
        "y": 16,
        "t": 17,
        "1": 18,
        "2": 19,
        "3": 20,
        "4": 21,
        "6": 22,
        "5": 23,
        "=": 24,
        "9": 25,
        "7": 26,
        "-": 27,
        "8": 28,
        "0": 29,
        "o": 31,
        "u": 32,
        "i": 34,
        "p": 35,
        "l": 37,
        "j": 38,
        "k": 40,
        "n": 45,
        "m": 46,
        "enter": 36,
        "return": 36,
        "tab": 48,
        "space": 49,
        "backspace": 51,
        "escape": 53,
        "home": 115,
        "pageup": 116,
        "delete": 117,
        "end": 119,
        "pagedown": 121,
        "insert": 114,
        "left": 123,
        "arrowleft": 123,
        "right": 124,
        "arrowright": 124,
        "down": 125,
        "arrowdown": 125,
        "up": 126,
        "arrowup": 126,
    }

    # Luanti uses SDL's physical keyboard path.  A CGEvent carrying a Unicode
    # payload but virtual keycode 0 is therefore interpreted as the physical
    # ``a`` key, regardless of the payload.  Text entry must use the real macOS
    # virtual keycode for every character and post both key-down and key-up.
    # A one-character Unicode payload on that correctly identified key-down
    # keeps SDL text entry exact even when a non-Latin macOS IME is selected.
    _TEXT_KEYCODES = {
        "a": 0,
        "s": 1,
        "d": 2,
        "f": 3,
        "h": 4,
        "g": 5,
        "z": 6,
        "x": 7,
        "c": 8,
        "v": 9,
        "b": 11,
        "q": 12,
        "w": 13,
        "e": 14,
        "r": 15,
        "y": 16,
        "t": 17,
        "1": 18,
        "2": 19,
        "3": 20,
        "4": 21,
        "6": 22,
        "5": 23,
        "9": 25,
        "7": 26,
        "8": 28,
        "0": 29,
        "o": 31,
        "u": 32,
        "i": 34,
        "p": 35,
        "l": 37,
        "j": 38,
        "k": 40,
        "n": 45,
        "m": 46,
        " ": 49,
        "=": 24,
        "-": 27,
        "]": 30,
        "[": 33,
        "'": 39,
        ";": 41,
        "\\": 42,
        ",": 43,
        "/": 44,
        ".": 47,
        "`": 50,
        "\n": 36,
        "\r": 36,
        "\t": 48,
    }
    _SHIFTED_TEXT_BASES = {
        "!": "1",
        "@": "2",
        "#": "3",
        "$": "4",
        "%": "5",
        "^": "6",
        "&": "7",
        "*": "8",
        "(": "9",
        ")": "0",
        "_": "-",
        "+": "=",
        "{": "[",
        "}": "]",
        "|": "\\",
        ":": ";",
        '"': "'",
        "<": ",",
        ">": ".",
        "?": "/",
        "~": "`",
    }

    def __init__(self, pid: int, *, frame_width: int = 1280, frame_height: int = 720) -> None:
        if os.uname().sysname != "Darwin":
            raise NativeInputUnavailableError(
                "Quartz native input is available only on macOS"
            )
        self.pid = int(pid)
        self.frame_width = int(frame_width)
        self.frame_height = int(frame_height)
        self._cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        self._configure_ctypes()

    def _configure_ctypes(self) -> None:
        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        self._CGPoint = CGPoint
        self._cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        self._cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
        self._cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        self._cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        self._cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self._cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self._cg.CFRelease.argtypes = [ctypes.c_void_p]
        self._cg.CGEventKeyboardSetUnicodeString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_uint16),
        ]

    def focus(self) -> None:
        script = f'''
tell application "System Events"
  tell first application process whose unix id is {self.pid} to set frontmost to true
  delay 0.05
  return unix id of first application process whose frontmost is true
end tell
'''
        last_error = "frontmost PID did not match"
        for _attempt in range(3):
            completed = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode == 0:
                try:
                    frontmost_pid = int(completed.stdout.strip())
                except ValueError:
                    last_error = f"invalid frontmost PID: {completed.stdout!r}"
                else:
                    if frontmost_pid == self.pid:
                        time.sleep(0.15)
                        return
                    last_error = f"frontmost PID was {frontmost_pid}"
            else:
                last_error = completed.stderr.strip() or "osascript failed"
            time.sleep(0.1)
        raise NativeInputUnavailableError(
            f"could not focus Luanti PID {self.pid}: {last_error}"
        )

    def window_bounds(self) -> WindowBounds:
        script = f'''
tell application "System Events"
  tell first application process whose unix id is {self.pid}
    set p to position of front window
    set s to size of front window
    return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)
  end tell
end tell
'''
        completed = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        if completed.returncode != 0:
            raise NativeInputUnavailableError(
                f"could not read Luanti window bounds: {completed.stderr.strip()}"
            )
        try:
            x, y, width, height = (float(value.strip()) for value in completed.stdout.strip().split(","))
        except Exception as error:
            raise NativeInputUnavailableError(
                f"invalid Luanti window bounds: {completed.stdout!r}"
            ) from error
        # AppleScript reports the outer window.  The engine hook captures content
        # only.  Treat surplus vertical extent as the title bar; the horizontal
        # extent is scaled when decorations/OS sizing differ from the requested
        # 1280x720 content.
        content_width = width
        expected_content_height = width * self.frame_height / max(1, self.frame_width)
        content_height = min(height, expected_content_height)
        titlebar = max(0.0, height - content_height)
        return WindowBounds(
            x=x,
            y=y,
            width=width,
            height=height,
            content_x=x,
            content_y=y + titlebar,
            content_width=content_width,
            content_height=content_height,
        )

    def map_point(self, x: int, y: int, *, bounds: WindowBounds | None = None) -> tuple[float, float]:
        bounds = bounds or self.window_bounds()
        px = min(max(float(x), 0.0), max(0.0, self.frame_width - 1.0))
        py = min(max(float(y), 0.0), max(0.0, self.frame_height - 1.0))
        return (
            bounds.content_x + px / max(1, self.frame_width) * bounds.content_width,
            bounds.content_y + py / max(1, self.frame_height) * bounds.content_height,
        )

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self.focus()
        sx, sy = self.map_point(x, y)
        down, up, quartz_button = (3, 4, 1) if button == "right" else (1, 2, 0)
        self._mouse_event(5, sx, sy)  # kCGEventMouseMoved
        time.sleep(self._MOUSE_SETTLE_S)
        click_count = max(1, int(clicks))
        for index in range(click_count):
            self._mouse_event(down, sx, sy, button=quartz_button)
            time.sleep(self._MOUSE_PRESS_S)
            self._mouse_event(up, sx, sy, button=quartz_button)
            if index + 1 < click_count:
                time.sleep(self._MOUSE_PRESS_S)

    def move(self, x: int, y: int) -> None:
        self.focus()
        sx, sy = self.map_point(x, y)
        self._mouse_event(5, sx, sy)  # kCGEventMouseMoved

    def drag(self, x: int, y: int, x2: int, y2: int) -> None:
        self.focus()
        start = self.map_point(x, y)
        end = self.map_point(x2, y2)
        self._mouse_event(1, *start)
        for index in range(1, 7):
            t = index / 6
            self._mouse_event(6, start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            time.sleep(0.025)
        self._mouse_event(2, *end)

    def key(self, key: str, *, modifiers: tuple[str, ...] = ()) -> None:
        normalized = str(key).strip().lower()
        code = self._KEYCODES.get(normalized)
        if code is None:
            if len(normalized) == 1:
                self.type_text(normalized)
                return
            raise NativeActionValidationError(f"unsupported native key: {key!r}")
        flags = 0
        for modifier in modifiers:
            normalized_modifier = str(modifier).lower()
            flag = {
                "shift": self._SHIFT_FLAG,
                "control": self._CONTROL_FLAG,
                "ctrl": self._CONTROL_FLAG,
                "alt": self._OPTION_FLAG,
                "option": self._OPTION_FLAG,
                "meta": self._COMMAND_FLAG,
                "command": self._COMMAND_FLAG,
            }.get(normalized_modifier)
            if flag is None:
                raise NativeActionValidationError(
                    f"unsupported native key modifier: {modifier!r}"
                )
            flags |= flag
        self.focus()
        self._press_key(code, flags=flags)

    def type_text(self, text: str) -> None:
        plan = [
            self._text_keystroke(character, index=index)
            for index, character in enumerate(text)
        ]
        self.focus()
        for character, (code, flags) in zip(text, plan, strict=True):
            self._press_key(code, flags=flags, text=character)

    @classmethod
    def _text_keystroke(cls, character: str, *, index: int) -> tuple[int, int]:
        if character.isascii() and character.isalpha() and character.isupper():
            return cls._TEXT_KEYCODES[character.lower()], cls._SHIFT_FLAG
        if character in cls._TEXT_KEYCODES:
            return cls._TEXT_KEYCODES[character], 0
        base = cls._SHIFTED_TEXT_BASES.get(character)
        if base is not None:
            return cls._TEXT_KEYCODES[base], cls._SHIFT_FLAG
        raise NativeActionValidationError(
            f"unsupported native text character at index {index}: {character!r}"
        )

    def _press_key(
        self,
        code: int,
        *,
        flags: int = 0,
        text: str | None = None,
    ) -> None:
        pressed_modifiers: list[tuple[int, int]] = []
        held_flags = 0
        base_down = False
        try:
            for flag, modifier_code in self._MODIFIER_KEYCODES:
                if not flags & flag:
                    continue
                held_flags |= flag
                self._key_event(modifier_code, True, flags=held_flags)
                pressed_modifiers.append((flag, modifier_code))
                time.sleep(self._KEY_INTERVAL_S)
            if text is None:
                self._key_event(code, True, flags=held_flags)
            else:
                self._key_event(code, True, flags=held_flags, text=text)
            base_down = True
            time.sleep(self._KEY_PRESS_S)
            self._key_event(code, False, flags=held_flags)
            base_down = False
            time.sleep(self._KEY_INTERVAL_S)
        finally:
            if base_down:
                with contextlib.suppress(QuartzInputError):
                    self._key_event(code, False, flags=held_flags)
            for flag, modifier_code in reversed(pressed_modifiers):
                held_flags &= ~flag
                with contextlib.suppress(QuartzInputError):
                    self._key_event(modifier_code, False, flags=held_flags)
                time.sleep(self._KEY_INTERVAL_S)

    def scroll(self, dx: int, dy: int) -> None:
        # kCGScrollEventUnitPixel=0; two axes. Positive Quartz Y scrolls up, while
        # Playwright-style action deltas use positive Y for down.
        self.focus()
        create = self._cg.CGEventCreateScrollWheelEvent
        create.restype = ctypes.c_void_p
        event = create(None, 0, 2, int(-dy), int(-dx))
        if not event:
            raise NativeInputUnavailableError(
                "CGEventCreateScrollWheelEvent failed"
            )
        try:
            self._cg.CGEventPost(0, event)
        finally:
            self._cg.CFRelease(event)

    def _mouse_event(self, event_type: int, x: float, y: float, *, button: int = 0) -> None:
        event = self._cg.CGEventCreateMouseEvent(None, event_type, self._CGPoint(x, y), button)
        if not event:
            raise NativeInputUnavailableError("CGEventCreateMouseEvent failed")
        try:
            self._cg.CGEventPost(0, event)  # kCGHIDEventTap
        finally:
            self._cg.CFRelease(event)

    def _key_event(
        self,
        code: int,
        down: bool,
        *,
        flags: int = 0,
        text: str | None = None,
    ) -> None:
        event = self._cg.CGEventCreateKeyboardEvent(None, int(code), bool(down))
        if not event:
            raise NativeInputUnavailableError(
                "CGEventCreateKeyboardEvent failed"
            )
        try:
            if flags:
                self._cg.CGEventSetFlags(event, flags)
            if text is not None:
                encoded = text.encode("utf-16-le")
                count = len(encoded) // 2
                characters = (ctypes.c_uint16 * count).from_buffer_copy(encoded)
                self._cg.CGEventKeyboardSetUnicodeString(
                    event,
                    count,
                    characters,
                )
            self._cg.CGEventPost(0, event)
        finally:
            self._cg.CFRelease(event)
