"""Zero-model capture/input/capture compatibility checks for C1 backends."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .agent_s_bridge import FRAME_PIXEL_COORDINATE_SPACE
from .gui_agent import (
    AgentAction,
    LuantiGuiRuntime,
    _apply_action_batch,
    _capture_runtime,
    _runtime_dimensions,
)


def background_pixel_roundtrip(
    runtime: Any,
    task_dir: Path,
    *,
    image_evidence: Callable[[str | Path | None], dict[str, Any]],
) -> dict[str, Any]:
    """Exercise exactly one capture/input/capture transaction without a model."""

    session_id = getattr(runtime, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        session_id = getattr(runtime, "_warptest_background_session_id", None)
    if not isinstance(session_id, str) or not session_id:
        session_id = hashlib.sha256(
            f"{id(runtime)}:{time.time_ns()}".encode("ascii")
        ).hexdigest()[:32]
        setattr(runtime, "_warptest_background_session_id", session_id)
    width, height = _runtime_dimensions(runtime)
    before = _capture_runtime(runtime, task_dir / "compat-before.png")
    if not before:
        raise RuntimeError("background compatibility capture returned no frame")
    frame_id = getattr(runtime, "last_policy_frame_id", None)
    if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
        raise RuntimeError("background compatibility capture returned no frame id")
    receipt = _apply_action_batch(
        runtime,
        (AgentAction(kind="wait", seconds=0.01),),
        session_id=session_id,
        frame_id=frame_id,
        batch_id=f"{session_id}:{frame_id}:compat",
        width=width,
        height=height,
    )
    # ``wait`` is intentionally state-neutral.  Native backends acknowledge the
    # batch exactly once but correctly report zero synthesized input events.
    if receipt.accepted is not True or receipt.batch_id != f"{session_id}:{frame_id}:compat":
        raise RuntimeError("background compatibility input batch was not accepted")
    after = _capture_runtime(runtime, task_dir / "compat-after.png")
    if not after:
        raise RuntimeError("background compatibility follow-up capture failed")
    resulting_frame_id = getattr(runtime, "last_policy_frame_id", None)
    if (
        isinstance(resulting_frame_id, bool)
        or not isinstance(resulting_frame_id, int)
        or resulting_frame_id <= frame_id
    ):
        raise RuntimeError("background compatibility frame id did not advance")
    result = {
        "protocol": "warptest-agent-s-v2",
        "execution_backend": "background_pixel_v1",
        "coordinate_space": FRAME_PIXEL_COORDINATE_SPACE,
        "foreground_required": False,
        "capture_backend": str(
            getattr(runtime, "capture_backend", "playwright_page_screenshot_v1")
        ),
        "input_backend": str(
            getattr(runtime, "input_backend", "playwright_page_input_v1")
        ),
        "frame": [width, height],
        "session_id": session_id,
        "frame_id": frame_id,
        "resulting_frame_id": resulting_frame_id,
        "receipt": asdict(receipt),
        "before": image_evidence(before),
        "after": image_evidence(after),
        "passed": True,
    }
    capture_mapping = getattr(runtime, "last_policy_frame_capture", None)
    if isinstance(capture_mapping, dict):
        result["capture_mapping"] = dict(capture_mapping)
    return result


__all__ = ["LuantiGuiRuntime", "background_pixel_roundtrip"]
