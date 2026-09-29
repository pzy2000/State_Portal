"""Freeze a live C1 runtime while the remote Start judge is in flight.

Moving the before gate ahead of the transition must not let timers, animations,
or a native simulation advance the candidate start during a potentially long
model request. This lived in the GUI arm only, so the state arm graded Start
against a runtime that kept running -- an uncontrolled difference between two
arms whose Start rates are compared directly.

Chromium is held through CDP and native processes are suspended at the OS
process boundary. A runtime that declares a hold but cannot be held fails
closed; lightweight test doubles deliberately use a no-op hold.
"""
from __future__ import annotations

import os
import signal
from contextlib import contextmanager
from typing import Any

from . import run_context
from .c1_desktop_runtime import BlenderC1DesktopSession, LibreOfficeC1DesktopSession
from .runtime import BrowserRuntimeAdapter


NO_HOLD_BACKEND = "test_or_unknown_runtime"
SIGSTOP_BACKEND = "sigstop"
CDP_BACKEND = "chromium_cdp_script_pause"
# The state arm tears its Unity adapter down before the judge runs, so the start
# it grades cannot drift. That is a stronger guarantee than a hold, but it has to
# be recorded distinctly from "we did not recognize this runtime".
TERMINATED_BACKEND = "runtime_terminated_before_grading"


def _declares_hold_process(runtime: Any) -> bool:
    """True when the runtime owns a native process it can name for suspension.

    Declared separately from reading the value so a runtime whose process has
    already exited fails closed instead of silently degrading to a no-op hold.
    """

    return hasattr(type(runtime), "hold_process")


@contextmanager
def hold_runtime_state_for_start_grading(
    runtime: Any,
    *,
    metadata: dict[str, Any] | None = None,
    no_runtime_backend: str = NO_HOLD_BACKEND,
):
    metadata = metadata if metadata is not None else {}
    metadata.update(
        {
            "required": False,
            "held": False,
            "released": False,
            "backend": no_runtime_backend if runtime is None else NO_HOLD_BACKEND,
        }
    )
    cdp = None
    native_pid: int | None = None
    runtime_type = type(runtime)
    is_browser_runtime = (
        runtime_type.__name__ == "BrowserRuntimeAdapter"
        and runtime_type.__module__ == BrowserRuntimeAdapter.__module__
    )
    browser_context = getattr(runtime, "context", None)
    if browser_context is None:
        browser_context = getattr(getattr(runtime, "runtime", None), "context", None)
    is_c1_web_runtime = bool(
        browser_context is not None
        and hasattr(browser_context, "new_cdp_session")
        and getattr(runtime, "page", None) is not None
        and str(getattr(runtime, "benchmark", ""))
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
        == "jupyterlab"
    )
    try:
        try:
            if _declares_hold_process(runtime):
                metadata.update(required=True, backend=SIGSTOP_BACKEND)
                proc = runtime.hold_process
                if proc is None or proc.poll() is not None:
                    raise RuntimeError("cannot hold a stopped native runtime")
                native_pid = int(proc.pid)
                os.kill(native_pid, signal.SIGSTOP)
                metadata["held_pid"] = native_pid
                metadata["held"] = True
            elif isinstance(
                runtime, (BlenderC1DesktopSession, LibreOfficeC1DesktopSession)
            ):
                metadata.update(required=True, backend=SIGSTOP_BACKEND)
                process = getattr(runtime, "process", None)
                if process is None or process.poll() is not None:
                    raise RuntimeError("cannot hold a stopped desktop runtime")
                native_pid = int(process.pid)
                os.kill(native_pid, signal.SIGSTOP)
                metadata["held_pid"] = native_pid
                metadata["held"] = True
            elif isinstance(getattr(runtime, "hold_pid", None), int):
                metadata.update(required=True, backend=SIGSTOP_BACKEND)
                native_pid = int(runtime.hold_pid)
                os.kill(native_pid, signal.SIGSTOP)
                metadata["held_pid"] = native_pid
                metadata["held"] = True
            elif is_browser_runtime or is_c1_web_runtime:
                metadata.update(required=True, backend=CDP_BACKEND)
                cdp = browser_context.new_cdp_session(runtime.page)
                cdp.send("Emulation.setScriptExecutionDisabled", {"value": True})
                metadata["held"] = True
        except Exception as error:
            metadata["hold_error"] = run_context.safe_error_text(
                str(error), limit=1000
            )
            raise RuntimeError(
                "failed to hold runtime during start grading: "
                + metadata["hold_error"]
            ) from error
        yield metadata
    finally:
        release_errors: list[str] = []
        if native_pid is not None:
            try:
                os.kill(native_pid, signal.SIGCONT)
            except Exception as error:
                release_errors.append(f"sigcont: {error}")
        if cdp is not None:
            try:
                cdp.send("Emulation.setScriptExecutionDisabled", {"value": False})
            except Exception as error:
                release_errors.append(f"cdp-active: {error}")
            try:
                cdp.detach()
            except Exception as error:
                release_errors.append(f"cdp-detach: {error}")
        metadata["released"] = not release_errors
        if release_errors:
            metadata["release_error"] = "; ".join(release_errors)
            raise RuntimeError(
                "failed to release runtime after start grading: "
                + metadata["release_error"]
            )
