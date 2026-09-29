"""Offline unit tests for the ScaleCUA worker: prompt build, parse, usage."""

from __future__ import annotations

import io
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from warptest.scalecua_worker import (
    SCALECUA_PROMPT_TEMPLATE_PATH,
    ScaleCUAChatClient,
    ScaleCUAJsonlServer,
    WorkerConfig,
    build_scalecua_actions,
    load_scalecua_bindings,
    parse_scalecua_response,
)
from warptest.model_request import model_request_profiles_environment


ROOT = Path(__file__).resolve().parents[1]
SCALECUA_ROOT = ROOT / "baselines" / "ScaleCUA"


def _png(width: int = 1280, height: int = 800) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (60, 60, 60)).save(buf, format="PNG")
    return buf.getvalue()


class WorkerConfigTests(unittest.TestCase):
    def _mapping(self, **overrides):
        values = dict(
            model="Qwen/Qwen3.8-27B",
            base_url="https://scalecua.invalid/v1",
            api_key_env="TEST_SCALECUA_KEY",
            scalecua_root=str(SCALECUA_ROOT),
            native_screen_width=1280,
            native_screen_height=800,
        )
        values.update(overrides)
        return values

    def test_rejects_non_background_execution(self) -> None:
        with self.assertRaises(ValueError):
            WorkerConfig(self._mapping(execution_backend="foreground_native_v1"))

    def test_rejects_wrong_coordinate_space(self) -> None:
        with self.assertRaises(ValueError):
            WorkerConfig(self._mapping(coordinate_space="screen_pixels_v1"))

    def test_rejects_raw_key_values(self) -> None:
        with self.assertRaises(ValueError):
            WorkerConfig(self._mapping(api_key_env="sk-abc123"))


class ResponseParsingTests(unittest.TestCase):
    def test_multiline_action_block(self) -> None:
        response = (
            "<think>open the file menu</think>\n"
            "<operation>click File then type name</operation>\n"
            "<action>\nclick(x=100, y=200)\nwrite(message='hello')\n</action>"
        )
        think, operation, actions = parse_scalecua_response(response)
        self.assertEqual(think, "open the file menu")
        self.assertEqual(operation, "click File then type name")
        self.assertEqual(actions, ["click(x=100, y=200)", "write(message='hello')"])

    def test_empty_response_has_no_actions(self) -> None:
        _think, _operation, actions = parse_scalecua_response("no blocks at all")
        self.assertEqual(actions, [])

    def test_press_expands_to_key_actions(self) -> None:
        _t, _o, actions, skipped = build_scalecua_actions(
            "<action>\npress(keys='enter', presses=2)\n</action>",
            screenshot_bytes=_png(56, 56),
            width=56,
            height=56,
        )
        self.assertEqual(skipped, [])
        self.assertEqual([a["kind"] for a in actions], ["key", "key"])

    def test_unsupported_verb_is_skipped_not_executed(self) -> None:
        _t, _o, actions, skipped = build_scalecua_actions(
            "<action>\nkeyDown(key='shift')\n</action>",
            screenshot_bytes=_png(56, 56),
            width=56,
            height=56,
        )
        self.assertEqual(actions, [])
        self.assertTrue(any("not representable" in note for note in skipped))

    def test_json_action_fallback_from_non_native_models(self) -> None:
        # General-purpose decision models emit JSON action arrays instead of
        # the native pyautogui grammar; the parser must tolerate them.
        _t, _o, actions, skipped = build_scalecua_actions(
            '[{"name": "click", "args": {"x": 640, "y": 400}}]',
            screenshot_bytes=_png(),
            width=1280,
            height=800,
        )
        self.assertEqual(skipped, [])
        self.assertEqual(actions[0]["kind"], "click")

    def test_xml_key_tag_fallback(self) -> None:
        _t, _o, actions, skipped = build_scalecua_actions(
            "<press>\nLeft\n</press>",
            screenshot_bytes=_png(),
            width=1280,
            height=800,
        )
        self.assertEqual(skipped, [])
        self.assertEqual(actions[0]["kind"], "key")
        self.assertEqual(actions[0]["key"], "Left")

    def test_native_grammar_takes_precedence(self) -> None:
        _t, _o, actions, _skipped = build_scalecua_actions(
            "<action>\nclick(x=100, y=200)\n</action>",
            screenshot_bytes=_png(),
            width=1280,
            height=800,
        )
        self.assertEqual(actions[0]["kind"], "click")
        self.assertTrue(actions[0]["raw"].startswith("click("))

    def test_no_action_block_raises_info_for_protocol(self) -> None:
        _t, _o, actions, skipped = build_scalecua_actions(
            "just chatter",
            screenshot_bytes=_png(56, 56),
            width=56,
            height=56,
        )
        self.assertEqual(actions, [])
        self.assertEqual(skipped, ["response carried no <action> block"])


