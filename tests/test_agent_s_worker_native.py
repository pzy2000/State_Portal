"""Tests for the native-only Agent-S worker path.

These cover the JSONL protocol wiring (done/fail/wait/exec dispatch, screenshot
bytes handed to ``agent.predict``, usage accounting) with fakes, plus one
skip-if-missing-venv test that exercises the real pinned upstream
``build_native_agent_s3`` end to end with only its HTTP client faked out.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from warptest.agent_s_bridge import (
    AGENT_S_REQUEST_MAX_TOKENS,
    PINNED_AGENT_S_COMMIT,
    PINNED_AGENT_S_VERSION,
)
from warptest.agent_s_worker import (
    AgentSReasoningContentContractError,
    AgentSBindings,
    AgentSJsonlServer,
    ChatCompletionsBackend,
    _NativeUsageRecordingCompletions,
    _background_procedural_prompt,
    _background_reflection_prompt,
    _extract_prompt_text,
    _native_action_fields,
    _native_dispatch,
    _parse_plan_code_verb,
    _plan_code_verb,
    _usage_audit,
)
from warptest.model_request import model_request_profiles_environment

ROOT = Path(__file__).resolve().parents[1]


class UsageAuditV2Tests(unittest.TestCase):
    def test_reasoning_gap_keeps_primary_usage_complete(self) -> None:
        audit, retry_reasoning = _usage_audit(
            SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=4,
                total_tokens=14,
                completion_tokens_details=SimpleNamespace(),
            ),
            content="",
            has_reasoning_content=False,
            finish_reason="length",
            reasoning_expected=True,
        )
        self.assertTrue(audit["usage_complete"])
        self.assertFalse(audit["reasoning_detail_complete"])
        self.assertIsNone(audit["reasoning_tokens"])
        self.assertEqual(retry_reasoning, 0)

    def test_thinking_disabled_absent_reasoning_is_zero(self) -> None:
        audit, _ = _usage_audit(
            SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=4,
                total_tokens=14,
                completion_tokens_details=SimpleNamespace(),
            ),
            content="",
            has_reasoning_content=False,
            finish_reason="length",
            reasoning_disabled=True,
        )
        self.assertTrue(audit["usage_complete"])
        self.assertTrue(audit["reasoning_detail_complete"])
        self.assertEqual(audit["reasoning_tokens"], 0)


def _provider_response(content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=None,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
            prompt_tokens_details=None,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )


def _screenshot(path: Path, width: int = 16, height: int = 12) -> Path:
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(path)
    return path


def _native_payload(agent_s_root: Path) -> dict:
    from warptest.agent_s_bridge import AgentSBridgeConfig

    return AgentSBridgeConfig(
        model="decision/model",
        base_url="https://decision.invalid/v1",
        api_key_env="TEST_AGENT_S_KEY",
        grounder_model="vision/model",
        grounder_base_url="https://vision.invalid/v1",
        grounder_api_key_env="TEST_AGENT_S_GROUND_KEY",
        native_screen_width=16,
        native_screen_height=12,
    ).to_worker_payload(agent_s_root=agent_s_root)


class NativeDispatchTests(unittest.TestCase):
    """``_native_dispatch`` replicates upstream cli_app.py's branching exactly."""

    def test_done_and_fail_are_detected_without_exec(self) -> None:
        with patch("warptest.agent_s_worker._native_exec") as exec_mock:
            self.assertEqual(_native_dispatch("DONE")["kind"], "done")
            self.assertEqual(_native_dispatch("FAIL")["kind"], "fail")
        exec_mock.assert_not_called()

    def test_wait_action_code_has_no_wait_substring_and_falls_to_exec(self) -> None:
        # OSWorldACI.wait() returns "import time; time.sleep(N)", which contains
        # no "wait" substring, so upstream cli_app.py's "wait" branch is actually
        # unreachable for a real wait() call and it falls through to plain
        # exec() -- this mode intentionally preserves that upstream behavior.
        with (
            patch("warptest.agent_s_worker.time.sleep") as sleep_mock,
            patch("warptest.agent_s_worker._native_exec") as exec_mock,
        ):
            action = _native_dispatch("import time; time.sleep(1.333)")
        self.assertEqual(action["kind"], "native_exec")
        exec_mock.assert_called_once_with("import time; time.sleep(1.333)")
        self.assertEqual(sleep_mock.call_count, 2)

    def test_explicit_wait_keyword_sleeps_five_seconds_and_skips_exec(self) -> None:
        with (
            patch("warptest.agent_s_worker.time.sleep") as sleep_mock,
            patch("warptest.agent_s_worker._native_exec") as exec_mock,
        ):
            action = _native_dispatch("agent wait requested")
        self.assertEqual(action["kind"], "wait")
        exec_mock.assert_not_called()
        sleep_mock.assert_called_once_with(5.0)

    def test_else_branch_execs_pyautogui_code(self) -> None:
        with (
            patch("warptest.agent_s_worker.time.sleep"),
            patch("warptest.agent_s_worker._native_exec") as exec_mock,
        ):
            action = _native_dispatch("import pyautogui; pyautogui.click(1, 2)")
        self.assertEqual(action["kind"], "native_exec")
        exec_mock.assert_called_once_with("import pyautogui; pyautogui.click(1, 2)")

    def test_else_branch_without_plan_code_stays_native_exec(self) -> None:
        with (
            patch("warptest.agent_s_worker.time.sleep"),
            patch("warptest.agent_s_worker._native_exec"),
        ):
            action = _native_dispatch("import pyautogui; pyautogui.click(1, 2)")
        self.assertEqual(action["kind"], "native_exec")
        self.assertNotIn("verb", action)

    def test_else_branch_with_click_plan_code_reports_click_kind_and_point(
        self,
    ) -> None:
        exec_code = (
            "import pyautogui; pyautogui.click(801, 442, clicks=1, button='left'); "
        )
        with (
            patch("warptest.agent_s_worker.time.sleep"),
            patch("warptest.agent_s_worker._native_exec") as exec_mock,
        ):
            action = _native_dispatch(
                exec_code,
                plan_code='agent.click("The 2048 title", 1, "left")',
            )
        exec_mock.assert_called_once_with(exec_code)
        self.assertEqual(action["kind"], "click")
        self.assertEqual(action["x"], 801)
        self.assertEqual(action["y"], 442)
        self.assertEqual(action["button"], "left")
        self.assertEqual(action["clicks"], 1)


