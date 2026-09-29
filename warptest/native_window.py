"""Real-desktop window focus and full-screen capture for Agent-S native mode.

Only used when ``policy_runtime_metadata["native_exec_enabled"]`` is true (see
``warptest/agent_s_worker.py``'s ``build_native_agent_s3``). The Agent-S native
worker child runs real ``pyautogui`` code against the OS desktop, so pyautogui's
coordinate space is the physical screen, not a game viewport or a rendered
frame buffer. The parent therefore must, before every step:

1. bring the target game window to the real OS foreground (``focus_runtime``),
   so the click Agent-S is about to make actually lands on it; and
2. hand the worker a real, unscaled screenshot of that desktop
   (``capture_full_screen``), not a Playwright/Luanti frame render, so the
   grounding model's coordinates and the executed pyautogui click agree.

This module never parses or executes model output; it only manages window
focus and takes screenshots.
"""
from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from PIL import Image


class NativeWindowError(RuntimeError):
    """The real desktop could not be focused or captured for native mode."""


_screen_size_lock = threading.Lock()
_screen_size_cache: tuple[int, int] | None = None


def capture_full_screen(path: Path) -> Path:
    """Capture the entire real desktop with macOS's built-in ``screencapture``.

    ``-x`` suppresses the capture sound, ``-t png`` fixes the format so the
    Agent-S worker child (and ``screenshot_dimensions_match``) can rely on it.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["screencapture", "-x", "-t", "png", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or not path.is_file():
        raise NativeWindowError(
            f"screencapture failed (exit={completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    return path


def screen_size() -> tuple[int, int]:
    """Return the real desktop pixel size, measured from an actual capture.

    Measuring a real capture (rather than querying a separate display API)
    guarantees the reported size exactly matches what ``capture_full_screen``
    will hand the Agent-S native worker -- the coordinate space its pyautogui
    clicks operate in -- even on Retina displays where logical/backing-store
    scale factors can otherwise disagree.
    """

    global _screen_size_cache

    cached = _screen_size_cache
    if cached is not None:
        return cached
    # C1 can start up to eight task lanes at once.  A concurrent cache miss used
    # to launch one macOS ``screencapture`` process per lane; under load, some of
    # those identical probes could exceed the fixed command timeout before any
    # provider call.  The physical-screen contract is process-stable, so perform
    # the real capture exactly once and share its measured backing-store size.
    with _screen_size_lock:
        cached = _screen_size_cache
        if cached is not None:
            return cached
        with tempfile.TemporaryDirectory() as tmp:
            probe = capture_full_screen(Path(tmp) / "probe.png")
            with Image.open(probe) as image:
                measured = int(image.width), int(image.height)
        _screen_size_cache = measured
        return measured


def focus_runtime(runtime: Any) -> None:
    """Bring a GUI runtime's real OS window to the foreground before a native step.

    Dispatches on shape rather than type so it works for both
    ``LuantiGuiRuntime`` (exposes ``.native`` -- a ``QuartzNativeInput`` with
    its own AppleScript-based ``focus()``) and Playwright's headed browser
    ``Page`` (``bring_to_front()`` is a first-class Playwright API).
    """

    native = getattr(runtime, "native", None)
    if native is not None and hasattr(native, "focus"):
        native.focus()
        return
    page = getattr(runtime, "page", None)
    if page is not None and hasattr(page, "bring_to_front"):
        page.bring_to_front()
        return
    raise NativeWindowError(
        f"do not know how to bring runtime {runtime!r} to the foreground for native mode"
    )


__all__ = [
    "NativeWindowError",
    "capture_full_screen",
    "focus_runtime",
    "screen_size",
]