class ScaleCUAChatClientTests(unittest.TestCase):
    def test_error_accounting_preserves_reported_usage_and_unknown_timeouts(self) -> None:
        class ProviderError(Exception):
            pass

        rejected = ProviderError("rate limited before generation")
        rejected.status_code = 429
        accounted = ProviderError("provider error with metered completion")
        accounted.status_code = 500
        accounted.body = {"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
        for error, expected_calls, expected_complete in (
            (rejected, 0, None),
            (accounted, 1, True),
            (TimeoutError("read timed out after dispatch"), 1, False),
        ):
            with self.subTest(error=str(error)):
                sink = []
                client = self._client(sink, None)
                client._record_error(
                    error, model="gpt-5.6-terra", messages=[],
                    requested_at="2026-09-10T00:00:00Z", start=time.perf_counter(),
                )
                self.assertEqual(len(sink), expected_calls)
                if sink:
                    self.assertIs(sink[0]["usage_complete"], expected_complete)
                    if expected_complete:
                        self.assertEqual(sink[0]["total_tokens"], 10)
                    else:
                        from warptest import cost
                        from warptest.agent_s_bridge import AgentSBridgeClient, AgentSUsageAccountingError
                        self.assertFalse(sink[0]["total_tokens_known"])
                        with cost.scoped_meter() as meter:
                            with self.assertRaises(AgentSUsageAccountingError):
                                AgentSBridgeClient._record_usage_events(sink)
                        self.assertIsNone(meter.summary()["calls"][0]["total_tokens"])

    def _client(self, sink, response):
        def factory(**kwargs):
            return lambda **kw: response

        client = ScaleCUAChatClient(
            model="Qwen/Qwen3.8-27B",
            base_url="https://scalecua.invalid/v1",
            api_key_env="TEST_SCALECUA_KEY",
            default_headers={},
            temperature=0.2,
            timeout_s=30.0,
            max_retries=0,
            usage_sink=sink,
        )
        client._get_client = lambda: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: response)
            )
        )
        return client

    def test_usage_event_is_fail_closed_complete(self) -> None:
        sink = []
        response = SimpleNamespace(
            id="r1",
            model="Qwen/Qwen3.8-27B",
            system_fingerprint=None,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content="<action>\nclick(x=1, y=2)\n</action>",
                        reasoning_content=None,
                    ),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                reasoning_tokens=None,
                prompt_tokens_details=None,
                completion_tokens_details=None,
            ),
        )
        client = self._client(sink, response)
        with model_request_profiles_environment({"Qwen/Qwen3.8-27B": "thinking_disabled"}):
            content = client.create(
                model="Qwen/Qwen3.8-27B", messages=[{"role": "user", "content": "hi"}]
            )
        self.assertIn("<action>", content)
        self.assertEqual(len(sink), 1)
        self.assertTrue(sink[0]["usage_complete"])
        self.assertEqual(sink[0]["total_tokens"], 15)
        self.assertTrue(sink[0]["request_contract_passed"])