class PlanCodeVerbParsingTests(unittest.TestCase):
    """``_parse_plan_code_verb``/``_native_action_fields`` never raise and only
    ever report a friendlier kind for display; they never affect execution
    (``_native_dispatch`` always execs the already-grounded ``exec_code``,
    independent of what these helpers return)."""

    def test_click_call_parses_description_args_and_coords_from_exec_code(self) -> None:
        kind, fields = _native_action_fields(
            'agent.click("The 2048 game title text", 1, "left")',
            "import pyautogui; pyautogui.click(801, 442, clicks=1, button='left'); ",
        )
        self.assertEqual(kind, "click")
        self.assertEqual(fields, {"button": "left", "clicks": 1, "x": 801, "y": 442})

    def test_type_call_takes_text_directly_from_plan_code_not_exec_code(self) -> None:
        kind, fields = _native_action_fields(
            'agent.type("search box", "hello world", False, True)',
            "import pyautogui; pyautogui.click(10, 20); pyautogui.write('hello world'); "
            "pyautogui.press('enter'); ",
        )
        self.assertEqual(kind, "type")
        self.assertEqual(
            fields, {"text": "hello world", "overwrite": False, "enter": True}
        )

    def test_hotkey_call_maps_last_key_to_key_and_rest_to_modifiers(self) -> None:
        kind, fields = _native_action_fields(
            "agent.hotkey(['cmd', 'alt', 'i'])",
            "import pyautogui; pyautogui.hotkey('cmd', 'alt', 'i')",
        )
        self.assertEqual(kind, "key")
        self.assertEqual(fields, {"key": "i", "modifiers": ["cmd", "alt"]})

    def test_hotkey_call_with_empty_keys_falls_back_to_native_exec(self) -> None:
        kind, fields = _native_action_fields(
            "agent.hotkey([])", "import pyautogui; pyautogui.hotkey()"
        )
        self.assertEqual(kind, "native_exec")
        self.assertEqual(fields, {})

    def test_scroll_call_maps_clicks_to_dy_or_dx_by_shift(self) -> None:
        kind, fields = _native_action_fields(
            'agent.scroll("page", -3, False)',
            "import pyautogui; pyautogui.moveTo(1, 2); pyautogui.vscroll(-3)",
        )
        self.assertEqual(kind, "scroll")
        self.assertEqual(fields, {"dx": 0, "dy": -3})

        kind, fields = _native_action_fields(
            'agent.scroll("page", 4, True)',
            "import pyautogui; pyautogui.moveTo(1, 2); pyautogui.hscroll(4)",
        )
        self.assertEqual(kind, "scroll")
        self.assertEqual(fields, {"dx": 4, "dy": 0})

    def test_drag_and_drop_call_extracts_both_endpoints_from_exec_code(self) -> None:
        kind, fields = _native_action_fields(
            'agent.drag_and_drop("start", "end")',
            "import pyautogui; pyautogui.moveTo(10, 20); "
            "pyautogui.dragTo(30, 40, duration=1., button='left'); pyautogui.mouseUp(); ",
        )
        self.assertEqual(kind, "drag")
        self.assertEqual(fields, {"x": 10, "y": 20, "x2": 30, "y2": 40})

    def test_full_action_surface_verb_falls_back_to_native_exec(self) -> None:
        for plan_code in (
            'agent.open("Chrome")',
            'agent.switch_applications("chrome")',
            'agent.highlight_text_span("start", "end")',
            "agent.hold_and_press(['shift'], ['down'])",
            'agent.save_to_knowledge(["fact"])',
        ):
            with self.subTest(plan_code=plan_code):
                kind, fields = _native_action_fields(plan_code, "WAIT")
                self.assertEqual(kind, "native_exec")
                self.assertEqual(fields, {})

    def test_unparseable_or_missing_plan_code_falls_back_to_native_exec(self) -> None:
        for plan_code in (None, "", "not python", "some_other_object.click(1, 2)"):
            with self.subTest(plan_code=plan_code):
                kind, fields = _native_action_fields(plan_code, "import pyautogui")
                self.assertEqual(kind, "native_exec")
                self.assertEqual(fields, {})

    def test_plan_code_verb_extracts_bare_verb_name(self) -> None:
        self.assertEqual(_plan_code_verb('agent.click("x", 1, "left")'), "click")
        self.assertEqual(_plan_code_verb('agent.open("Chrome")'), "open")
        self.assertIsNone(_plan_code_verb(None))
        self.assertIsNone(_plan_code_verb("garbage("))

    def test_parse_plan_code_verb_rejects_non_agent_calls(self) -> None:
        self.assertIsNone(_parse_plan_code_verb("os.system('rm -rf /')"))
        self.assertIsNone(_parse_plan_code_verb("print('hi')"))


