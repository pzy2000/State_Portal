from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from warptest.agent_s_bridge import (
    AgentSAction,
    InputBatch,
    PolicyFrame,
)
from warptest.agent_s_worker import (
    AgentSJsonlServer,
    WorkerConfig,
    _safe_create_grounded_action,
    _structured_dispatch,
    parse_background_agent_call,
)


@pytest.mark.parametrize(
    "source,verb",
    [
        ("agent.click('Save', 2, 'right', ['shift'])", "click"),
        ("agent.type('Editor', '你好, WarpTest', True, True)", "type"),
        ("agent.hotkey(['meta', 's'])", "hotkey"),
        ("agent.hold_and_press(['shift'], ['left', 'right'])", "hold_and_press"),
        ("agent.scroll('panel', -4, False)", "scroll"),
        ("agent.drag_and_drop('a', 'b', [])", "drag_and_drop"),
        ("agent.highlight_text_span('a', 'b', 'left')", "highlight_text_span"),
        ("agent.wait(5)", "wait"),
        ("agent.done()", "done"),
        ("agent.fail()", "fail"),
        ("agent.save_to_knowledge('visible fact')", "save_to_knowledge"),
    ],
)
def test_background_parser_accepts_only_literal_agent_calls(source: str, verb: str) -> None:
    parsed, _args, _kwargs = parse_background_agent_call(source)
    assert parsed == verb


@pytest.mark.parametrize(
    "source",
    [
        "agent.open('Terminal')",
        "agent.switch_applications('Finder')",
        "agent.set_cell_values('A1', 'x')",
        "agent.call_code_agent('rm -rf /')",
        "__import__('os').system('true')",
        "agent.click(dynamic_value)",
        "agent.click(*['x'])",
        "agent.click('x'); agent.done()",
        "(lambda: agent.done())()",
    ],
)
def test_background_parser_rejects_code_and_non_pixel_verbs(source: str) -> None:
    with pytest.raises((SyntaxError, ValueError)):
        parse_background_agent_call(source)


def test_safe_grounding_dispatch_never_uses_python_eval_or_exec() -> None:
    class Agent:
        def assign_screenshot(self, observation):
            self.observation = observation

        def click(self, description):
            assert description == "button"
            return "import pyautogui; pyautogui.click(4, 5)"

    agent = Agent()
    with patch("builtins.eval", side_effect=AssertionError("eval forbidden")), patch(
        "builtins.exec", side_effect=AssertionError("exec forbidden")
    ):
        result = _safe_create_grounded_action(
            agent, "agent.click('button')", {"screenshot": b"pixels"}
        )
    assert result.endswith("pyautogui.click(4, 5)")
    assert agent.observation == {"screenshot": b"pixels"}


@pytest.mark.parametrize(
    "plan,grounded,kinds",
    [
        ("agent.click('x', 3, 'left', [])", "pyautogui.click(10, 20)", ["click"]),
        ("agent.type('x', '你好', True, True)", "pyautogui.click(10, 20)", ["type"]),
        ("agent.hotkey(['meta', 's'])", "", ["key"]),
        (
            "agent.hold_and_press(['shift'], ['left', 'right'])",
            "",
            ["key", "key"],
        ),
        ("agent.scroll('panel', -3, False)", "pyautogui.moveTo(10, 20)", ["scroll"]),
        (
            "agent.drag_and_drop('a', 'b', [])",
            "pyautogui.moveTo(10, 20); pyautogui.dragTo(30, 40)",
            ["drag"],
        ),
        (
            "agent.highlight_text_span('a', 'b', 'left')",
            "pyautogui.moveTo(10, 20); pyautogui.dragTo(30, 40)",
            ["drag"],
        ),
        ("agent.wait(5)", "WAIT", ["wait"]),
        ("agent.done()", "DONE", ["done"]),
        ("agent.fail()", "FAIL", ["fail"]),
        ("agent.save_to_knowledge('fact')", "", ["wait"]),
    ],
)
def test_background_action_compiler_covers_allowlist(
    plan: str, grounded: str, kinds: list[str]
) -> None:
    _first, actions = _structured_dispatch(plan, grounded, width=1280, height=800)
    assert [action["kind"] for action in actions] == kinds


def test_policy_frame_and_input_batch_bounds(tmp_path: Path) -> None:
    shot = tmp_path / "frame.png"
    Image.new("RGB", (8, 6), "navy").save(shot)
    digest = hashlib.sha256(shot.read_bytes()).hexdigest()
    assert PolicyFrame(
        session_id="s",
        frame_id=1,
        path=str(shot),
        sha256=digest,
        width=8,
        height=6,
        capture_backend="test_framebuffer",
    ).validated().frame_id == 1
    with pytest.raises(ValueError, match="outside"):
        InputBatch(
            session_id="s",
            frame_id=1,
            batch_id="b",
            actions=(AgentSAction(kind="click", x=8, y=0, clicks=1),),
        ).validated(width=8, height=6)
    with pytest.raises(ValueError, match="too long"):
        InputBatch(
            session_id="s",
            frame_id=1,
            batch_id="b",
            actions=(AgentSAction(kind="type", text="界" * 4097),),
        ).validated(width=8, height=6)


def test_background_worker_rejects_stale_policy_frame_without_native_exec(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "frame.png"
    Image.new("RGB", (8, 6), "navy").save(shot)
    digest = hashlib.sha256(shot.read_bytes()).hexdigest()
    payload = {
        "model": "decision/model",
        "base_url": "https://decision.invalid/v1",
        "api_key_env": "TEST_KEY",
        "grounder_model": "vision/model",
        "grounder_base_url": "https://vision.invalid/v1",
        "grounder_api_key_env": "TEST_GROUND_KEY",
        "agent_s_root": str(tmp_path),
        "native_screen_width": 8,
        "native_screen_height": 6,
        "execution_backend": "background_pixel_v1",
        "coordinate_space": "frame_pixels_top_left_v1",
        "enable_reflection": True,
        "enable_local_env": False,
        "worker_observation_mode": "screenshot",
    }

    class Agent:
        warptest_agent_s_version = "0.3.2"
        warptest_agent_s_commit = "test"

        def __init__(self):
            self.warptest_usage_events = []
            self.executor = type("Executor", (), {"turn_count": 1})()

        def reset(self):
            pass

        def predict(self, _instruction, _observation):
            self.warptest_usage_events.append(
                {"kind": "agent_s_worker", "total_tokens": 1, "usage_complete": True}
            )
            return {"plan_code": "agent.click('x')"}, ["pyautogui.click(1, 2)"]

    server = AgentSJsonlServer(io.StringIO(), io.StringIO())
    with patch.dict(
        os.environ, {"TEST_KEY": "not-used", "TEST_GROUND_KEY": "not-used"}
    ):
        server.config = WorkerConfig.from_mapping(payload)
    server.agent = Agent()
    request = {
        "task": "task",
        "instruction": "click",
        "screenshot": str(shot),
        "width": 8,
        "height": 6,
        "history": [],
        "policy_frame": {
            "session_id": "session",
            "frame_id": 1,
            "path": str(shot),
            "sha256": digest,
            "width": 8,
            "height": 6,
            "coordinate_space": "frame_pixels_top_left_v1",
            "capture_backend": "test_framebuffer",
        },
    }
    with patch("warptest.agent_s_worker._native_exec") as native_exec:
        result = server._handle_predict_native(request, server.agent)
        with pytest.raises(ValueError, match="stale"):
            server._handle_predict_native(request, server.agent)
    assert result["actions"][0]["kind"] == "click"
    native_exec.assert_not_called()