class ScaleCUAJsonlServerTests(unittest.TestCase):
    def test_surface_rebinding_keeps_web_capture_and_click_metadata_consistent(self) -> None:
        server, _frame, _sha, tmp = self._server("<action>wait()</action>")
        self.addCleanup(tmp.cleanup)
        metadata = server.handle({"op": "init", "config": {
            "model": "gpt-5.6-terra", "base_url": "https://scalecua.invalid/v1",
            "api_key_env": "TEST_SCALECUA_KEY", "scalecua_root": str(SCALECUA_ROOT),
            "native_screen_width": 1280, "native_screen_height": 800,
        }})
        # One lane visits native desktop and browser tasks in succession.
        for width, height in [(1440, 1000), (1280, 720), (1280, 800)]:
            metadata.update(server.handle({"op": "configure_surface", "width": width, "height": height}))
            self.assertEqual(metadata["native_screen_width"], width)
            self.assertEqual(metadata["native_click_width"], width)
            self.assertEqual(metadata["native_screen_height"], height)
            self.assertEqual(metadata["native_click_height"], height)

    def _server(self, reply_content: str):

        class FakeCompletions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    id="r1",
                    model="Qwen/Qwen3.8-27B",
                    system_fingerprint=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop",
                            message=SimpleNamespace(
                                content=reply_content, reasoning_content=None
                            ),
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=10,
                        completion_tokens=5,
                        total_tokens=15,
                        reasoning_tokens=None,
                        prompt_tokens_details=None,
                        completion_tokens_details=None,
                    ),
                )

        class FakeClient:
            def __init__(self, **kwargs):
                self.model = kwargs["model"]
                self.completions = FakeCompletions()

            class chat:  # noqa: N801 - simple namespace shim
                pass

        def factory(**kwargs):
            sink = kwargs["usage_sink"]

            def create(**_kw):
                sink.append(
                    {
                        "model": kwargs["model"],
                        "kind": "scalecua_worker",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                        "usage_complete": True,
                        "request_contract_passed": True,
                    }
                )
                return reply_content

            client = SimpleNamespace(model=kwargs["model"])
            client.create = create
            return client

        import tempfile

        tmp = tempfile.TemporaryDirectory()
        frame = Path(tmp.name) / "frame.png"
        frame.write_bytes(_png())
        import hashlib

        sha = hashlib.sha256(frame.read_bytes()).hexdigest()
        server = ScaleCUAJsonlServer(io.StringIO(), io.StringIO(), client_factory=factory)
        return server, frame, sha, tmp

    def test_full_protocol_round_trip(self) -> None:
        server, frame, sha, tmp = self._server(
            "<think>ok</think>\n<operation>o</operation>\n<action>\nclick(x=640, y=400)\n</action>"
        )
        init = server.handle(
            {
                "op": "init",
                "config": {
                    "model": "Qwen/Qwen3.8-27B",
                    "base_url": "https://scalecua.invalid/v1",
                    "api_key_env": "TEST_SCALECUA_KEY",
                    "scalecua_root": str(SCALECUA_ROOT),
                    "native_screen_width": 1280,
                    "native_screen_height": 800,
                },
            }
        )
        self.assertEqual(init["protocol_version"], 1)
        self.assertEqual(init["action_execution_owner"], "parent_runtime")
        server.handle({"op": "reset", "task": "task-1"})
        result = server.handle(
            {
                "op": "predict",
                "task": "task-1",
                "instruction": "click the middle",
                "observation": "raw_screenshot",
                "screenshot": str(frame),
                "width": 1280,
                "height": 800,
                "history": [],
                "policy_frame": {
                    "session_id": "s",
                    "frame_id": 0,
                    "sha256": sha,
                },
            }
        )
        self.assertTrue(result["usage_accounting_complete"])
        self.assertEqual(result["action"]["kind"], "click")
        # The model-space click at (640, 400) rescales into the 1280x800 logical
        # frame with the smart-resize rounding (1280 -> 1288 model columns).
        self.assertAlmostEqual(result["actions"][0]["x"], 640, delta=10)
        self.assertAlmostEqual(result["actions"][0]["y"], 400, delta=10)
        tmp.cleanup()

    def test_stale_frame_id_is_rejected(self) -> None:
        server, frame, sha, tmp = self._server(
            "<action>\nclick(x=640, y=400)\n</action>"
        )
        server.handle(
            {
                "op": "init",
                "config": {
                    "model": "Qwen/Qwen3.8-27B",
                    "base_url": "https://scalecua.invalid/v1",
                    "api_key_env": "TEST_SCALECUA_KEY",
                    "scalecua_root": str(SCALECUA_ROOT),
                    "native_screen_width": 1280,
                    "native_screen_height": 800,
                },
            }
        )
        server.handle({"op": "reset", "task": "task-1"})
        request = {
            "op": "predict",
            "task": "task-1",
            "instruction": "click",
            "observation": "raw_screenshot",
            "screenshot": str(frame),
            "width": 1280,
            "height": 800,
            "history": [],
            "policy_frame": {"session_id": "s", "frame_id": 5, "sha256": sha},
        }
        server.handle(request)
        with self.assertRaises(ValueError):
            server.handle(request)
        tmp.cleanup()


class BindingsTests(unittest.TestCase):
    def test_pinned_checkout_loads_template(self) -> None:
        bindings = load_scalecua_bindings(SCALECUA_ROOT)
        self.assertIn("sys_prompt_planning_withoutcot", bindings.template)
        self.assertTrue((bindings.root / SCALECUA_PROMPT_TEMPLATE_PATH).is_file())


if __name__ == "__main__":
    unittest.main()