class ExtractPromptTextTests(unittest.TestCase):
    """``_extract_prompt_text`` never raises and only reads back the exact text
    parts of the last user turn the decision model was shown."""

    def test_extracts_text_parts_and_skips_image_parts(self) -> None:
        agent = type(
            "Agent",
            (),
            {
                "executor": type(
                    "Executor",
                    (),
                    {
                        "generator_agent": type(
                            "Gen",
                            (),
                            {
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": [{"type": "text", "text": "sys"}],
                                    },
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": "part one"},
                                            {
                                                "type": "image_url",
                                                "image_url": {"url": "data:..."},
                                            },
                                            {"type": "text", "text": "part two"},
                                        ],
                                    },
                                    {
                                        "role": "assistant",
                                        "content": [{"type": "text", "text": "plan"}],
                                    },
                                ]
                            },
                        )()
                    },
                )()
            },
        )()
        self.assertEqual(_extract_prompt_text(agent), "part one\npart two")

    def test_missing_or_malformed_conversation_returns_none_without_raising(
        self,
    ) -> None:
        self.assertIsNone(_extract_prompt_text(object()))
        self.assertIsNone(_extract_prompt_text(type("A", (), {"executor": None})()))


class NativeJsonlProtocolTests(unittest.TestCase):
    """Fake-agent coverage of the JSONL server's native dispatch and protocol shape."""

    def test_init_selects_native_factory_and_predict_execs_via_fake_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            expected_screenshot_bytes = shot.read_bytes()
            payload = _native_payload(tmp)
            requests = [
                {"id": 1, "op": "init", "config": payload},
                {
                    "id": 2,
                    "op": "predict",
                    "task": "task",
                    "instruction": "click the button",
                    "screenshot": str(shot),
                    "width": 16,
                    "height": 12,
                    "history": [],
                },
                {"id": 3, "op": "close"},
            ]
            input_stream = io.StringIO(
                "".join(json.dumps(item) + "\n" for item in requests)
            )
            output_stream = io.StringIO()

            class BaseAgent:
                pass

            bindings = AgentSBindings(
                agent_s3_class=BaseAgent,
                reflection_prompt="reflect",
                version=PINNED_AGENT_S_VERSION,
                commit=PINNED_AGENT_S_COMMIT,
            )

            class FakeExecutor:
                turn_count = 3

            class FakeNativeAgent(BaseAgent):
                def __init__(self):
                    self.executor = FakeExecutor()
                    # _handle_predict_native clears this before calling predict(),
                    # mirroring how the real instrumented client appends events
                    # during the call; populate it from inside predict() too.
                    self.warptest_usage_events = []
                    self.warptest_agent_s_version = PINNED_AGENT_S_VERSION
                    self.warptest_agent_s_commit = PINNED_AGENT_S_COMMIT
                    self.received_observation = None

                def reset(self):
                    pass

                def predict(self, instruction, observation):
                    self.received_observation = observation
                    self.warptest_usage_events.append(
                        {
                            "model": "decision/model",
                            "kind": "agent_s_worker",
                            "prompt_tokens": 5,
                            "completion_tokens": 2,
                            "reasoning_tokens": 0,
                            "cached_prompt_tokens": 0,
                            "total_tokens": 7,
                            "latency_ms": 4,
                            "usage_complete": True,
                        }
                    )
                    return {"plan": "agent.click('button')"}, [
                        "import pyautogui; pyautogui.click(1, 2)"
                    ]

            fake_agent = FakeNativeAgent()
            with (
                patch.dict(
                    os.environ,
                    {"TEST_AGENT_S_KEY": "dummy", "TEST_AGENT_S_GROUND_KEY": "dummy"},
                ),
                patch("warptest.agent_s_worker._native_exec") as exec_mock,
                patch("warptest.agent_s_worker.time.sleep"),
            ):
                server = AgentSJsonlServer(
                    input_stream,
                    output_stream,
                    agent_factory=lambda _config, _bindings: fake_agent,
                    bindings_loader=lambda _root: bindings,
                )
                server.serve()
            responses = [
                json.loads(line) for line in output_stream.getvalue().splitlines()
            ]
            observation = fake_agent.received_observation

        self.assertTrue(all(item["ok"] for item in responses))
        init_result = responses[0]["result"]
        self.assertEqual(init_result["runtime_mode"], "native")
        self.assertEqual(init_result["worker_observation_mode"], "screenshot")
        self.assertTrue(init_result["native_exec_enabled"])
        self.assertNotIn("max_tokens", init_result)

        predict_result = responses[1]["result"]
        self.assertEqual(predict_result["action"]["kind"], "native_exec")
        self.assertEqual(
            predict_result["info"]["worker_observation_mode"], "screenshot"
        )
        self.assertTrue(predict_result["info"]["native_exec_enabled"])
        self.assertTrue(predict_result["info"]["usage_accounting_complete"])
        exec_mock.assert_called_once_with("import pyautogui; pyautogui.click(1, 2)")

        # The child reads the screenshot file into raw bytes for upstream
        # OSWorldACI/LMMAgent (native observation is screenshot bytes only).
        self.assertIsInstance(observation["screenshot"], bytes)
        self.assertEqual(observation["screenshot"], expected_screenshot_bytes)

    def test_predict_info_carries_plan_reflection_prompt_text_and_parsed_kind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            payload = _native_payload(tmp)
            requests = [
                {"id": 1, "op": "init", "config": payload},
                {
                    "id": 2,
                    "op": "predict",
                    "task": "task",
                    "instruction": "click the button",
                    "screenshot": str(shot),
                    "width": 16,
                    "height": 12,
                    "history": [],
                },
            ]
            input_stream = io.StringIO(
                "".join(json.dumps(item) + "\n" for item in requests)
            )
            output_stream = io.StringIO()

            class BaseAgent:
                pass

            bindings = AgentSBindings(
                agent_s3_class=BaseAgent,
                reflection_prompt="reflect",
                version=PINNED_AGENT_S_VERSION,
                commit=PINNED_AGENT_S_COMMIT,
            )

            class FakeGeneratorAgent:
                # Mirrors the real post-predict state: upstream's own
                # ``generate_next_action`` already appended the assistant
                # ``plan`` reply as messages[-1] before returning, so the
                # user turn ``_extract_prompt_text`` should read is messages[-2].
                messages = [
                    {"role": "system", "content": [{"type": "text", "text": "sys"}]},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "REFLECTION: ...\n"},
                            {"type": "image_url", "image_url": {"url": "data:..."}},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "(Grounded Action) ..."}],
                    },
                ]

            class FakeExecutor:
                turn_count = 2
                generator_agent = FakeGeneratorAgent()

            class FakeNativeAgent(BaseAgent):
                def __init__(self):
                    self.executor = FakeExecutor()
                    self.warptest_usage_events = []
                    self.warptest_agent_s_version = PINNED_AGENT_S_VERSION
                    self.warptest_agent_s_commit = PINNED_AGENT_S_COMMIT

                def reset(self):
                    pass

                def predict(self, _instruction, _observation):
                    self.warptest_usage_events.append(
                        {
                            "model": "decision/model",
                            "kind": "agent_s_worker",
                            "prompt_tokens": 5,
                            "completion_tokens": 2,
                            "reasoning_tokens": 0,
                            "cached_prompt_tokens": 0,
                            "total_tokens": 7,
                            "latency_ms": 4,
                            "usage_complete": True,
                        }
                    )
                    return (
                        {
                            "plan": "(Grounded Action)\n```python\nagent.click('button')\n```",
                            "plan_code": 'agent.click("the button", 1, "left")',
                            "reflection": "previous action verified successfully",
                        },
                        [
                            "import pyautogui; pyautogui.click(801, 442, clicks=1, button='left'); "
                        ],
                    )

            with (
                patch.dict(
                    os.environ,
                    {"TEST_AGENT_S_KEY": "dummy", "TEST_AGENT_S_GROUND_KEY": "dummy"},
                ),
                patch("warptest.agent_s_worker._native_exec"),
                patch("warptest.agent_s_worker.time.sleep"),
            ):
                AgentSJsonlServer(
                    input_stream,
                    output_stream,
                    agent_factory=lambda _config, _bindings: FakeNativeAgent(),
                    bindings_loader=lambda _root: bindings,
                ).serve()
            responses = [
                json.loads(line) for line in output_stream.getvalue().splitlines()
            ]

        predict_result = responses[1]["result"]
        self.assertEqual(predict_result["action"]["kind"], "click")
        self.assertEqual(predict_result["action"]["x"], 801)
        self.assertEqual(predict_result["action"]["y"], 442)
        info = predict_result["info"]
        self.assertIn("agent.click('button')", info["plan"])
        self.assertEqual(info["plan_code"], 'agent.click("the button", 1, "left")')
        self.assertEqual(info["reflection"], "previous action verified successfully")
        self.assertEqual(info["verb"], "click")
        self.assertEqual(info["prompt_text"], "REFLECTION: ...\n")

    def test_done_action_never_calls_exec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            payload = _native_payload(tmp)
            requests = [
                {"id": 1, "op": "init", "config": payload},
                {
                    "id": 2,
                    "op": "predict",
                    "task": "task",
                    "instruction": "finish",
                    "screenshot": str(shot),
                    "width": 16,
                    "height": 12,
                    "history": [],
                },
            ]
            input_stream = io.StringIO(
                "".join(json.dumps(item) + "\n" for item in requests)
            )
            output_stream = io.StringIO()

            class BaseAgent:
                pass

            bindings = AgentSBindings(
                agent_s3_class=BaseAgent,
                reflection_prompt="reflect",
                version=PINNED_AGENT_S_VERSION,
                commit=PINNED_AGENT_S_COMMIT,
            )

            class FakeAgent(BaseAgent):
                def __init__(self):
                    self.executor = type("E", (), {"turn_count": 1})()
                    self.warptest_usage_events = []
                    self.warptest_agent_s_version = PINNED_AGENT_S_VERSION
                    self.warptest_agent_s_commit = PINNED_AGENT_S_COMMIT

                def reset(self):
                    pass

                def predict(self, _instruction, _observation):
                    self.warptest_usage_events.append(
                        {
                            "model": "decision/model",
                            "kind": "agent_s_worker",
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "reasoning_tokens": 0,
                            "total_tokens": 2,
                            "usage_complete": True,
                        }
                    )
                    return {}, ["DONE"]

            with (
                patch.dict(
                    os.environ,
                    {"TEST_AGENT_S_KEY": "dummy", "TEST_AGENT_S_GROUND_KEY": "dummy"},
                ),
                patch("warptest.agent_s_worker._native_exec") as exec_mock,
            ):
                AgentSJsonlServer(
                    input_stream,
                    output_stream,
                    agent_factory=lambda _config, _bindings: FakeAgent(),
                    bindings_loader=lambda _root: bindings,
                ).serve()
            responses = [
                json.loads(line) for line in output_stream.getvalue().splitlines()
            ]
        self.assertEqual(responses[1]["result"]["action"]["kind"], "done")
        exec_mock.assert_not_called()


class NativeRequestTemperatureTests(unittest.TestCase):
    class TemperatureRejected(RuntimeError):
        status_code = 400

        def __init__(self) -> None:
            super().__init__("Unsupported value: 'temperature'")

    @staticmethod
    def _backend(usage_sink: list[dict]) -> ChatCompletionsBackend:
        return ChatCompletionsBackend(
            model="gpt-5.5",
            base_url="https://provider.invalid/v1",
            api_key_env="TEST_AGENT_S_KEY",
            default_headers={},
            temperature=1.0,
            timeout_s=30.0,
            max_retries=0,
            usage_sink=usage_sink,
            client=object(),
        )

    def test_explicit_400_retries_without_temperature_and_stays_sticky(self) -> None:
        calls: list[dict] = []

        class Completions:
            def create(self, **kwargs):
                calls.append(dict(kwargs))
                if len(calls) == 1:
                    raise NativeRequestTemperatureTests.TemperatureRejected()
                return _provider_response()

        usage: list[dict] = []
        backend = self._backend(usage)
        proxy = _NativeUsageRecordingCompletions(
            Completions(), backend=backend, kind="agent_s_worker"
        )
        request = {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "act"}],
        }
        with model_request_profiles_environment({"gpt-5.5": "reasoning_medium"}):
            proxy.create(**request)
            proxy.create(**request)

        self.assertEqual(calls[0]["temperature"], 1.0)
        self.assertEqual(calls[0]["max_tokens"], AGENT_S_REQUEST_MAX_TOKENS)
        self.assertEqual(calls[0]["reasoning_effort"], "medium")
        self.assertNotIn("temperature", calls[1])
        self.assertEqual(calls[1]["max_tokens"], AGENT_S_REQUEST_MAX_TOKENS)
        self.assertEqual(calls[1]["reasoning_effort"], "medium")
        self.assertNotIn("temperature", calls[2])
        self.assertEqual(calls[2]["reasoning_effort"], "medium")
        self.assertTrue(backend.omit_temperature_after_rejection)
        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[0]["temperature_configured"], 1.0)
        self.assertIsNone(usage[0]["temperature_requested"])
        self.assertTrue(usage[0]["temperature_fallback_triggered"])
        self.assertTrue(usage[0]["temperature_omitted_after_rejection"])
        self.assertEqual(usage[0]["max_tokens_requested"], AGENT_S_REQUEST_MAX_TOKENS)
        self.assertFalse(usage[1]["temperature_fallback_triggered"])
        self.assertTrue(usage[1]["temperature_omitted_after_rejection"])

    def test_non_400_or_untyped_error_never_changes_the_request(self) -> None:
        for status in (503, None):
            with self.subTest(status=status):
                calls: list[dict] = []

                class ProviderError(RuntimeError):
                    def __init__(self) -> None:
                        super().__init__("temperature failed locally")
                        if status is not None:
                            self.status_code = status

                class Completions:
                    def create(self, **kwargs):
                        calls.append(dict(kwargs))
                        raise ProviderError()

                usage: list[dict] = []
                backend = self._backend(usage)
                proxy = _NativeUsageRecordingCompletions(
                    Completions(), backend=backend, kind="agent_s_worker"
                )
                with model_request_profiles_environment(
                    {"gpt-5.5": "reasoning_medium"}
                ):
                    with self.assertRaises(ProviderError):
                        proxy.create(
                            model="gpt-5.5",
                            messages=[{"role": "user", "content": "act"}],
                        )
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["temperature"], 1.0)
                self.assertEqual(calls[0]["max_tokens"], AGENT_S_REQUEST_MAX_TOKENS)
                self.assertEqual(calls[0]["reasoning_effort"], "medium")
                self.assertFalse(backend.omit_temperature_after_rejection)

    def test_reasoning_high_reaches_native_agent_s_sdk_boundary(self) -> None:
        calls: list[dict] = []

        class Completions:
            def create(self, **kwargs):
                calls.append(dict(kwargs))
                return _provider_response()

        usage: list[dict] = []
        proxy = _NativeUsageRecordingCompletions(
            Completions(), backend=self._backend(usage), kind="agent_s_worker"
        )
        with model_request_profiles_environment({"kimi-k3": "reasoning_high"}):
            proxy.create(
                model="kimi-k3",
                messages=[{"role": "user", "content": "act"}],
            )

        self.assertEqual(calls[0]["reasoning_effort"], "high")
        self.assertTrue(usage[0]["reasoning_expected"])

    def test_disabled_thinking_blocks_reasoning_content_but_keeps_usage(self) -> None:
        calls: list[dict] = []

        class Completions:
            def create(self, **kwargs):
                calls.append(dict(kwargs))
                response = _provider_response("agent.click('button', 1, 'left')")
                response.choices[0].message.reasoning_content = "private reasoning"
                return response

        usage: list[dict] = []
        proxy = _NativeUsageRecordingCompletions(
            Completions(), backend=self._backend(usage), kind="agent_s_worker"
        )
        with model_request_profiles_environment({"kimi-k2.6": "thinking_disabled"}):
            with self.assertRaises(AgentSReasoningContentContractError):
                proxy.create(
                    model="kimi-k2.6",
                    messages=[{"role": "user", "content": "act"}],
                )

        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["total_tokens"], 5)
        self.assertTrue(usage[0]["reasoning_content_observed"])
        self.assertFalse(usage[0]["request_contract_passed"])
        self.assertFalse(usage[0]["reasoning_token_anomaly"])
        self.assertIsNone(usage[0]["response"])
        self.assertNotIn("private reasoning", repr(usage[0]))


class BackgroundPromptTests(unittest.TestCase):
    def test_forbidden_capability_sections_and_password_are_removed(self) -> None:
        upstream = (
            "You are an expert in graphical user interfaces and Python code.\n"
            "## Agent Usage Guidelines\n"
            "Use the Code Agent and call_code_agent for shell work.\n"
            "You are provided with:\n"
            "1. A screenshot.\n"
            "7. My computer's password is 'secret', feel free to use it when "
            "you need sudo rights.\n"
            "Use agent.done() when complete.\n"
        )

        prompt = _background_procedural_prompt(upstream)

        self.assertIn("TASK_DESCRIPTION", prompt)
        self.assertIn("You are provided with:", prompt)
        self.assertIn("agent.done()", prompt)
        lowered = prompt.lower()
        for forbidden in ("code agent", "call_code_agent", "password", "sudo"):
            self.assertNotIn(forbidden, lowered)

    def test_reflection_is_replaced_with_pixel_only_evidence_rules(self) -> None:
        upstream = (
            "You are an expert computer use agent.\n"
            "Your task is to generate a reflection.\n"
            "A code agent may have changed files using sudo.\n"
        )

        prompt = _background_reflection_prompt(upstream)

        self.assertIn("BACKGROUND PIXEL REFLECTION CONTRACT", prompt)
        self.assertIn("Only visible frame changes", prompt)
        lowered = prompt.lower()
        for forbidden in ("code agent", "call_code_agent", "password", "sudo"):
            self.assertNotIn(forbidden, lowered)


class RealPinnedNativeWorkerTests(unittest.TestCase):
    """Exercises the real pinned upstream S3 path with only the HTTP client faked."""

    def test_real_upstream_background_agent_reaches_done_without_pyautogui(
        self,
    ) -> None:
        python = ROOT / "baselines" / "agent-s" / ".venv" / "bin" / "python"
        if not python.is_file():
            self.skipTest("local Agent-S Python 3.12 environment is not installed")
        script = textwrap.dedent(
            """
            import json
            from pathlib import Path
            from types import SimpleNamespace
            from warptest.agent_s_bridge import AgentSBridgeConfig
            from warptest.agent_s_worker import WorkerConfig, build_native_agent_s3, load_agent_s_bindings

            root = Path.cwd()
            payload = AgentSBridgeConfig(
                model="decision", base_url="https://invalid/v1", api_key_env="TEST_AGENT_S_KEY",
                grounder_model="grounder", grounder_base_url="https://invalid/v1",
                grounder_api_key_env="TEST_AGENT_S_GROUND_KEY",
                native_screen_width=16, native_screen_height=12,
                execution_backend="background_pixel_v1",
            ).to_worker_payload(agent_s_root=root / "baselines" / "agent-s")
            config = WorkerConfig.from_mapping(payload)
            bindings = load_agent_s_bindings(config.agent_s_root)

            class Completions:
                def __init__(self): self.calls = []
                def create(self, **kwargs):
                    self.calls.append(kwargs)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(
                                content='```python\\nagent.done()\\n```',
                                reasoning_content=None,
                            ),
                        )],
                        usage=SimpleNamespace(
                            prompt_tokens=2, completion_tokens=3, total_tokens=5,
                            prompt_tokens_details=None,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
                        ),
                    )
            clients = []
            def factory(**_kwargs):
                client = SimpleNamespace()
                client.completions = Completions()
                client.chat = SimpleNamespace(completions=client.completions)
                clients.append(client)
                return client

            agent = build_native_agent_s3(
                config, bindings, client_factory=factory, pyautogui_size=(16, 12)
            )
            screenshot_bytes = (root / "baselines" / "agent-s" / "README.md").read_bytes()[:64]
            _info, actions = agent.predict("finish the task", {"screenshot": screenshot_bytes})
            calls = [call for client in clients for call in client.completions.calls]
            worker_call = next(call for call in calls if call.get("model") == "decision")
            system_prompt = worker_call["messages"][0]["content"][0]["text"]
            reflection_prompt = agent.executor.reflection_agent.system_prompt
            print(json.dumps({
                "worker_mro": [f"{item.__module__}.{item.__name__}" for item in type(agent.executor).__mro__],
                "action": actions[0],
                "usage_kinds": sorted({event["kind"] for event in agent.warptest_usage_events}),
                "usage_complete": all(event.get("usage_complete") for event in agent.warptest_usage_events),
                "max_tokens": worker_call.get("max_tokens"),
                "system_prompt": system_prompt,
                "reflection_prompt": reflection_prompt,
            }))
            """
        )
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(ROOT),
                "TEST_AGENT_S_KEY": "dummy-not-used",
                "TEST_AGENT_S_GROUND_KEY": "dummy-not-used",
            }
        )
        completed = subprocess.run(
            [str(python), "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        # Native mode silences upstream ``call_llm_safe`` prints so they
        # cannot corrupt the JSONL stdout protocol.
        lines = completed.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, msg=completed.stdout)
        result = json.loads(lines[0])
        self.assertIn("gui_agents.s3.agents.worker.Worker", result["worker_mro"])
        self.assertEqual(result["action"].strip(), "DONE")
        self.assertIn("agent_s_worker", result["usage_kinds"])
        self.assertTrue(result["usage_complete"])
        self.assertEqual(result["max_tokens"], AGENT_S_REQUEST_MAX_TOKENS)
        lowered = result["system_prompt"].lower()
        for forbidden in ("code agent", "call_code_agent", "password", "sudo"):
            self.assertNotIn(forbidden, lowered)
        reflection_lowered = result["reflection_prompt"].lower()
        self.assertIn("background pixel reflection contract", reflection_lowered)
        for forbidden in ("code agent", "call_code_agent", "password", "sudo"):
            self.assertNotIn(forbidden, reflection_lowered)

    def test_grounding_resize_uses_logical_click_size_not_physical_screenshot_size(
        self,
    ) -> None:
        """Regression test for the Retina physical/logical coordinate mismatch.

        ``grounding_width/height`` (the image space the model reasons in) must
        stay at the physical screenshot size (``native_screen_width/height``),
        while ``OSWorldACI.width/height`` (what ``resize_coordinates`` scales
        *into*, i.e. pyautogui's actual click space) must be whatever
        ``pyautogui_size`` reports -- these must be free to differ, and
        ``resize_coordinates`` must scale by their ratio.
        """

        python = ROOT / "baselines" / "agent-s" / ".venv" / "bin" / "python"
        if not python.is_file():
            self.skipTest("local Agent-S Python 3.12 environment is not installed")
        script = textwrap.dedent(
            """
            import json
            from pathlib import Path
            from types import SimpleNamespace
            from warptest.agent_s_bridge import AgentSBridgeConfig
            from warptest.agent_s_worker import WorkerConfig, build_native_agent_s3, load_agent_s_bindings

            root = Path.cwd()
            # Physical screenshot space (3024x1964-like Retina ratio, scaled down)
            # is deliberately *not* a clean multiple of the logical click space
            # (1512x982-like) below, so a bug that used the wrong one would fail
            # this test's exact-ratio assertion.
            payload = AgentSBridgeConfig(
                model="decision", base_url="https://invalid/v1", api_key_env="TEST_AGENT_S_KEY",
                grounder_model="grounder", grounder_base_url="https://invalid/v1",
                grounder_api_key_env="TEST_AGENT_S_GROUND_KEY",
                native_screen_width=3024, native_screen_height=1964,
            ).to_worker_payload(agent_s_root=root / "baselines" / "agent-s")
            config = WorkerConfig.from_mapping(payload)
            bindings = load_agent_s_bindings(config.agent_s_root)

            def factory(**_kwargs):
                client = SimpleNamespace()
                client.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_kw: None))
                return client

            agent = build_native_agent_s3(
                config, bindings, client_factory=factory,
                pyautogui_size=(1512, 982),
            )
            grounding_agent = agent.executor.grounding_agent
            print(json.dumps({
                "width": grounding_agent.width,
                "height": grounding_agent.height,
                "grounding_width": grounding_agent.engine_params_for_grounding["grounding_width"],
                "grounding_height": grounding_agent.engine_params_for_grounding["grounding_height"],
                "resized": grounding_agent.resize_coordinates([3024, 1964]),
            }))
            """
        )
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(ROOT),
                "TEST_AGENT_S_KEY": "dummy-not-used",
                "TEST_AGENT_S_GROUND_KEY": "dummy-not-used",
            }
        )
        completed = subprocess.run(
            [str(python), "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["grounding_width"], 3024)
        self.assertEqual(result["grounding_height"], 1964)
        self.assertEqual(result["width"], 1512)
        self.assertEqual(result["height"], 982)
        # A point at the far corner of the physical screenshot must resize
        # down to the logical click space, not pass through unscaled.
        self.assertEqual(result["resized"], [1512, 982])


if __name__ == "__main__":
    unittest.main()
